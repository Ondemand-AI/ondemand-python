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


class GpuCapacityUnavailable(RuntimeError):
    """Raised when no GPU matching the spec could be provisioned right now.

    Distinct from other errors so the caller can wait and retry (capacity is
    transient) rather than fail hard.
    """


def _is_no_capacity(err: Exception) -> bool:
    """True if a provision error is RunPod signalling no available instances."""
    msg = str(err).lower()
    return (
        "no longer any instances" in msg
        or "no instances" in msg
        or "not enough" in msg
        or "instances available" in msg
    )


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
    gpu_type_id: Optional[str] = None
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
        allowed_cuda_versions: Optional[list] = None,
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

        # The SDK's mutation generator interpolates env values into the GraphQL
        # string WITHOUT escaping (env: [{ key: "K", value: "V" }]). A value with
        # a double quote (e.g. HYPERPARAMS_JSON, which is JSON) breaks the query
        # ("Syntax Error: Expected ':'"). Escape backslash, quote and newlines so
        # any value survives the string literal.
        def _esc(v: Any) -> str:
            return (
                str(v)
                .replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\r", "")
            )

        safe_env = {k: _esc(v) for k, v in (env or {}).items()}

        gen_kwargs: Dict[str, Any] = {
            "name": name,
            "image_name": image,
            "gpu_type_id": gpu_type,
            "cloud_type": cloud_type,
            "gpu_count": 1,
            "container_disk_in_gb": container_disk_gb,
            "ports": ports,
            "volume_mount_path": volume_mount_path,
            "env": safe_env,
        }
        if volume_id:
            gen_kwargs["network_volume_id"] = volume_id
        elif volume_gb:
            gen_kwargs["volume_in_gb"] = volume_gb
        if self.region:
            gen_kwargs["data_center_id"] = self.region

        mutation = _pod_mutations.generate_pod_deployment_mutation(**gen_kwargs)
        anchor = f'imageName: "{image}"'
        if registry_auth_id:
            # Inject the field the SDK generator omits, right after imageName.
            mutation = mutation.replace(
                anchor,
                f'{anchor}\n        containerRegistryAuthId: "{registry_auth_id}"',
                1,
            )
        if allowed_cuda_versions:
            # Restrict to hosts whose driver supports one of these CUDA versions,
            # so the selector never lands on a box with a driver too old for our
            # image (the "NVIDIA driver too old / cannot find any torch accelerator"
            # crash on some Community hosts). VERIFY: RunPod's create-pod input is
            # version-sensitive; confirm the `allowedCudaVersions` field + accepted
            # version strings on a real run before relying on it as a default.
            versions = ", ".join(f'"{v}"' for v in allowed_cuda_versions)
            mutation = mutation.replace(
                anchor, f'{anchor}\n        allowedCudaVersions: [{versions}]', 1
            )

        raw = _runpod_graphql.run_graphql_query(mutation)
        if isinstance(raw, dict) and raw.get("errors"):
            raise RuntimeError(f"RunPod pod create failed: {raw['errors']}")
        pod = raw["data"]["podFindAndDeployOnDemand"]
        pod_id = pod["id"]
        logger.info("Provisioned RunPod pod %s (%s on %s)", name, pod_id, gpu_type)
        return PodHandle(id=pod_id, mode="dedicated", gpu_type_id=gpu_type, raw=pod)

    def wait_pod_ready(
        self,
        handle: PodHandle,
        internal_port: int = 8000,
        timeout: float = 900.0,
        interval: float = 5.0,
        heartbeat=None,
    ) -> PodHandle:
        """Poll until the pod exposes a public port, then set ``endpoint_url``.

        Does not probe the app; use ``LLMInferenceClient.wait_ready`` for that.
        Pass ``heartbeat`` (e.g. ``activity.heartbeat``) — a pod boot / image pull
        can exceed a Temporal activity heartbeat timeout, so we ping each poll.
        VERIFY: get_pod runtime/ports shape against the pinned SDK.
        """
        self._ensure_key()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if heartbeat:
                try:
                    heartbeat("waiting for pod to expose port")
                except Exception:
                    pass
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

    def pod_alive(self, handle: PodHandle) -> bool:
        """Best-effort liveness: False if the pod has exited/terminated/failed.

        On a query error we assume alive — a transient API blip must not kill a
        running job. A pod that exits normally at end of training also reads as
        not-alive, so callers must check the success marker BEFORE trusting this.
        """
        try:
            self._ensure_key()
            pod = _runpod_sdk.get_pod(handle.id)
        except Exception:
            return True
        if not isinstance(pod, dict) or not pod:
            return False
        return pod.get("desiredStatus") not in ("EXITED", "TERMINATED", "FAILED")

    # ------------------------------------------------------------- selection
    _CLOUD_PRICE_FIELD = {"SECURE": "securePrice", "COMMUNITY": "communityPrice"}
    _CLOUD_AVAIL_FIELD = {"SECURE": "secureCloud", "COMMUNITY": "communityCloud"}

    def list_gpu_candidates(
        self,
        min_vram_gb: int,
        cloud_types=("SECURE", "COMMUNITY"),
        exclude_gpu_types=(),
    ) -> list:
        """RunPod GPU types with >= min_vram, cheapest on-demand price first.

        Returns [{gpu_type_id, cloud_type, price, name, vram}] sorted by price.
        Actual stock is only known at deploy time, so this ranks by price and the
        caller falls back to the next candidate on a no-capacity error.

        ``exclude_gpu_types`` drops GPU type ids that already failed for a
        host/driver reason this run, so a retry picks a different GPU instead of
        the same bad one.
        """
        exclude = set(exclude_gpu_types or ())
        self._ensure_key()
        from runpod.api import graphql as _runpod_graphql

        query = """
        query {
          gpuTypes {
            id
            displayName
            memoryInGb
            secureCloud
            communityCloud
            securePrice
            communityPrice
          }
        }
        """
        raw = _runpod_graphql.run_graphql_query(query)
        if isinstance(raw, dict) and raw.get("errors"):
            raise RuntimeError(f"RunPod gpuTypes query failed: {raw['errors']}")

        candidates = []
        for g in (raw["data"]["gpuTypes"] or []):
            vram = g.get("memoryInGb") or 0
            if vram < min_vram_gb:
                continue
            if g["id"] in exclude:
                continue
            for cloud in cloud_types:
                if not g.get(self._CLOUD_AVAIL_FIELD[cloud]):
                    continue
                price = g.get(self._CLOUD_PRICE_FIELD[cloud])
                if price is None or price <= 0:
                    continue
                candidates.append({
                    "gpu_type_id": g["id"],
                    "cloud_type": cloud,
                    "price": price,
                    "name": g.get("displayName"),
                    "vram": vram,
                })
        candidates.sort(key=lambda c: c["price"])
        return candidates

    def provision_cheapest(
        self,
        image: str,
        name: str,
        min_vram_gb: int,
        cloud_types=("SECURE", "COMMUNITY"),
        env: Optional[Dict[str, str]] = None,
        volume_id: Optional[str] = None,
        container_disk_gb: int = 20,
        ports: str = "8000/http",
        volume_mount_path: str = "/runpod-volume",
        exclude_gpu_types=(),
        allowed_cuda_versions: Optional[list] = None,
    ) -> PodHandle:
        """Provision the cheapest available GPU meeting min_vram, falling back on
        capacity. Raises GpuCapacityUnavailable if every candidate is out now."""
        candidates = self.list_gpu_candidates(min_vram_gb, cloud_types, exclude_gpu_types)
        if not candidates:
            raise GpuCapacityUnavailable(
                f"No GPU type offered with >= {min_vram_gb}GB in {list(cloud_types)}"
                + (f" (excluding {set(exclude_gpu_types)})" if exclude_gpu_types else "")
            )
        last_err = None
        for c in candidates:
            try:
                logger.info(
                    "Trying %s (%s, %sGB, $%.3f/hr)",
                    c["name"], c["cloud_type"], c["vram"], c["price"],
                )
                return self.provision_pod(
                    gpu_type=c["gpu_type_id"], image=image, name=name,
                    cloud_type=c["cloud_type"], env=env, volume_id=volume_id,
                    container_disk_gb=container_disk_gb, ports=ports,
                    volume_mount_path=volume_mount_path,
                    allowed_cuda_versions=allowed_cuda_versions,
                )
            except Exception as e:  # noqa: BLE001
                if _is_no_capacity(e):
                    last_err = e
                    continue
                raise
        raise GpuCapacityUnavailable(
            f"All {len(candidates)} candidate GPUs out of stock (last: {last_err})"
        )

    def provision_with_wait(
        self,
        image: str,
        name: str,
        min_vram_gb: int,
        cloud_types=("SECURE", "COMMUNITY"),
        env: Optional[Dict[str, str]] = None,
        volume_id: Optional[str] = None,
        container_disk_gb: int = 20,
        ports: str = "8000/http",
        volume_mount_path: str = "/runpod-volume",
        max_wait_seconds: int = 1800,
        first_interval: int = 30,
        max_interval: int = 60,
        heartbeat=None,
        exclude_gpu_types=(),
        allowed_cuda_versions: Optional[list] = None,
    ) -> PodHandle:
        """``provision_cheapest`` but wait out a total shortage with backoff.

        Interval ramps first_interval (30s) -> capped at max_interval (60s), for
        up to max_wait_seconds (30min). An opening is picked up on the next poll,
        so within <= max_interval. ``heartbeat`` is called on each wait so a
        Temporal activity does not hit its heartbeat timeout.
        """
        deadline = time.time() + max_wait_seconds
        interval = first_interval
        while True:
            try:
                return self.provision_cheapest(
                    image=image, name=name, min_vram_gb=min_vram_gb,
                    cloud_types=cloud_types, env=env, volume_id=volume_id,
                    container_disk_gb=container_disk_gb, ports=ports,
                    volume_mount_path=volume_mount_path,
                    exclude_gpu_types=exclude_gpu_types,
                    allowed_cuda_versions=allowed_cuda_versions,
                )
            except GpuCapacityUnavailable as e:
                if time.time() >= deadline:
                    raise
                logger.warning("No GPU available; retrying in %ss (%s)", interval, e)
                if heartbeat:
                    try:
                        heartbeat("waiting for GPU capacity")
                    except Exception:
                        pass
                time.sleep(interval)
                interval = min(interval * 2, max_interval)

    @contextlib.contextmanager
    def dedicated_pod(
        self,
        image: str,
        name: str,
        min_vram_gb: int = 16,
        cloud_types=("SECURE", "COMMUNITY"),
        env: Optional[Dict[str, str]] = None,
        volume_id: Optional[str] = None,
        container_disk_gb: int = 20,
        ports: str = "8000/http",
        volume_mount_path: str = "/runpod-volume",
        max_wait_seconds: int = 1800,
        heartbeat=None,
        gpu_type: Optional[str] = None,
        exclude_gpu_types=(),
        allowed_cuda_versions: Optional[list] = None,
    ):
        """Provision the cheapest available GPU (waiting out shortages) and ALWAYS
        tear it down — the teardown guarantee, even if the body raises/times out.

        Pass ``gpu_type`` to pin one specific GPU instead of the selector.
        ``exclude_gpu_types`` skips GPU types that failed for a host reason;
        ``allowed_cuda_versions`` restricts to hosts with a new-enough driver.

            with client.dedicated_pod(image=..., name=..., min_vram_gb=16) as pod:
                ... use pod ...
            # pod terminated here, no matter what
        """
        if gpu_type:
            handle = self.provision_pod(
                gpu_type=gpu_type, image=image, name=name,
                cloud_type=(cloud_types[0] if cloud_types else "SECURE"),
                env=env, volume_id=volume_id, container_disk_gb=container_disk_gb,
                ports=ports, volume_mount_path=volume_mount_path,
                allowed_cuda_versions=allowed_cuda_versions,
            )
        else:
            handle = self.provision_with_wait(
                image=image, name=name, min_vram_gb=min_vram_gb,
                cloud_types=cloud_types, env=env, volume_id=volume_id,
                container_disk_gb=container_disk_gb, ports=ports,
                volume_mount_path=volume_mount_path,
                max_wait_seconds=max_wait_seconds, heartbeat=heartbeat,
                exclude_gpu_types=exclude_gpu_types,
                allowed_cuda_versions=allowed_cuda_versions,
            )
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

    def serverless_health(self, endpoint_id: str, timeout: float = 15.0) -> Dict[str, Any]:
        """Preflight an endpoint; raise LOUD if the id is missing/invalid/unreachable.

        Fails fast (one clear error) instead of letting every job silently error.
        RunPod endpoints are account-scoped, so the account key authorizes any.
        """
        import httpx
        if not endpoint_id:
            raise RunPodKeyMissingError("serverless endpoint id is empty")
        url = f"https://api.runpod.ai/v2/{endpoint_id}/health"
        try:
            r = httpx.get(url, headers=self._rest_headers(), timeout=timeout)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"serverless endpoint '{endpoint_id}' unreachable: {e}") from e
        if r.status_code != 200:
            raise RuntimeError(
                f"serverless endpoint '{endpoint_id}' invalid/forbidden "
                f"(HTTP {r.status_code}: {(r.text or '')[:200]})"
            )
        return r.json()

    def serverless_run(
        self,
        endpoint_id: str,
        payload: Dict[str, Any],
        timeout: float = 600.0,
        poll_interval: float = 1.0,
        heartbeat=None,
    ) -> Any:
        """Submit a job ASYNC and poll to completion, pinging ``heartbeat`` each poll.

        ``run_sync`` blocks with no heartbeat, so a cold start (worker boot + model
        load) exceeds a Temporal activity heartbeat timeout, the activity is retried
        mid-flight, and orphan RunPod jobs pile up. This polls with a heartbeat and
        cancels the job on timeout. Returns the job output; raises on failure/timeout
        (the caller decides whether one failure should sink a whole batch).
        """
        self._ensure_key()
        endpoint = _runpod_sdk.Endpoint(endpoint_id)
        job = endpoint.run(payload)
        deadline = time.time() + timeout
        terminal = ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT")
        st = None
        while True:
            st = job.status()
            if st in terminal:
                break
            if time.time() > deadline:
                try:
                    job.cancel()
                except Exception:  # noqa: BLE001
                    pass
                raise TimeoutError(f"serverless job timed out after {timeout}s (status {st})")
            if heartbeat:
                try:
                    heartbeat(f"serverless {st}")
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(poll_interval)
        if st != "COMPLETED":
            raise RuntimeError(f"serverless job ended {st}")
        return job.output()


def resolve_serverless_endpoint(base_model: str) -> str:
    """Endpoint id for a base model, from the ``RUNPOD_SERVERLESS_ENDPOINTS`` env
    (JSON map ``{"<base_model>": "<endpoint_id>"}``, from the org-wide
    ondemand-shared secret). One endpoint per base, shared across automations.
    Returns "" when unset or the base is not mapped.
    """
    import json
    raw = (os.environ.get("RUNPOD_SERVERLESS_ENDPOINTS") or "").strip()
    if not raw:
        return ""
    try:
        m = json.loads(raw)
    except Exception:  # noqa: BLE001
        return ""
    return str((m.get(base_model) if isinstance(m, dict) else "") or "")


# Global instance
_runpod_client: Optional[RunPodClient] = None


def get_runpod_client() -> RunPodClient:
    """Get the global RunPod client instance."""
    global _runpod_client
    if _runpod_client is None:
        _runpod_client = RunPodClient()
    return _runpod_client
