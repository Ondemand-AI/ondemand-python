"""The activity interceptor binds the run's organization for ondemand-obs."""

import asyncio
import dataclasses
import types

import ondemand_obs
from ondemand.worker import base


class _Next:
    def __init__(self):
        self.seen = None

    def init(self, outbound):
        pass

    async def execute_activity(self, input):
        self.seen = ondemand_obs.current_organization_id()
        return "ok"


@dataclasses.dataclass
class _Input:
    organization_id: str = ""


def _run(monkeypatch, args):
    monkeypatch.setenv("ONDEMAND_HEARTBEAT_SECONDS", "0")
    info = types.SimpleNamespace(workflow_id="wf-1", workflow_run_id="run-1")
    monkeypatch.setattr(base.activity, "info", lambda: info)
    nxt = _Next()
    result = asyncio.run(base._OndemandActivityInterceptor(nxt).execute_activity(types.SimpleNamespace(args=args)))
    assert result == "ok"
    return nxt.seen


def test_binds_organization_from_dataclass_input(monkeypatch):
    assert _run(monkeypatch, (_Input("org-1"),)) == "org-1"
    assert ondemand_obs.current_organization_id() is None  # reset afterwards


def test_binds_organization_from_dict_input(monkeypatch):
    assert _run(monkeypatch, ({"organization_id": "org-2"},)) == "org-2"


def test_no_organization_when_activity_takes_something_else(monkeypatch):
    assert _run(monkeypatch, ()) is None
    assert _run(monkeypatch, ("plain-string",)) is None
    assert _run(monkeypatch, (_Input(""),)) is None
