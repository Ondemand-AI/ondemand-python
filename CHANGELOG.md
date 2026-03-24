# Changelog

All notable changes to the `ondemand-ai` package will be documented in this file.

## [1.0.4] - 2026-03-24

### Added
- `get_run_info()` returns `RunInfo` dataclass with run_id, process_code, organization_id, started_at
- `ONDEMAND_ORGANIZATION_ID` env var support (set by worker)

## [1.0.3] - 2026-03-24

### Added
- Step reports now display manifest `title` instead of `step_id` when available
- Manifest title mapping cached on manifest send for O(1) lookup

### Fixed
- Step names in portal showing internal IDs (e.g. "BB Parsing") instead of user-friendly titles (e.g. "Extração de Transações")

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
