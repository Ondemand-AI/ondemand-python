"""Every way a step can end sends the records it buffered.

step_warning used to skip the flush, so a step that ended with alerts never sent
its records and the portal had no list to open for exactly those steps.
"""

import pytest

from ondemand.worker import activity_reporter as ar


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(ar, "_step_report", lambda **kw: calls.append(kw))
    return calls


@pytest.mark.parametrize("end", ["step_completed", "step_warning", "step_failed", "step_skipped"])
def test_ending_a_step_sends_its_records(sent, end):
    r = ar.ActivityReporter()
    r.record("validate", "nf-1", "success", "ok")
    r.record("validate", "nf-2", "warning", "Validado com alertas")
    getattr(r, end)("validate", "Validar Dados")

    with_records = [c for c in sent if c.get("records")]
    assert len(with_records) == 1
    assert [x["id"] for x in with_records[0]["records"]] == ["nf-1", "nf-2"]
    # Records go out before the step's own terminal report.
    assert sent.index(with_records[0]) < len(sent) - 1
    assert r._records == {}
