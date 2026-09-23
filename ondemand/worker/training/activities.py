"""
Training activity: provision a RunPod GPU, run a LoRA fine-tune, land the adapter in R2.

Contract with the training image (build the image to match this):
  The pod is created from ``training_image`` with these env vars, and its
  ENTRYPOINT must:
    1. Read R2_ENDPOINT / R2_ACCESS_KEY / R2_SECRET_KEY / R2_BUCKET.
    2. Download the dataset at DATASET_KEY from R2.
    3. Fine-tune ``BASE_MODEL`` with Unsloth/LoRA using the full hyperparameter
       set in HYPERPARAMS_JSON (a JSON object; every knob has a default).
    4. Upload the adapter files under OUTPUT_PREFIX/ in R2.
    5. Write an empty object at OUTPUT_PREFIX/_SUCCESS as the LAST step (or
       OUTPUT_PREFIX/_FAILED on error), then exit.

The activity polls R2 for the _SUCCESS/_FAILED marker — the marker is the source
of truth, not the pod's own progress %, which the spike found unreliable
(it hung at 99% after finishing). The RunPod pod is always torn down, even on
failure or cancellation, via the client's context manager.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict

from temporalio import activity

from ondemand.worker.training.input import TrainingInput, TrainingResult

logger = logging.getLogger("training")

# How often to poll R2 for the completion marker.
_POLL_INTERVAL_SECONDS = 15
# R2 credential env vars passed through to the training pod so it can up/download.
_R2_ENV_KEYS = ("R2_ENDPOINT", "R2_ACCESS_KEY", "R2_SECRET_KEY", "R2_BUCKET")


def _pod_env(input: TrainingInput) -> Dict[str, str]:
    """Build the env dict handed to the training pod.

    All hyperparameters travel as a single ``HYPERPARAMS_JSON`` blob so adding a
    knob never means touching this passthrough — the image reads the whole set.
    """
    env = {k: os.environ[k] for k in _R2_ENV_KEYS if os.environ.get(k)}
    env.update(
        {
            "DATASET_KEY": input.dataset_key,
            "BASE_MODEL": input.base_model,
            "OUTPUT_PREFIX": input.output_prefix,
            "HYPERPARAMS_JSON": json.dumps(input.hyperparams),
        }
    )
    return env


@activity.defn
def run_training_job(input: TrainingInput) -> TrainingResult:
    """Provision GPU, train, publish adapter. Sync activity (blocking IO/poll)."""
    from ondemand.worker.activity_reporter import report
    from ondemand.shared.runpod import get_runpod_client
    from ondemand.shared.r2_storage import get_r2_client

    if not input.dataset_key:
        raise ValueError("TrainingInput.dataset_key is required")
    if not input.training_image:
        raise ValueError("TrainingInput.training_image is required")

    r2 = get_r2_client()
    runpod = get_runpod_client()
    success_marker = f"{input.output_prefix}/_SUCCESS"
    failed_marker = f"{input.output_prefix}/_FAILED"

    report.step_started("provision", "Provisionar GPU no RunPod")
    logger.info("Provisionando GPU %s (imagem %s)", input.gpu_type, input.training_image)

    volume_id = None
    if input.volume_name:
        volume_id = runpod.ensure_volume(
            input.volume_name, size_gb=60, region=input.region
        )

    pod_kwargs = dict(
        gpu_type=input.gpu_type,
        image=input.training_image,
        name=f"train-{input.workflow_id}",
        env=_pod_env(input),
        volume_id=volume_id,
    )

    # The context manager guarantees teardown even if the body raises/cancels.
    with runpod.dedicated_pod(**pod_kwargs) as pod:
        report.step_completed("provision", "Provisionar GPU no RunPod",
                              summary=f"pod {pod.id}")
        report.step_started("train", "Treino LoRA (Unsloth)")
        logger.info("Pod %s treinando; aguardando marcador em %s", pod.id, success_marker)

        # Poll R2 for the terminal marker. start_to_close_timeout on the activity
        # bounds the total wait; heartbeat keeps Temporal/KEDA aware we are alive.
        while True:
            activity.heartbeat(f"waiting for {success_marker}")
            if r2.object_exists(success_marker):
                logger.info("Treino concluído (marcador _SUCCESS presente)")
                break
            if r2.object_exists(failed_marker):
                report.step_failed("train", "Treino LoRA (Unsloth)",
                                   error="training pod wrote _FAILED")
                raise RuntimeError(f"Training failed: {failed_marker} present in R2")
            time.sleep(_POLL_INTERVAL_SECONDS)

        report.step_completed("train", "Treino LoRA (Unsloth)")

    # Pod is down here. Publish the manifest that points at this adapter.
    report.step_started("publish", "Publicar adapter")
    manifest = {
        "client_code": input.client_code,
        "company_code": input.company_code,
        "adapter_prefix": input.output_prefix,
        "base_model": input.base_model,
        "workflow_id": input.workflow_id,
        "hyperparams": input.hyperparams,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    body = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    versioned_key = f"{input.manifest_prefix}/{input.workflow_id}.json"
    latest_key = f"{input.manifest_prefix}/latest.json"
    r2.put_bytes(versioned_key, body, content_type="application/json")
    r2.put_bytes(latest_key, body, content_type="application/json")
    report.step_completed("publish", "Publicar adapter", summary=input.output_prefix)
    logger.info("Adapter publicado em %s (manifesto %s)", input.output_prefix, latest_key)

    return TrainingResult(
        status="success",
        adapter_prefix=input.output_prefix,
        base_model=input.base_model,
        manifest_key=latest_key,
        hyperparams=input.hyperparams,
    )


# Convenience list for worker registration.
training_activities = [run_training_job]
