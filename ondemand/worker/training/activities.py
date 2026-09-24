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
# How many DIFFERENT GPUs to try when a pod fails for a host/driver reason before
# giving up. Bounded so a bad-host streak can't burn money forever.
_MAX_HW_ATTEMPTS = 3

# Substrings in a pod's _FAILED reason that mean the HOST is bad (wrong/old driver,
# no usable GPU, eviction) rather than the training itself. On these we exclude that
# GPU type and re-provision on a different one. OOM is deliberately NOT here: it is a
# config/VRAM problem (lower batch or raise min_vram_gb), not a bad host to skip.
_INFRA_FAILURE_SIGNS = (
    "cannot find any torch accelerator",
    "no cuda-capable device",
    "cuda initialization",
    "cuda driver version is insufficient",
    "driver on your system is too old",
    "forward compatibility",
    "cuinit",
    "no gpu",
    "device-side assert",
    "cuda unknown error",
)


def _is_infra_failure(reason: str) -> bool:
    """True if a _FAILED reason looks like a bad host (driver/GPU), not a real
    training bug. Drives 'exclude this GPU and re-provision on another'."""
    r = (reason or "").lower()
    return any(sign in r for sign in _INFRA_FAILURE_SIGNS)


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
            # Ship the pod's own logs to R2 so they survive its death (the pod
            # appends {RUNPOD_POD_ID}.log). See ``gpu_log_key`` for the full path.
            "GPU_LOG_R2_PREFIX": gpu_log_prefix(input.workflow_id),
        }
    )
    return env


def gpu_log_prefix(workflow_id: str) -> str:
    """R2 prefix under which a GPU pod ships its log, as a Run artifact.

    The pod appends ``{RUNPOD_POD_ID}.log`` — the pod id is the GPU's identifier,
    so a retry (new pod) writes its own file instead of overwriting the last one.
    """
    return f"artifacts/{workflow_id}/gpu-logs/"


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
    logger.info(
        "Provisionando GPU (min %sGB, clouds %s, imagem %s)",
        input.min_vram_gb, input.cloud_types, input.training_image,
    )

    volume_id = None
    if input.volume_name:
        volume_id = runpod.ensure_volume(
            input.volume_name, size_gb=60, region=input.region
        )

    # Provision + train with hardware-failure resilience. If a pod fails for a HOST
    # reason (driver too old, no CUDA device, eviction), exclude that GPU type and
    # re-provision on a DIFFERENT one instead of retrying the same bad host (which is
    # what plain Temporal retry did: the selector re-picked the same cheapest box).
    # A REAL training failure (a bug, bad data) is surfaced with its reason from the
    # pod's _FAILED marker and not retried on new hardware. A pinned gpu_type opts
    # out of re-selection. The context manager guarantees teardown every attempt.
    excluded_gpu_types: set = set()
    last_reason = None
    trained = False
    for hw_attempt in range(1, _MAX_HW_ATTEMPTS + 1):
        if hw_attempt > 1:
            logger.warning(
                "Re-provisionando GPU (tentativa %s/%s); tipos excluídos: %s",
                hw_attempt, _MAX_HW_ATTEMPTS, excluded_gpu_types or "-",
            )
        retry_hw = False
        # Clear stale completion markers before provisioning. The markers live under
        # the workflow_id, which is SHARED across hardware retries AND Temporal
        # activity retries — so a marker written by a previous pod would make the
        # poller "see" this fresh pod finish in ~1s (it never ran). Clearing here,
        # right before each provision, means the poller waits for THIS pod only.
        r2.delete(failed_marker)
        r2.delete(success_marker)
        with runpod.dedicated_pod(
            image=input.training_image,
            name=f"train-{input.workflow_id}",
            min_vram_gb=input.min_vram_gb,
            cloud_types=input.cloud_types,
            env=_pod_env(input),
            volume_id=volume_id,
            max_wait_seconds=input.max_provision_wait_seconds,
            heartbeat=activity.heartbeat,
            gpu_type=input.gpu_type,
            exclude_gpu_types=excluded_gpu_types,
            allowed_cuda_versions=input.allowed_cuda_versions,
        ) as pod:
            gpu_log_key = f"{gpu_log_prefix(input.workflow_id)}{pod.id}.log"
            report.step_completed("provision", "Provisionar GPU no RunPod",
                                  summary=f"pod {pod.id} | logs: {gpu_log_key}")
            report.step_started("train", "Treino LoRA (Unsloth)")
            logger.info("Pod %s treinando; logs em r2://%s; aguardando marcador em %s",
                        pod.id, gpu_log_key, success_marker)

            # Poll R2 for the terminal marker. start_to_close_timeout on the activity
            # bounds the total wait; heartbeat keeps Temporal/KEDA aware we are alive.
            while True:
                activity.heartbeat(f"waiting for {success_marker}")
                if r2.object_exists(success_marker):
                    logger.info("Treino concluído (marcador _SUCCESS presente)")
                    trained = True
                    break
                if r2.object_exists(failed_marker):
                    # The pod writes its error/traceback into the _FAILED body, so we
                    # surface the actual cause instead of just "a marker exists".
                    reason = r2.get_text(failed_marker) or "(pod escreveu _FAILED sem detalhe)"
                    first_line = next((l for l in reason.splitlines() if l.strip()), "_FAILED")
                    if (_is_infra_failure(reason) and not input.gpu_type
                            and hw_attempt < _MAX_HW_ATTEMPTS):
                        excluded_gpu_types.add(pod.gpu_type_id)
                        last_reason = reason
                        logger.warning("Falha de infra na GPU %s (%s): %s — trocando de GPU",
                                       pod.gpu_type_id, pod.id, first_line)
                        report.step_failed(
                            "train", "Treino LoRA (Unsloth)",
                            error=f"infra na GPU {pod.gpu_type_id}: {first_line} — reprovisionando",
                        )
                        retry_hw = True
                    else:
                        report.step_failed("train", "Treino LoRA (Unsloth)", error=first_line)
                        raise RuntimeError(f"Training failed: {reason}")
                    break
                # Liveness: a pod that died without a marker (e.g. a Community-cloud
                # eviction) is a host failure — exclude it and re-provision.
                if not runpod.pod_alive(pod):
                    if r2.object_exists(success_marker):
                        logger.info("Treino concluído (pod saiu; _SUCCESS presente)")
                        trained = True
                        break
                    if not input.gpu_type and hw_attempt < _MAX_HW_ATTEMPTS:
                        excluded_gpu_types.add(pod.gpu_type_id)
                        last_reason = f"pod {pod.id} morreu sem marcador (eviction?)"
                        logger.warning("%s — trocando de GPU", last_reason)
                        report.step_failed("train", "Treino LoRA (Unsloth)",
                                           error="pod morreu sem marcador (eviction?) — reprovisionando")
                        retry_hw = True
                        break
                    report.step_failed("train", "Treino LoRA (Unsloth)",
                                       error="pod died without a completion marker")
                    raise RuntimeError(
                        f"Training pod {pod.id} died without _SUCCESS/_FAILED "
                        "(likely a Community-cloud eviction)"
                    )
                time.sleep(_POLL_INTERVAL_SECONDS)
        # pod torn down here (context manager)
        if trained:
            report.step_completed("train", "Treino LoRA (Unsloth)")
            break
        if retry_hw:
            report.step_started("provision", "Provisionar GPU no RunPod")
            continue
        break

    if not trained:
        raise RuntimeError(
            f"Treino falhou após tentar {_MAX_HW_ATTEMPTS} GPUs distintas "
            f"(excluídas: {excluded_gpu_types or '-'}). Última causa: {last_reason}"
        )

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
