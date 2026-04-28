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
