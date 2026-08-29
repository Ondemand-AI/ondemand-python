# Changelog

## [1.10.1] - 2026-08-28

### Changed

A failing Workflow Task now exports at ERROR instead of WARNING.

temporalio logs `Failed activation on workflow ...` at WARNING, tagged
`__temporal_error_identifier: WorkflowTaskFailure`. That level is defensible
from its side — the task is retried forever and a corrected deploy resolves it.
From ours it is not: the worker could not process the task at all (arguments
that will not deserialize, a crash before user code, a non-determinism error),
the workflow stays RUNNING, and the portal reads "executando" for a run that
never started — until the 31-day execution timeout.

Matched on the identifier rather than the message text, which is an f-string
that can change between SDK releases. `ActivityFailure` is deliberately left at
its own level: activities have a RetryPolicy, transient failures there are
normal, and promoting them would fill the error feed with noise that resolves
itself.

## [1.10.0] - 2026-08-28

### Added

`ondemand.worker.WorkflowInput` — the workflow input contract, in one place.

Every robot used to redeclare the portal's payload by hand, and they had
drifted: `ondemand-auto-demo` declared a `webhook_secret` the portal never sends
(`""` since day one, unnoticed) and ignored `process_code` and
`organization_id`, while `GSE1-conciliacao-de-contas` declared those two and not
the secret. Two robots, two contracts for the same payload.

The cost surfaced with the 1.9.0 rename: a change that belongs to the platform
had to be made in every automation repo. Now it does not — robots upgrade this
library at container start, so a future contract change reaches them without a
rebuild.

```python
from dataclasses import dataclass
from ondemand.worker import WorkflowInput

@dataclass
class MyInput(WorkflowInput):
    @property
    def period(self) -> str:
        return self.inputs.get("period", "")
```

Temporal needs a concrete type to deserialize into, so a robot still declares
and inherits. What goes away is the duplication of the platform's own fields.

Every field carries a default, `workflow_id` included: Python 3.9 has no
`kw_only` for dataclasses, and a field without a default cannot follow one that
has it. Give your own fields defaults too.

## [1.9.0] - 2026-08-28

### Changed — BREAKING

Adopted Temporal's vocabulary for the two identifiers of a Workflow Execution.
Temporal identifies one by a **pair**: the Workflow ID (the business identifier
the caller chooses — for us `process_runs.id`) and the Run ID (a
server-generated UUID for ONE execution of it, new on retry, continue-as-new,
cron tick or reset). The library called the first one "run id" and had no name
for the second, which collided with Temporal's own `TemporalWorkflowID` /
`TemporalRunID` span attributes and made a run impossible to follow in HyperDX.

- `current_run_id()` → `current_workflow_id()`
- `set_run_id()` / `get_run_id()` → `set_workflow_id()` / `get_workflow_id()`
- `ONDEMAND_RUN_ID` → `ONDEMAND_WORKFLOW_ID`
- `download_input_files(..., run_id=)` → `workflow_id=`
- Robot workflow input field `run_id` → `workflow_id`
- Log attributes exported to HyperDX are now `TemporalWorkflowID` and
  `TemporalRunID`, matching what Temporal's interceptor writes on spans

### Added

- `current_temporal_run_id()` — the Run ID of the current execution.
- R2 artifact keys are partitioned by attempt:
  `artifacts/{workflow_id}/{temporal_run_id}/…` when the Run ID is known. A
  workflow retry writes the same filenames, and without the segment the second
  attempt would overwrite the first one's artifacts. Listing is by prefix and
  therefore recursive, so the portal keeps seeing everything under a workflow;
  objects written before this change are unaffected.

### Migration

No fallback is provided — every robot must move together. Rename the workflow
input field in the robot's dataclass and any `input.run_id` reads. Requires
ondemand-obs >= 0.1.8 and the portal API from ondemand-infra 2026-08-28.

All notable changes to the `ondemand-ai` package will be documented in this file.

## [1.8.0] - 2026-08-21

### Added
- `ondemand.__version__`, read from installed distribution metadata rather than
  hardcoded, so it reflects a runtime upgrade rather than what was baked into the
  image at build time.
- The worker startup log now reports the library version actually loaded.
- Via `ondemand-obs>=0.1.7`, every span, metric and log carries
  `ondemand.ai.version` and `ondemand.obs.version` as resource attributes.

  This exists to support robots upgrading the library at container start: once
  that is enabled the image tag no longer implies which library version is
  running, and telemetry becomes the only reliable way to answer "which robots
  are on which version?".

## [1.7.0] - 2026-08-21

### Fixed
- **Concurrent runs on one pod could overwrite each other's run context.** The
  activity interceptor publishes `ONDEMAND_RUN_ID` / `ONDEMAND_WEBHOOK_URL` into
  `os.environ`, which is process-global, while a worker executes up to
  `MAX_CONCURRENT_ACTIVITIES` activities at once in a `ThreadPoolExecutor`. Two
  activities from different runs therefore raced, and whichever wrote last won for
  both — sending artifacts to `artifacts/{wrong_run_id}/` and posting step reports,
  approvals, artifact notifications and log streams against the wrong run.

  Every in-process consumer now resolves the run through the new
  `ondemand.shared.run_context`, which reads temporalio's per-task activity
  contextvar and falls back to the environment only outside an activity. Verified
  with two concurrent activities where the environment variable held the other
  run's id throughout.

  The interceptor still publishes both variables, for subprocesses that inherit
  the environment and for code running outside an activity, but they are no longer
  the in-process source of truth.

### Added
- `ondemand.shared.run_context` with `current_run_id()` and
  `current_webhook_url()`. Prefer these over reading the environment directly.
  `current_webhook_url()` composes the URL from `ONDEMAND_APP_URL` (static per
  deployment, so not subject to the race) plus the resolved run id.

## [1.6.0] - 2026-08-20

### Fixed
- **Log lines were attributed to the logging module, not the robot.** Every record
  emitted through `logger.success()`, `.section()`, `.step()`, `.divider()`,
  `.summary()` and `.timed()` reported `ondemand/shared/logging.py` as its source,
  because Python's `findCaller` stops at the first frame outside the *stdlib*
  logging module — which was our wrapper. Those helpers now pass an explicit
  `stacklevel`, so `code.file.path` / `code.line.number` / `code.function.name`
  (the `code.*` attributes OpenTelemetry exports to HyperDX) point at the robot.
  The required offset changed in Python 3.11, so it is resolved from
  `sys.version_info` and verified on 3.9, 3.13 and 3.14.

### Added
- Records exported to HyperDX are now tagged with the Temporal run context:
  `run_id`, `activity_name`, `attempt` and `task_queue`. HyperDX can filter a
  single execution with `run_id:"<uuid>"`. The run id is read from temporalio's
  activity contextvar rather than `ONDEMAND_RUN_ID`, because that env var is
  process-global while a worker runs up to `MAX_CONCURRENT_ACTIVITIES` activities
  in parallel threads — concurrent runs on one pod overwrite each other's value.
  The env var remains the fallback outside an activity.

### Changed
- `Logger.success()` had two independent definitions — `OndemandLogger.success` in
  `shared/logging.py` and a `logging.Logger` monkeypatch in `worker/logging.py` —
  both carrying the same attribution bug. There is now one definition, in
  `shared/logging.py`, patched onto the base `Logger` class so it remains available
  on loggers created before `OndemandLogger` was registered (the common
  `logging.getLogger("demo")` pattern in robots). `worker/logging.py` re-exports
  `SUCCESS` for compatibility.
- Requires `ondemand-obs>=0.1.6`, which exports SUCCESS records as OTel severity
  `INFO` plus an `ondemand.outcome=success` attribute, so HyperDX's level filter
  recognises them.

## [1.5.3] - 2026-08-20

### Security
- Every sender to the supervisor webhook now authenticates. `request_approval` was
  the only one sending `Authorization: Bearer`; step reports
  (`worker/activity_reporter.py`), log streaming (`worker/logging.py`) and artifact
  notifications (`shared/r2_storage.py`) sent no credentials at all, so the API
  could not enforce auth without breaking three of the four callers.
- Headers are now built in one place, `ondemand.shared.webhook_auth.webhook_headers()`,
  reading a single environment variable: `ONDEMAND_WEBHOOK_SECRET`.

### Changed
- **Renamed:** the worker previously read `SUPERVISOR_WEBHOOK_SECRET` into a config
  field that nothing ever used. That field is removed and the variable is gone; set
  `ONDEMAND_WEBHOOK_SECRET` instead. The differing names on each side of the wire are
  why the secret stayed unset everywhere.
- The Authorization header is omitted when no secret is set, so local runs and
  environments that have not yet enabled auth keep working.

## [1.5.2] - 2026-05-05

### Added
- `ondemand.shared.bitwarden` — `bw_connect()` and `bw_get_item()` wrapper over `t_vault`; robots should import from here instead of `t_vault` directly
- `bw_connect()` suppresses urllib3 connection-refused warnings during Bitwarden CLI server startup (transient retries that always resolve)

## [1.5.0] - 2026-04-28

### Added
- OpenTelemetry observability via `ondemand-obs` package — traces, metrics, and logs sent to HyperDX when `HYPERDX_API_KEY` is set
- `TracingInterceptor` automatically added to Temporal worker (Temporal workflow + activity spans)
- OTLP log handler attached to root logger as sibling to `OndemandLogHandler` (dual-emit — webhook path unchanged)
- All httpx and requests calls auto-instrumented; webhook `traceparent` headers propagate traces end-to-end into the API

## [1.4.9] - 2026-04-08

### Added
- `ActivityReporter` — sends step updates directly to portal via webhook (STEP_REPORT) from inside activities for real-time SSE updates
- `report` singleton: `report.step_started()`, `report.step_completed()`, `report.step_failed()`, `report.record()`

## [1.4.8] - 2026-04-07

### Fixed
- Log handler excludes `httpx`, `httpcore`, `urllib3`, `temporalio` loggers (prevents feedback loops)
- Idle watchdog checks if activities are running before shutting down worker

## [1.4.7] - 2026-04-07

### Added
- Activity interceptor auto-sets `ONDEMAND_RUN_ID` and `ONDEMAND_WEBHOOK_URL` before each activity — robots no longer need to set them manually
- `ONDEMAND_RUN_ID` derived from `activity.info().workflow_id`, `ONDEMAND_WEBHOOK_URL` derived from `ONDEMAND_APP_URL` env var

### Fixed
- `upload_content()` now logs a warning (not debug) when skipping due to missing run_id

## [1.4.6] - 2026-04-07

### Fixed
- Log handler flushes every 3 lines OR every 2 seconds (whichever comes first) — logs stream to portal even for small activities
- Timer-based flush ensures no log lines are stuck in the buffer

## [1.4.5] - 2026-04-07

### Changed
- Step lifecycle logs use custom levels: `STARTED`, `COMPLETED`, `FAILED`, `WARNING`, `SKIPPED` instead of generic `INFO`/`SUCCESS`/`ERROR`
- Step name is used as the module in lifecycle log lines (e.g. `Inicialização - STARTED -`)

## [1.4.4] - 2026-04-07

### Fixed
- `OndemandLogHandler` reads `ONDEMAND_WEBHOOK_URL` lazily on flush (not on init) since it's set by the first activity at runtime
- Added `SUCCESS` log level (`logger.success(...)`) — maps to `SUCCESS` in log format for portal green highlighting

## [1.4.3] - 2026-04-07

### Added
- `OndemandLogHandler` — Python logging handler that captures all log output for portal display (via LOG_STREAM webhook) and R2 upload
- `setup_logging()`, `get_collected_logs()`, `get_and_clear_logs()` — helper functions for log collection
- Auto-setup: `OndemandWorker` calls `setup_logging()` on startup — no manual setup needed

## [1.4.2] - 2026-04-07

### Changed
- `upload_content()` and `notify_artifacts_uploaded()`: `run_id` is read automatically from `ONDEMAND_RUN_ID` env var — developers never pass it
- Local runs (no `ONDEMAND_RUN_ID`) skip uploads silently instead of crashing

## [1.4.1] - 2026-04-07

### Fixed
- `upload_content()` and `notify_artifacts_uploaded()`: `run_id` is now a required parameter (not optional) — prevents uploading to wrong paths

## [1.4.0] - 2026-04-07

### Added
- `R2StorageClient.upload_content()` — upload raw bytes to R2 with optional webhook notification to the portal
- `notify_artifacts_uploaded()` — POST artifact metadata to the portal webhook (ARTIFACTS_UPLOADED action) so artifacts appear in the UI immediately via SSE
- `WorkflowReporter.apply_updates()` now supports `records` array embedded in step status updates (e.g. `{step_id, status, records: [...]}`)

### Changed
- Artifact uploads can now notify the portal directly instead of routing through the workflow reporter → temporalSync → DB pipeline
- The old `{"record": {...}}` format in `apply_updates()` still works (backward compatible)

## [1.3.1] - 2026-03-31

### Fixed
- `WorkflowReporter.apply_updates()` now preserves real timestamps from activity execution instead of overwriting with workflow-side timestamps

## [1.3.0] - 2026-03-31

### Added
- `WorkflowReporter` — Temporal-native step tree management replacing the old webhook-based reporting system
  - `add_step()`, `start_step()`, `complete_step()`, `fail_step()`, `warn_step()`, `skip_step()` for step lifecycle
  - `add_record()` for per-item results within steps
  - `log()` for structured log lines in standard format
  - `add_artifact()` for registering R2-uploaded files
  - `apply_updates()` for batch-applying updates returned by activities
  - `to_dict()` for exporting state to `@workflow.query`
- `WorkflowReporter` step tree supports nested steps via `parent` parameter
- Log format: `timestamp - module - LEVEL - message` (module defaults to current step title)

### Changed
- Step progress is now queryable via Temporal Query API instead of webhooks

## [1.2.0] - 2026-03-30

### Added
- `OndemandWorker` — base class for Cloud Run automation workers
  - Connects to Temporal, registers workflows/activities, polls task queue
  - `@worker.activity` and `@worker.workflow` decorators for registration
  - `register_activity()` and `register_workflow()` for explicit registration
  - `worker.run()` as the main entrypoint (blocking, runs asyncio loop)
  - Idle timeout: exits after configurable seconds with no work (saves Cloud Run costs)
  - Captures stdout/stderr via `TeeStream` for console log upload
  - Graceful shutdown on SIGINT/SIGTERM

### Changed
- Package now uses `[worker]` optional dependency for `temporalio` (not required for shared utilities)

## [1.1.0] - 2026-03-28

### Added
- `R2StorageClient.copy_object()` for copying objects within the same bucket
- `upload_root_artifacts()` for uploading shared files from the run's base output directory, with `skip_subdirs` parameter to avoid re-uploading task directories
- `download_input_files()` now copies downloaded files to `artifacts/{run_id}/inputs/` for portal visibility
- Support for `scheduled-inputs/` prefix in addition to `inputs/` for scheduled workflow file downloads
- `upload_task_artifacts()` `exclude` parameter to skip specific filenames (e.g., `console.txt`)

### Changed
- `download_input_files()` returns `Path` for single files and `List[Path]` for multi-file inputs

## [1.0.5] - 2026-03-24

### Fixed
- Step title mapping broken for nested manifest steps — `_build_title_map` referenced non-existent `OndemandConnector` instead of `OndemandStreamer`, causing a silent `NameError` that prevented child step titles from being cached

## [1.0.4] - 2026-03-24

### Added
- `get_run_info()` returns `RunInfo` dataclass with run_id, process_code, organization_id, started_at
- `ONDEMAND_ORGANIZATION_ID` env var support (set by worker)

## [1.0.3] - 2026-03-24

### Added
- Step reports now display manifest `title` instead of `step_id` when available
- Manifest title mapping cached on manifest send for O(1) lookup

### Fixed
- Step names in portal showing internal IDs (e.g., "BB Parsing") instead of user-friendly titles (e.g., "Extracao de Transacoes")

## [1.0.2] - 2026-03-23

### Fixed
- `shutil.move` race condition when concurrent RCC runs share the same holotree environment
- Patched globally at import time before `t_vault` loads, preventing `Bitwarden()` singleton crash

## [1.0.1] - 2026-03-20

### Fixed
- Attempted fix for `t_vault` Bitwarden CLI install race condition (incomplete — replaced by 1.0.2)

## [1.0.0] - 2026-03-17

### Added
- Initial PyPI release
- `@supervised_step` decorator for step tracking and reporting
- `step_scope` context manager for dynamic sub-steps
- `request_approval()` for HITL human-in-the-loop workflows
- `save_artifact` / `load_artifact` for inter-task state management
- `update_manifest` / `build_manifest_step` for dynamic workflow manifests
- R2 storage integration (`download_input_files`, `upload_task_artifacts`)
- `get_inputs()` CLI argument parser with `ONDEMAND_INPUTS` env var support
- Webhook-based step reporting to Ondemand portal
- Git version tracking in step reports
