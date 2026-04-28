# Changelog

All notable changes to the `ondemand-ai` package will be documented in this file.

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
