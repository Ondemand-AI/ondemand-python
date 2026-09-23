"""
RunPod client for Ondemand GPU workloads (training + inference).

Two lanes, one interface:
  - **dedicated**: provision an on-demand GPU pod, use it, and tear it down. The
    library guarantees the teardown (use ``dedicated_pod(...)`` as a context
    manager, or wrap ``provision_pod`` / ``teardown`` in the workflow's own
    finally). A forgotten GPU is the expensive failure mode; teardown must not
    depend on the caller finishing cleanly.
  - **serverless**: call a pre-deployed RunPod Serverless endpoint that scales to
    zero on its own. No teardown to manage.

Storage split (see the platform decision): **R2 is the durable origin** of base
weights and LoRA adapters; a **RunPod network volume is a warm cache**. This
client can ensure a network volume exists and attach it to a pod; the pod itself
rehydrates the volume from R2 on a miss (the client never streams weights).

Credentials: the RunPod API key is injected as a Kubernetes secret, exactly like
R2, HyperDX and the Bitwarden bootstrap creds — the pod reads it from the
``RUNPOD_API_KEY`` env var. Each automation references its OWN k8s secret in its
``scaledjob.yml`` (separation of cost and blast radius); the env var name is the
same everywhere, so the library stays agnostic. If a GPU function is called with
no key set, the library raises a loud, explicit ``RunPodKeyMissingError``.

Version note: the ``runpod`` SDK and REST API drift between releases. The exact
SDK calls are isolated in small private methods and flagged VERIFY; confirm them
against the pinned ``runpod`` version before the first real run.
"""

import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Optional SDK — isolated behind the [runpod] extra so a CPU robot never needs it.
try:
    import runpod as _runpod_sdk
    RUNPOD_AVAILABLE = True
except ImportError:  # pragma: no cover
    _runpod_sdk = None
    RUNPOD_AVAILABLE = False
    logger.warning("runpod SDK not installed. GPU provisioning will not be available.")

REST_BASE = "https://rest.runpod.io/v1"  # VERIFY: network-volume REST base for the account


class RunPodKeyMissingError(RuntimeError):
    """Raised when a GPU operation is attempted with no RUNPOD_API_KEY set."""


def get_runpod_api_key() -> str:
    """Return the RunPod API key from the ``RUNPOD_API_KEY`` env var.

    The key is injected as a Kubernetes secret (see the module docstring). If it
    is not set, raise a loud, actionable error rather than failing obscurely deep
    inside an SDK call.
    """
    key = os.environ.get("RUNPOD_API_KEY")
    if not key:
        raise RunPodKeyMissingError(
            "RUNPOD_API_KEY is not set, but this automation just tried to use the "
            "GPU (RunPod). Each automation gets its own RunPod key injected as a "
            "Kubernetes secret, the same way R2 and the Bitwarden bootstrap creds "
            "are. To fix: add RUNPOD_API_KEY to this automation's k8s secret and "
            "reference it via `secretKeyRef` in its scaledjob.yml (see the demo "
            "robot's manifest for the R2 pattern). For a local run, export "
            "RUNPOD_API_KEY in your shell."
        )
    return key


@dataclass
class PodHandle:
    """A provisioned GPU pod. ``endpoint_url`` is set once the pod is ready."""

    id: str
    mode: str = "dedicated"
    endpoint_url: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


class RunPodClient:
    """Provision / serve / tear down GPU work on RunPod.

    Args:
        api_key: overrides the RUNPOD_API_KEY env lookup (e.g. for tests).
        region: default datacenter/region; a network volume is region-locked and
            the pod must be created in the same region.
    """

    def __init__(self, api_key: Optional[str] = None, region: Optional[str] = None):
        self._api_key = api_key
        self.region = region or os.environ.get("RUNPOD_REGION")
        self._key_set = False

    def _key(self) -> str:
        return self._api_key or get_runpod_api_key()

    def _ensure_key(self) -> None:
        if not RUNPOD_AVAILABLE:
            raise RuntimeError("runpod SDK is not installed. Run: pip install runpod")
        if not self._key_set:
            _runpod_sdk.api_key = self._key()
            self._key_set = True

    def _rest_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key()}",
            "Content-Type": "application/json",
        }

    # ----------------------------------------------------------------- volumes
    def ensure_volume(self, name: str, size_gb: int, region: Optional[str] = None) -> str:
        """Ensure a network volume named ``name`` exists; return its id (idempotent).

        Only ensures existence. Filling it (base weights, adapters) happens
        pod-side, from R2, on first use. Region-locked: the pod must be created
        in the same region.

        VERIFY: the network-volume REST endpoints against the account's RunPod API.
        """
        import httpx

        region = region or self.region
        with httpx.Client(timeout=30.0, headers=self._rest_headers()) as client:
            existing = client.get(f"{REST_BASE}/networkvolumes")
            existing.raise_for_status()
            for vol in existing.json() or []:
                if isinstance(vol, dict) and vol.get("name") == name:
                    return vol["id"]

            body = {"name": name, "size": size_gb}
            if region:
                body["dataCenterId"] = region  # VERIFY field name
            created = client.post(f"{REST_BASE}/networkvolumes", json=body)
            created.raise_for_status()
            vol = created.json()
            logger.info("Created RunPod network volume %s (%s)", name, vol.get("id"))
            return vol["id"]

    # -------------------------------------------------------------------- pods
    def provision_pod(
        self,
        gpu_type: str,
        image: str,
        name: str,
        volume_id: Optional[str] = None,
        volume_mount_path: str = "/runpod-volume",
        ports: str = "8000/http",
        env: Optional[Dict[str, str]] = None,
        container_disk_gb: int = 20,
        volume_gb: int = 0,
        cloud_type: str = "SECURE",
        registry_auth_id: Optional[str] = None,
    ) -> PodHandle:
        """Create an on-demand GPU pod and return its handle (not yet ready).

        Prefer ``dedicated_pod`` (context manager) so teardown is guaranteed even
        on failure.

        ``registry_auth_id`` is the RunPod container-registry credential id used
        to pull a PRIVATE image (our GHCR image is private). Defaults to the
        RUNPOD_CONTAINER_REGISTRY_AUTH_ID env var.

        The runpod SDK's ``create_pod`` (1.10.0) does not expose
        ``containerRegistryAuthId``, so we build the same GraphQL mutation the SDK
        builds (via its own generator, keeping all formatting correct) and inject
        that one field, then run it through the SDK's GraphQL client.
        """
        self._ensure_key()
        registry_auth_id = registry_auth_id or os.environ.get("RUNPOD_CONTAINER_REGISTRY_AUTH_ID")

        from runpod.api.mutations import pods as _pod_mutations
        from runpod.api import graphql as _runpod_graphql

        gen_kwargs: Dict[str, Any] = {
            "name": name,
            "image_name": image,
            "gpu_type_id": gpu_type,
            "cloud_type": cloud_type,
            "gpu_count": 1,
            "container_disk_in_gb": container_disk_gb,
            "ports": ports,
            "volume_mount_path": volume_mount_path,
            "env": env or {},
        }
        if volume_id:
            gen_kwargs["network_volume_id"] = volume_id
        elif volume_gb:
            gen_kwargs["volume_in_gb"] = volume_gb
        if self.region:
            gen_kwargs["data_center_id"] = self.region

        mutation = _pod_mutations.generate_pod_deployment_mutation(**gen_kwargs)
        if registry_auth_id:
            # Inject the field the SDK generator omits, right after imageName.
            anchor = f'imageName: "{image}"'
            mutation = mutation.replace(
                anchor,
                f'{anchor}\n        containerRegistryAuthId: "{registry_auth_id}"',
                1,
            )

        raw = _runpod_graphql.run_graphql_query(mutation)
        if isinstance(raw, dict) and raw.get("errors"):
            raise RuntimeError(f"RunPod pod create failed: {raw['errors']}")
        pod = raw["data"]["podFindAndDeployOnDemand"]
        pod_id = pod["id"]
        logger.info("Provisioned RunPod pod %s (%s on %s)", name, pod_id, gpu_type)
        return PodHandle(id=pod_id, mode="dedicated", raw=pod)

    def wait_pod_ready(
        self,
        handle: PodHandle,
        internal_port: int = 8000,
        timeout: float = 900.0,
        interval: float = 5.0,
    ) -> PodHandle:
        """Poll until the pod exposes a public port, then set ``endpoint_url``.

        Does not probe the app; use ``LLMInferenceClient.wait_ready`` for that.
        VERIFY: get_pod runtime/ports shape against the pinned SDK.
        """
        self._ensure_key()
        deadline = time.time() + timeout
        while time.time() < deadline:
            pod = _runpod_sdk.get_pod(handle.id)
            runtime = (pod or {}).get("runtime") if isinstance(pod, dict) else None
            ports = (runtime or {}).get("ports") or []
            for p in ports:
                if p.get("privatePort") == internal_port and p.get("ip") and p.get("publicPort"):
                    scheme = "https" if p.get("type") == "https" else "http"
                    handle.endpoint_url = f"{scheme}://{p['ip']}:{p['publicPort']}"
                    handle.raw = pod
                    logger.info("Pod %s ready at %s", handle.id, handle.endpoint_url)
                    return handle
            time.sleep(interval)
        raise TimeoutError(f"Pod {handle.id} did not expose port {internal_port} within {timeout}s")

    def teardown(self, handle: PodHandle) -> None:
        """Terminate the pod. Safe to call more than once. VERIFY: terminate_pod."""
        if handle.mode != "dedicated":
            return
        try:
            self._ensure_key()
            _runpod_sdk.terminate_pod(handle.id)
            logger.info("Terminated RunPod pod %s", handle.id)
        except Exception as e:  # noqa: BLE001 - teardown must never raise past here
            logger.error("Failed to terminate pod %s: %s — CHECK RUNPOD CONSOLE", handle.id, e)

    @contextlib.contextmanager
    def dedicated_pod(self, **kwargs):
        """Context manager: provision a dedicated pod and always tear it down.

        This is the teardown guarantee. The pod is terminated on the way out even
        if the body raises or times out.

            with client.dedicated_pod(gpu_type=..., image=..., name=...) as pod:
                client.wait_pod_ready(pod)
                ... use pod.endpoint_url ...
            # pod terminated here, no matter what
        """
        handle = self.provision_pod(**kwargs)
        try:
            yield handle
        finally:
            self.teardown(handle)

    # -------------------------------------------------------------- serverless
    def run_serverless(
        self,
        endpoint_id: str,
        payload: Dict[str, Any],
        sync: bool = True,
        timeout: int = 600,
    ) -> Dict[str, Any]:
        """Invoke a pre-deployed RunPod Serverless endpoint (scales to zero itself).

        VERIFY: Endpoint.run_sync / run signature against the pinned SDK.
        """
        self._ensure_key()
        endpoint = _runpod_sdk.Endpoint(endpoint_id)
        if sync:
            return endpoint.run_sync(payload, timeout=timeout)
        job = endpoint.run(payload)
        return {"id": job.job_id, "status": job.status()}


# Global instance
_runpod_client: Optional[RunPodClient] = None


def get_runpod_client() -> RunPodClient:
    """Get the global RunPod client instance."""
    global _runpod_client
    if _runpod_client is None:
        _runpod_client = RunPodClient()
    return _runpod_client
