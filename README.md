# Ondemand Python Library

Python library for building automation agents on the Ondemand platform. Provides step supervision, input handling, artifact management, R2 storage, and **HITL approval requests**.

## Installation

### For RCC Robots (Production)

Robots use the `install_private_packages.py` shim in `bin/` which installs the library at runtime using `GITHUB_TOKEN`. See `robo-demo` for the pattern.

### Local Development

```bash
# Via SSH (recommended)
pip install git+ssh://git@github.com/Ondemand-AI/ondemand-python.git@main

# Or install locally in editable mode
git clone git@github.com:Ondemand-AI/ondemand-python.git
cd ondemand-python
pip install -e .
```

## Core Features

### 1. Supervised Steps (`@supervised_step`)

```python
from ondemand import supervised_step

@supervised_step("Processamento de Dados")
def process(self):
    # Your automation logic here
    # Step reporting, artifact upload, and error handling are automatic
    pass
```

### 2. Getting Inputs

```python
from ondemand.shared import get_inputs

inputs = get_inputs()  # Parses ONDEMAND_INPUTS JSON env var
empresa = inputs.get("empresa")
periodo = inputs.get("periodo")
input_files = inputs.get("input_file")  # Can be a list of R2 keys
```

### 3. Artifacts

```python
from ondemand.shared import save_artifact, load_artifact

# Save artifact (to output/{run_id}/{task_name}/)
save_artifact({"companies": data})

# Load artifact from another task
state = load_artifact(task="Iniciar Coleta")
```

### 4. R2 File Downloads

```python
from ondemand.shared import download_input_files

# Downloads all R2-keyed inputs to a local directory
downloaded = download_input_files(inputs, dest_dir=Path("./downloads"))
```

### 5. HITL Approvals (`request_approval`)

Pause execution and wait for human approval before continuing.

```python
from ondemand import request_approval

# Request approval — returns URLs for approve/reject
approval_url, rejection_url = request_approval(
    message="3 divergências encontradas. Revisar?",
    data={"total": 15000, "empresas": ["ABC Corp", "XYZ Inc"]},
    show_buttons=True,     # Show buttons in portal UI
    timeout_days=7,        # Max wait time (default: 7)
)

# Developer sends notification however they want
logger.info(f"Approval: {approval_url}")
logger.info(f"Rejection: {rejection_url}")
send_email(to="reviewer@client.com", body=f"Approve: {approval_url}")
```

**Behavior:**
- `request_approval()` is synchronous — sends webhook to portal, gets URLs back
- After calling, the step should exit normally (artifacts are uploaded)
- The Temporal workflow pauses automatically (worker slot is freed)
- If approved → next task executes
- If rejected → remaining steps cancelled, run completes successfully
- If timeout → run status becomes `timed_out`, approval auto-rejected

**Raises `ApprovalRequestError`** if the portal is unreachable after 3 retries.

### 6. Dynamic Manifests

```python
from ondemand import build_manifest_step, update_manifest

steps = [
    build_manifest_step("company_a", "Process Company A", children=[
        build_manifest_step("company_a_extract", "Extract Data"),
        build_manifest_step("company_a_validate", "Validate Data"),
    ])
]
update_manifest(steps, parent_step_id="Processing")
```

## Input Sources (Priority)

1. **CLI `--inputs`** — JSON string (local testing)
2. **CLI `--inputs-file`** — Path to JSON file (local testing)
3. **`ONDEMAND_INPUTS` env var** — Set by worker in production (JSON string)

> **Note:** Inputs are NOT individual env vars. They come as a single JSON string in `ONDEMAND_INPUTS`.

## Environment Variables (Set by Worker)

| Variable | Description |
|---|---|
| `ONDEMAND_RUN_ID` | Current run UUID |
| `ONDEMAND_INPUTS` | JSON string with all user inputs |
| `ONDEMAND_WEBHOOK_URL` | Webhook URL for reporting progress |
| `ONDEMAND_WEBHOOK_SECRET` | Auth secret for webhooks |
| `GITHUB_TOKEN` | For private package installation |
| `R2_ENDPOINT`, `R2_ACCESS_KEY`, `R2_SECRET_KEY`, `R2_BUCKET` | R2 storage credentials |
| `BW_*` | Bitwarden vault credentials (per-org) |

## Local Testing

```bash
# With inline inputs
python src/process.py --inputs '{"empresa": "Test", "periodo": "2024-01"}'

# With input file
python src/process.py --inputs-file test_inputs.json

# With RCC
rcc task run --task Process -- --inputs '{"empresa": "Test"}'
```

## Package Structure

```
ondemand/
├── __init__.py              # Top-level exports (supervised_step, request_approval, etc.)
├── shared/
│   ├── __init__.py          # Shared exports
│   ├── approval.py          # request_approval() for HITL
│   ├── artifacts.py         # save_artifact, load_artifact, output dirs
│   ├── cli.py               # CLI parsing, get_inputs()
│   ├── logging.py           # OndemandLogger
│   └── r2_storage.py        # R2 client, download_input_files, upload
└── supervisor/
    ├── __init__.py           # Supervisor exports
    └── connector.py          # @supervised_step decorator, webhook reporting
```
