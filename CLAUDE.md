# ondemand-python — Automation Agent Toolkit

Core Python library for all Ondemand automation workers.

- **PyPI name:** `ondemand-ai`
- **Import path:** `ondemand`
- **Current version:** 1.5.0
- **Python:** ≥ 3.9
- **Branch:** `main`

## Installation

Published to PyPI. Robots install via `requirements.txt`:

```
ondemand-ai[worker]>=1.5.0
```

Managed locally with `uv` (`uv.lock` present).

## Package layout

```
ondemand/
├── __init__.py          # Exports: request_approval, ApprovalRequestError
├── worker/              # OndemandWorker, WorkflowReporter (requires [worker] extra)
├── supervisor/          # Step/scope management for run reporting
├── shared/
│   ├── approval.py      # request_approval, ApprovalRequestError
│   └── ...              # get_logger, shared utilities
├── screen_recorder/     # Screen capture for robots that need it
└── utils/               # General helpers
```

## Public API

Top-level exports (`from ondemand import ...`):

```python
from ondemand import request_approval, ApprovalRequestError
from ondemand.worker import OndemandWorker, WorkflowReporter
from ondemand.shared import get_logger
```

## Dependencies

- Base: `requests`, `httpx`, `boto3`
- `[worker]` extra: `temporalio>=1.7.0`, `ondemand-obs[temporal]>=0.1.0`

## Release process

1. Bump version in `pyproject.toml`
2. Update `CHANGELOG.md`
3. Push to `main`
4. Push a tag `vX.Y.Z` → GitHub Actions publishes to PyPI automatically

## Changelog

See `CHANGELOG.md` for version history.

## Workflow input contract

`ondemand.worker.WorkflowInput` holds the fields the portal sends on every
start. Robots **subclass** it and declare only what is theirs:

```python
from ondemand.worker import WorkflowInput

@dataclass
class MyInput(WorkflowInput):
    @property
    def period(self) -> str:
        return self.inputs.get("period", "")
```

Never redeclare `workflow_id`, `process_code`, `organization_id`,
`webhook_url` or `inputs` in a robot. That duplication is why the 1.9.0 rename
had to touch every automation repo; with the contract here, the next change
reaches robots through the container-start upgrade with no rebuild.

Every field has a default, `workflow_id` included — Python 3.9 has no `kw_only`
for dataclasses, so a field without a default cannot follow one that has it.
Give robot-specific fields defaults too.

## Identifier vocabulary — Workflow ID vs Run ID

The platform uses **Temporal's own names**, everywhere, for the two identifiers
of a Workflow Execution. Temporal identifies one by a **pair**:

| Name | What it is | Where it lives |
|---|---|---|
| **Workflow ID** | The business identifier we choose. `executor.js` starts every workflow with `workflowId: run.id`, so `process_runs.id` **is** the Workflow ID. Stable across retries. | `process_runs.id`, `step_runs.workflow_id`, `approval_requests.workflow_id`, `scheduled_runs.last_workflow_id`, `artifacts/{workflow_id}/…`, `/api/webhooks/supervisor/{workflow_id}` |
| **Run ID** | A server-generated UUID identifying **one execution** of that Workflow ID. A new one appears on workflow retry, continue-as-new, cron tick, reset, or re-run under the same Workflow ID. | `process_runs.temporal_run_id`, `step_runs.temporal_run_id` |

- **API JSON:** `workflow_id` and `run_id`.
- **HyperDX:** `TemporalWorkflowID` and `TemporalRunID` on every span *and* every
  log, in every service — the same spelling Temporal's own `TracingInterceptor`
  writes, so one filter follows a run portal → API → robot.
- **Python:** `current_workflow_id()` and `current_temporal_run_id()` in
  `ondemand.shared.run_context`. Env var: `ONDEMAND_WORKFLOW_ID`.

Never reintroduce a bare `run_id` meaning the Workflow ID. That collision is
what this vocabulary exists to end.

`step_runs` is keyed on `(workflow_id, temporal_run_id, step_id)` with
`NULLS NOT DISTINCT`, so a workflow retry gets its own step rows instead of
overwriting the previous attempt's.
