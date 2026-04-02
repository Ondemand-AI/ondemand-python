# ondemand-ai

Python SDK for building automations on the [Ondemand](https://ondemand-ai.com.br) platform.

Provides `OndemandWorker` (a Temporal worker wrapper for Cloud Run Jobs), `WorkflowReporter` (step tree management queryable via the Temporal Query API), structured logging, R2 artifact storage, and human-in-the-loop approval helpers.

[![PyPI](https://img.shields.io/pypi/v/ondemand-ai)](https://pypi.org/project/ondemand-ai/)
[![Python](https://img.shields.io/pypi/pyversions/ondemand-ai)](https://pypi.org/project/ondemand-ai/)
[![License](https://img.shields.io/pypi/l/ondemand-ai)](https://github.com/Ondemand-AI/ondemand-python/blob/main/LICENSE)

## Installation

```bash
# Full install with Temporal worker support
pip install ondemand-ai[worker]

# Shared utilities only (logging, R2 storage, artifacts, approvals)
pip install ondemand-ai
```

**Requirements:** Python 3.9+

## Quick Start

A minimal automation with one workflow and one activity:

```python
# workflows.py
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from activities import process_data

from ondemand.worker import WorkflowReporter


@workflow.defn
class MyWorkflow:
    def __init__(self):
        self.reporter = WorkflowReporter()

    @workflow.query
    def get_progress(self) -> dict:
        return self.reporter.to_dict()

    @workflow.run
    async def run(self, inputs: dict) -> dict:
        # Define the step tree
        self.reporter.add_step("extract", "Extrair Dados")
        self.reporter.add_step("validate", "Validar Dados", parent="extract")

        # Execute activity
        self.reporter.start_step("extract")
        result = await workflow.execute_activity(
            process_data,
            inputs,
            start_to_close_timeout=timedelta(minutes=30),
        )

        # Apply updates returned by the activity
        self.reporter.apply_updates(result.get("step_updates", []))
        self.reporter.complete_step("extract")

        return result
```

```python
# activities.py
from temporalio import activity
from ondemand.shared import get_logger

logger = get_logger(__name__)


@activity.defn
async def process_data(inputs: dict) -> dict:
    logger.section("Processing Data")

    with logger.timed("Loading files"):
        data = load(inputs["file"])

    logger.success(f"Processed {len(data)} records")

    return {
        "count": len(data),
        "step_updates": [
            {"step_id": "validate", "status": "completed"},
            {"record": {"step_id": "extract", "id": "file1.pdf", "status": "success", "message": "OK"}},
            {"log": "All records validated"},
        ],
    }
```

```python
# main.py
from ondemand.worker import OndemandWorker
from workflows import MyWorkflow
from activities import process_data

worker = OndemandWorker(name="my-automation")
worker.register_workflow(MyWorkflow)
worker.register_activity(process_data)

if __name__ == "__main__":
    worker.run()
```

## Modules

### `ondemand.worker.OndemandWorker`

Connects to Temporal, registers workflows and activities, polls a task queue, and shuts down after an idle timeout (Cloud Run Jobs pay per second).

```python
from ondemand.worker import OndemandWorker

worker = OndemandWorker(name="my-worker")

# Register via decorators
@worker.workflow
class MyWorkflow: ...

@worker.activity
async def my_activity(inputs: dict) -> dict: ...

# Or register explicitly
worker.register_workflow(MyWorkflow)
worker.register_activity(my_activity)

# Start polling (blocking call, runs asyncio event loop)
worker.run()
```

**Behavior:**
- Reads configuration from environment variables (see below)
- Captures all stdout/stderr for console log upload
- Exits gracefully on SIGINT/SIGTERM
- Exits after `WORKER_IDLE_TIMEOUT` seconds with no work (default: 300s)

### `ondemand.worker.WorkflowReporter`

Manages a step tree with records, logs, and artifacts. Lives inside the workflow class and is exposed to the portal via `@workflow.query`.

#### Step Management

```python
reporter = WorkflowReporter()

# Build the step tree
reporter.add_step("extract", "Extrair Dados")
reporter.add_step("parse", "Parsear Arquivos", parent="extract")
reporter.add_step("classify", "Classificar")

# Track progress
reporter.start_step("extract")      # logs "▶ Extrair Dados" at INFO
reporter.complete_step("extract")    # logs "✓ Extrair Dados" at SUCCESS
reporter.fail_step("classify", "Timeout na API")  # logs "✗ Classificar: Timeout na API" at ERROR
reporter.warn_step("parse")         # marks step as completed with warnings
reporter.skip_step("classify")      # marks step as skipped
```

**Step statuses:** `pending`, `running`, `completed`, `failed`, `warning`, `skipped`

#### Records

Attach individual item results to a step (e.g., one file processed, one transaction classified):

```python
reporter.add_record(
    step_id="extract",
    record_id="invoice_001.pdf",
    status="success",        # "success", "warning", "failed"
    message="Processado OK",
    metadata={"pages": 3, "total": 1500.00},
)
```

#### Logs

```python
reporter.log("Downloading 42 files...", level="INFO", module="Downloader")
# module defaults to the current step's title if omitted
```

**Console log format:** `timestamp - module - LEVEL - message`

Colors in the portal UI:
| Level | Color |
|---|---|
| `ERROR` | Red |
| `WARNING` | Amber |
| `SUCCESS` | Green |
| Lines starting with `▶` | Cyan |
| Everything else | Gray |

#### Artifacts

Register files uploaded to R2 so they appear in the portal:

```python
reporter.add_artifact(
    name="relatorio.xlsx",
    r2_key="artifacts/run-123/relatorio.xlsx",
    size=45_000,
    mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
```

#### Batch Updates from Activities

Activities cannot modify workflow state directly. Instead, they return a list of updates that the workflow applies:

```python
# In the activity — return updates
return {
    "result": "...",
    "step_updates": [
        {"step_id": "parse", "status": "running", "timestamp": "2026-03-31T12:00:00Z"},
        {"step_id": "parse", "status": "completed", "timestamp": "2026-03-31T12:00:05Z"},
        {"record": {"step_id": "parse", "id": "file1.pdf", "status": "success", "message": "OK"}},
        {"log": "Parsed 150 records"},
        {"artifact": {"name": "output.csv", "r2_key": "artifacts/run-123/output.csv", "size": 1024}},
    ],
}

# In the workflow — apply them
result = await workflow.execute_activity(my_activity, inputs, ...)
self.reporter.apply_updates(result.get("step_updates", []))
```

#### State Export

```python
@workflow.query
def get_progress(self) -> dict:
    return self.reporter.to_dict()
```

Returns a dict with `status`, `current_step`, `steps` (flat list), `step_tree` (nested), `logs`, and `artifacts`. The portal polls this via the Temporal Query API.

### `ondemand.shared.logging`

Custom logger with a `SUCCESS` level (25, between INFO and WARNING) and helpers for structured output.

```python
from ondemand.shared import get_logger

logger = get_logger(__name__)

logger.info("Processing started")
logger.success("All files uploaded")        # SUCCESS level, green in portal
logger.section("Fase 2: Classificacao")     # logs "#### Fase 2: Classificacao", cyan in portal
logger.step("Extrair", "ABC Corp")          # logs "[Extrair] ABC Corp"
logger.divider()                            # logs "============..."
logger.summary("Results", {"total": 42, "errors": 0})

with logger.timed("Uploading files"):
    upload()
# logs "#### Uploading files" on entry
# logs "SUCCESS - Uploading files completed in 3.2s" on exit
# logs "ERROR - Uploading files FAILED after 3.2s" on exception
```

### `ondemand.shared.r2_storage`

Upload and download files from Cloudflare R2 (S3-compatible). Uses boto3 under the hood.

```python
from ondemand.shared import get_r2_client, download_input_files, upload_task_artifacts
from pathlib import Path

# Direct client usage
r2 = get_r2_client()
r2.upload_file(Path("output.xlsx"), "artifacts/run-123/output.xlsx")
r2.download_file("inputs/uuid/data.csv", Path("./downloads/data.csv"))
r2.copy_object("inputs/uuid/data.csv", "artifacts/run-123/inputs/data.csv")

# Download all file-type inputs from a workflow's input dict
downloaded = download_input_files(
    inputs={"planilha": "inputs/uuid/data.xlsx", "empresa": "ABC"},
    dest_dir=Path("./downloads"),
    run_id="run-123",                # copies to artifacts/ for portal visibility
)
# downloaded == {"planilha": Path("./downloads/data.xlsx")}

# Upload a task's output directory
uploaded = upload_task_artifacts(
    task_output_dir=Path("output/run-123/classify"),
    run_id="run-123",
    task_name="classify",
    exclude=["console.txt"],
)
```

### `ondemand.shared.approval`

Pause a workflow and wait for human approval (HITL pattern).

```python
from ondemand import request_approval

approval_url, rejection_url = request_approval(
    message="3 divergencias encontradas. Revisar?",
    data={"total": 15000, "items": ["NF-001", "NF-002", "NF-003"]},
    show_buttons=True,      # show approve/reject buttons in portal UI
    timeout_days=7,         # auto-reject after 7 days (default)
)

# Send notification however you want (email, Slack, WhatsApp, etc.)
send_email(to="reviewer@client.com", body=f"Aprovar: {approval_url}")
```

**Behavior:**
- Synchronous call -- sends a webhook to the portal and gets tokenized URLs back
- After calling, the activity/step should exit normally
- The Temporal workflow pauses automatically (the worker slot is freed)
- If approved, the next step executes
- If rejected, remaining steps are cancelled
- Raises `ApprovalRequestError` if the portal is unreachable after 3 retries

### `ondemand.shared.artifacts`

Manage output directories and pass data between workflow steps.

```python
from ondemand.shared import (
    set_run_id, get_run_id, get_run_info,
    get_output_dir, get_base_output_dir,
    save_artifact, load_artifact,
)

set_run_id("run-123")

# Per-task output: output/run-123/extract/
output_dir = get_output_dir("extract")

# Shared output: output/run-123/
base_dir = get_base_output_dir()

# Save/load JSON artifacts
save_artifact({"companies": [...]}, "companies.json")
data = load_artifact("companies.json", task="extract")

# Run context
info = get_run_info()  # RunInfo(run_id, process_code, organization_id, started_at)
```

## Environment Variables

Set by the platform when running on Cloud Run. For local development, set them manually or use a `.env` file.

| Variable | Required | Description |
|---|---|---|
| `TEMPORAL_ADDRESS` | Yes | Temporal server address (e.g., `temporal.example.com:7233`) |
| `TEMPORAL_NAMESPACE` | Yes | Temporal namespace (typically the org code) |
| `TEMPORAL_QUEUE` | Yes | Task queue name (typically the process code) |
| `ONDEMAND_APP_URL` | No | API base URL for webhook callbacks |
| `SUPERVISOR_WEBHOOK_SECRET` | No | Auth token for webhook calls |
| `WORKER_NAME` | No | Worker name (default: `ondemand-worker`) |
| `WORKER_MAX_CONCURRENT` | No | Max concurrent activities (default: `1`) |
| `WORKER_IDLE_TIMEOUT` | No | Seconds to wait before exiting if idle (default: `300`) |
| `R2_ENDPOINT` | No | Cloudflare R2 endpoint URL |
| `R2_ACCESS_KEY` | No | R2 access key ID |
| `R2_SECRET_KEY` | No | R2 secret access key |
| `R2_BUCKET` | No | R2 bucket name |
| `ONDEMAND_RUN_ID` | No | Current run UUID |
| `ONDEMAND_PROCESS_CODE` | No | Process code for the current run |
| `ONDEMAND_ORGANIZATION_ID` | No | Organization ID for the current run |
| `ONDEMAND_WEBHOOK_URL` | No | Webhook URL (required for `request_approval`) |
| `ONDEMAND_WEBHOOK_SECRET` | No | Webhook auth secret |

## Package Structure

```
ondemand/
├── __init__.py                # Top-level exports (request_approval, ApprovalRequestError)
├── worker/
│   ├── __init__.py            # Exports: OndemandWorker, WorkflowReporter
│   ├── base.py                # OndemandWorker — Temporal connection, polling, idle timeout
│   └── reporter.py            # WorkflowReporter — step tree, records, logs, artifacts
└── shared/
    ├── __init__.py            # Re-exports all shared utilities
    ├── approval.py            # request_approval() for HITL workflows
    ├── artifacts.py           # save_artifact, load_artifact, output dirs, RunInfo
    ├── logging.py             # OndemandLogger with SUCCESS level, section/step/timed helpers
    └── r2_storage.py          # R2StorageClient, download/upload utilities
```

## Publishing

```bash
# Bump version in pyproject.toml, then:
python -m build
python -m twine upload dist/*
```

Requires a PyPI API token configured in `~/.pypirc` or via `TWINE_PASSWORD`.

## License

Apache 2.0
