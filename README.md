# ondemand-ai

Python SDK for building automations on the [Ondemand](https://ondemand-ai.com.br) platform.

Provides `OndemandWorker` (a Temporal worker with automatic log capture and graceful shutdown), `ActivityReporter` (real-time step progress via webhooks), structured logging, R2 artifact storage, and human-in-the-loop approval helpers.

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
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities import process_data, MyInput


@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, input: MyInput) -> dict:
        result = await workflow.execute_activity(
            process_data,
            args=[input],
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        return result
```

```python
# activities.py
import logging
from temporalio import activity
from ondemand.worker.activity_reporter import report

logger = logging.getLogger("my-automation")


@activity.defn
async def process_data(input) -> dict:
    report.step_started("extract", "Extrair Dados")
    # ... do work ...
    report.record("extract", "file1.pdf", "success", "Processed OK")
    report.step_completed("extract", "Extrair Dados")

    logger.info("Processing complete")  # streamed to portal in real-time
    return {"count": 42}
```

```python
# main.py
from ondemand.worker import OndemandWorker
from workflows import MyWorkflow
from activities import process_data

worker = OndemandWorker(name="my-automation")
worker.register_workflow(MyWorkflow)
worker.register_activity(process_data)
worker.run()
```

## Architecture Overview

The SDK is designed for automations running on **GKE Autopilot with KEDA**. KEDA scales worker pods from 0 to N based on Temporal task queue depth, with a `cooldownPeriod` to prevent premature scale-down.

Key design decisions:

- **No heartbeats** -- `start_to_close_timeout` is the only activity timeout. When a pod crashes, Temporal detects it via worker disconnect.
- **Graceful shutdown** -- on SIGTERM (from KEDA scale-down), the worker drains current activities before exiting. Combined with `terminationGracePeriodSeconds: 3600` in K8s.
- **Auto-injected context** -- an activity interceptor automatically sets `ONDEMAND_RUN_ID` and `ONDEMAND_WEBHOOK_URL` before each activity. Developers never set these manually.
- **Real-time updates** -- `ActivityReporter` sends step progress directly to the portal via STEP_REPORT webhooks. `OndemandLogHandler` streams Python logs via LOG_STREAM webhooks. Both feed the portal's SSE stream.

## Modules

### `ondemand.worker.OndemandWorker`

Connects to Temporal, registers workflows and activities, polls a task queue, and handles graceful shutdown on SIGTERM.

```python
from ondemand.worker import OndemandWorker

worker = OndemandWorker(name="my-worker")

# Register workflows and activities
worker.register_workflow(MyWorkflow)
worker.register_activity(my_activity)

# Start polling (blocking call, runs asyncio event loop)
worker.run()
```

**Behavior:**
- Reads configuration from environment variables (see below)
- Auto-sets up `OndemandLogHandler` on startup (captures all Python logs for portal streaming and R2 upload)
- Auto-sets `ONDEMAND_RUN_ID` and `ONDEMAND_WEBHOOK_URL` via activity interceptor before each activity execution
- Drains current activities on SIGTERM before exiting

### `ondemand.worker.activity_reporter`

Sends step progress updates directly to the portal via STEP_REPORT webhooks. Each call fires immediately -- no batching, no state accumulation. The portal writes to DB and broadcasts via SSE for real-time UI updates.

```python
from ondemand.worker.activity_reporter import report

# Report step lifecycle
report.step_started("extract", "Extrair Dados", parent="process")
report.step_completed("extract", "Extrair Dados", parent="process")
report.step_failed("extract", "Extrair Dados", error="Timeout na API", parent="process")
report.step_warning("extract", "Extrair Dados", parent="process")
report.step_skipped("extract", "Extrair Dados", parent="process")

# Attach individual item results to a step
report.record(
    step_id="extract",
    record_id="invoice_001.pdf",
    status="success",
    message="Processado OK",
    metadata={"pages": 3, "total": 1500.00},
)
```

**Step statuses:** `RUNNING`, `SUCCEEDED`, `FAILED`, `WARNING`, `SKIPPED`

All methods are no-ops when `ONDEMAND_WEBHOOK_URL` is not set, making local development seamless.

### `ondemand.worker.logging` (OndemandLogHandler)

Python logging handler that captures all log output for portal display and R2 upload. Automatically set up when `OndemandWorker` starts -- no manual configuration needed.

```python
import logging

logger = logging.getLogger("my-automation")

@activity.defn
async def my_activity():
    logger.info("Processing...")    # captured + streamed to portal + stored for R2
    logger.warning("Watch out")     # same
    return {"result": ...}
```

**Behavior:**
- Attaches to Python's root logger -- captures all Python logging output
- Adds handler directly to the `t_vault` logger (which sets `propagate=False`)
- Tees stderr to capture non-Python output (Temporal Rust core, subprocesses)
- Streams log batches to the portal via LOG_STREAM webhook (every 3 lines or 2 seconds)
- Collects all log lines in memory for R2 upload as `console.log` on run completion

**Console log format:** `timestamp - module - LEVEL - message`

Portal UI color coding:
| Level | Color |
|---|---|
| `ERROR` | Red |
| `WARNING` | Amber |
| `SUCCESS` | Green |
| Lines starting with `####` | Cyan |
| Everything else | Gray |

### `ondemand.worker.WorkflowReporter` (legacy)

Query-based step tree reporter that stores state inside the Temporal workflow for polling via the Temporal Query API. This is the legacy approach -- new automations should use `ActivityReporter` for real-time webhook-based updates instead.

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

# Upload raw content with automatic key prefixing and portal notification
# Key is built as: artifacts/{ONDEMAND_RUN_ID}/{folder}/{filename}
r2.upload_content(
    content=report_bytes,
    filename="report.xlsx",
    folder="reports",
    notify=True,  # POSTs ARTIFACTS_UPLOADED webhook for SSE broadcast
)

# Download all file-type inputs from a workflow's input dict
downloaded = download_input_files(
    inputs={"planilha": "inputs/uuid/data.xlsx", "empresa": "ABC"},
    dest_dir=Path("./downloads"),
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
- Synchronous call -- sends an APPROVAL_REQUESTED webhook to the portal and gets tokenized URLs back
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

Set by the platform at runtime. For local development, set them manually or use a `.env` file.

| Variable | Required | Description |
|---|---|---|
| `TEMPORAL_ADDRESS` | Yes | Temporal server address (e.g., `temporal.ondemand-ai.com.br:7233`) |
| `TEMPORAL_NAMESPACE` | Yes | Temporal namespace |
| `TEMPORAL_QUEUE` | Yes | Task queue name |
| `ONDEMAND_APP_URL` | Yes | API base URL (e.g., `https://api.ondemand-ai.com.br`) |
| `R2_ENDPOINT` | No | Cloudflare R2 endpoint URL |
| `R2_ACCESS_KEY` | No | R2 access key ID |
| `R2_SECRET_KEY` | No | R2 secret access key |
| `R2_BUCKET` | No | R2 bucket name |
| `WORKER_MAX_CONCURRENT` | No | Max concurrent activities (default: `1`) |

`ONDEMAND_RUN_ID` and `ONDEMAND_WEBHOOK_URL` are **auto-set by the activity interceptor** at runtime -- never set these manually.

## Package Structure

```
ondemand/
├── __init__.py                    # Top-level: request_approval, ApprovalRequestError
├── worker/
│   ├── __init__.py                # Exports: OndemandWorker, report, ActivityReporter, setup_logging
│   ├── base.py                    # OndemandWorker + activity interceptor + graceful shutdown
│   ├── activity_reporter.py       # ActivityReporter — real-time step updates via webhook
│   ├── logging.py                 # OndemandLogHandler + _StderrTee + setup_logging()
│   └── reporter.py                # WorkflowReporter (legacy — for Temporal query-based reporting)
└── shared/
    ├── __init__.py                # Re-exports all shared utilities
    ├── approval.py                # request_approval() for HITL workflows
    ├── artifacts.py               # save_artifact, load_artifact, output dirs, RunInfo
    ├── logging.py                 # OndemandLogger with SUCCESS level
    └── r2_storage.py              # R2StorageClient, upload_content, download_input_files
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
