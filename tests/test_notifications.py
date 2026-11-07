"""request_approval notify/email options, notify(), and ApprovalDecision."""

import json

import httpx
import pytest

import ondemand
import ondemand.shared
from ondemand.shared import approval, notifications, webhook_client
from ondemand.shared.approval_decision import ApprovalDecision
from ondemand.shared.notifications import NotificationError, normalize_emails

WEBHOOK = "https://api.example.test/api/webhooks/supervisor/wf-1"

OLD_APPROVAL_KEYS = {"message", "data", "show_buttons", "step_name", "timeout_days"}


class _Recorder:
    """Stands in for the API: records each POST and answers from a script."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def api(monkeypatch):
    """Route the webhook client through a mock transport, with no retry delay."""
    monkeypatch.setenv("ONDEMAND_WEBHOOK_URL", WEBHOOK)
    monkeypatch.delenv("ONDEMAND_APP_URL", raising=False)
    monkeypatch.delenv("ONDEMAND_WORKFLOW_ID", raising=False)
    monkeypatch.setenv("ONDEMAND_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(webhook_client, "RETRY_DELAY_SECONDS", 0)

    holder = {}

    def install(*responses):
        rec = _Recorder(responses)
        real_client = httpx.Client

        def fake_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(rec.handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(webhook_client.httpx, "Client", fake_client)
        holder["rec"] = rec
        return rec

    return install


def _approval_ok():
    return httpx.Response(200, json={
        "success": True,
        "approval_url": "https://x/approve",
        "rejection_url": "https://x/reject",
        "approval_id": "a-1",
    })


# --- request_approval -------------------------------------------------------


def test_approval_without_new_args_sends_old_keys_plus_default_notify(api):
    rec = api(_approval_ok())
    urls = ondemand.request_approval("Revisar?", data={"Notas": 9}, step_name="classify")

    assert urls == ("https://x/approve", "https://x/reject")
    (body,) = rec.bodies
    assert body["action"] == "APPROVAL_REQUESTED"
    assert body["client"] == "ondemand-python"
    assert body["version"] == "2.0.0"
    assert set(body["payload"]) == OLD_APPROVAL_KEYS | {"notify"}
    assert body["payload"]["notify"] == {"portal": True, "email": []}
    assert body["payload"]["message"] == "Revisar?"
    assert body["payload"]["data"] == {"Notas": 9}
    assert body["payload"]["show_buttons"] is True
    assert body["payload"]["step_name"] == "classify"
    assert body["payload"]["timeout_days"] == 7
    assert rec.requests[0].headers["authorization"] == "Bearer s3cret"
    assert str(rec.requests[0].url) == WEBHOOK


def test_approval_with_notify_and_email(api):
    rec = api(_approval_ok())
    ondemand.request_approval(
        "Revisar?",
        step_name="s",
        notify=False,
        email=[" A@Cliente.com.br ", "b@cliente.com.br", "a@cliente.com.br"],
    )
    assert rec.bodies[0]["payload"]["notify"] == {
        "portal": False,
        "email": ["A@Cliente.com.br", "b@cliente.com.br"],
    }


def test_approval_single_email_string_becomes_list(api):
    rec = api(_approval_ok())
    ondemand.request_approval("Revisar?", step_name="s", email="revisor@cliente.com.br")
    assert rec.bodies[0]["payload"]["notify"] == {
        "portal": True,
        "email": ["revisor@cliente.com.br"],
    }


def test_approval_new_args_are_keyword_only():
    with pytest.raises(TypeError):
        ondemand.request_approval("m", None, True, "s", 7, False)


def test_approval_invalid_email_raises_before_any_http(api):
    rec = api(_approval_ok())
    with pytest.raises(ValueError):
        ondemand.request_approval("Revisar?", step_name="s", email=["ok@x.com", "not-an-address"])
    assert rec.requests == []


def test_approval_retries_then_raises(api):
    rec = api(httpx.Response(502, text="bad gateway"))
    with pytest.raises(ondemand.ApprovalRequestError, match="after 3 attempts.*HTTP 502"):
        ondemand.request_approval("Revisar?", step_name="s")
    assert len(rec.requests) == webhook_client.MAX_RETRIES


def test_approval_recovers_after_transient_errors(api):
    rec = api(httpx.ConnectError("refused"), httpx.Response(500), _approval_ok())
    assert ondemand.request_approval("Revisar?", step_name="s") == (
        "https://x/approve",
        "https://x/reject",
    )
    assert len(rec.requests) == 3


def test_approval_200_without_urls_is_not_retried(api):
    rec = api(httpx.Response(200, json={"success": True}))
    with pytest.raises(ondemand.ApprovalRequestError, match="missing URLs"):
        ondemand.request_approval("Revisar?", step_name="s")
    assert len(rec.requests) == 1


def test_approval_without_run_context(monkeypatch):
    monkeypatch.delenv("ONDEMAND_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ONDEMAND_APP_URL", raising=False)
    with pytest.raises(ondemand.ApprovalRequestError, match="No run context"):
        ondemand.request_approval("Revisar?")


def test_approval_module_keeps_retry_constants():
    assert approval.MAX_RETRIES == 3
    assert approval.RETRY_DELAY_SECONDS == 2


# --- notify -----------------------------------------------------------------


def _notify_ok(nid="n-1"):
    return httpx.Response(200, json={"success": True, "notification_id": nid})


def test_notify_payload_shape(api):
    rec = api(_notify_ok("n-42"))
    nid = ondemand.notify(
        "  Conciliação concluída  ",
        body="1.284 transações",
        level="success",
        email="financeiro@cliente.com.br",
        link="https://app.example/runs/1",
    )
    assert nid == "n-42"
    (body,) = rec.bodies
    assert body["action"] == "NOTIFY"
    assert body["client"] == "ondemand-python"
    assert body["version"] == "2.0.0"
    assert body["payload"] == {
        "title": "Conciliação concluída",
        "body": "1.284 transações",
        "level": "success",
        "email": ["financeiro@cliente.com.br"],
        "link": "https://app.example/runs/1",
    }
    assert rec.requests[0].headers["authorization"] == "Bearer s3cret"
    assert str(rec.requests[0].url) == WEBHOOK


def test_notify_minimal_payload(api):
    rec = api(_notify_ok())
    ondemand.notify("Pronto")
    assert rec.bodies[0]["payload"] == {
        "title": "Pronto",
        "body": None,
        "level": "info",
        "email": [],
        "link": None,
    }


@pytest.mark.parametrize("level", ["info", "success", "warning", "error"])
def test_notify_accepts_each_level(api, level):
    rec = api(_notify_ok())
    ondemand.notify("t", level=level)
    assert rec.bodies[0]["payload"]["level"] == level


@pytest.mark.parametrize("level", ["INFO", "warn", "critical", "", None])
def test_notify_rejects_unknown_level(api, level):
    rec = api(_notify_ok())
    with pytest.raises(ValueError, match="level"):
        ondemand.notify("t", level=level)
    assert rec.requests == []


@pytest.mark.parametrize("title", ["", "   ", None, 5])
def test_notify_requires_title(api, title):
    rec = api(_notify_ok())
    with pytest.raises(ValueError, match="title"):
        ondemand.notify(title)
    assert rec.requests == []


def test_notify_title_length_limit(api):
    rec = api(_notify_ok())
    ondemand.notify("x" * 200)
    ondemand.notify("  " + "x" * 200 + "  ")  # trimmed before measuring
    with pytest.raises(ValueError, match="200"):
        ondemand.notify("x" * 201)
    assert len(rec.requests) == 2


@pytest.mark.parametrize("link", ["javascript:alert(1)", "ftp://x/y", "//evil.example/x", "runs/1"])
def test_notify_rejects_bad_link(api, link):
    rec = api(_notify_ok())
    with pytest.raises(ValueError, match="link"):
        ondemand.notify("t", link=link)
    assert rec.requests == []


def test_notify_accepts_portal_path_link(api):
    rec = api(_notify_ok())
    ondemand.notify("t", link="/runs/wf-1")
    assert rec.bodies[0]["payload"]["link"] == "/runs/wf-1"


def test_notify_invalid_email_raises_before_any_http(api):
    rec = api(_notify_ok())
    with pytest.raises(ValueError):
        ondemand.notify("t", email="a@x.com; b@y.com")
    assert rec.requests == []


def test_notify_retries_then_raises_notification_error(api):
    rec = api(httpx.ReadTimeout("slow"))
    with pytest.raises(NotificationError, match="after 3 attempts.*timed out"):
        ondemand.notify("t")
    assert len(rec.requests) == webhook_client.MAX_RETRIES


def test_notify_recovers_after_transient_error(api):
    rec = api(httpx.Response(503), _notify_ok("n-2"))
    assert ondemand.notify("t") == "n-2"
    assert len(rec.requests) == 2


def test_notify_on_api_without_notify_support(api):
    # An API that predates NOTIFY logs "Unknown action" and answers a plain 200.
    rec = api(httpx.Response(200, json={"success": True, "message": "Webhook processed successfully"}))
    with pytest.raises(NotificationError, match="notification_id"):
        ondemand.notify("t")
    assert len(rec.requests) == 1


def test_notify_without_run_context(monkeypatch):
    monkeypatch.delenv("ONDEMAND_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ONDEMAND_APP_URL", raising=False)
    with pytest.raises(NotificationError, match="No run context"):
        ondemand.notify("t")


# --- address normalisation --------------------------------------------------


def test_normalize_emails_none_and_empty_list():
    assert normalize_emails(None) == []
    assert normalize_emails([]) == []


def test_normalize_emails_string_strip_dedupe_order():
    assert normalize_emails("  a@x.com ") == ["a@x.com"]
    assert normalize_emails(("b@x.com", "A@x.com", "a@X.com", "b@x.com")) == ["b@x.com", "A@x.com"]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "plainaddress",
        "@x.com",
        "a@",
        "a@x",
        "a@@x.com",
        "a b@x.com",
        "a@x.com, b@x.com",
        "a@x.com;b@x.com",
        "Name <a@x.com>",
        "a@x.com.",
        ["a@x.com", None],
        ["a@x.com", 3],
        3,
        {"a@x.com": 1},
        b"a@x.com",
    ],
)
def test_normalize_emails_rejects(bad):
    with pytest.raises(ValueError):
        normalize_emails(bad)


# --- ApprovalDecision -------------------------------------------------------


def test_decision_from_full_signal():
    d = ApprovalDecision.from_signal(
        {"approved": True, "comment": "  ok, pode seguir ", "resolved_by": "user-1"}
    )
    assert d == ApprovalDecision(approved=True, comment="ok, pode seguir", resolved_by="user-1")


def test_decision_from_legacy_signal():
    assert ApprovalDecision.from_signal({"approved": False}) == ApprovalDecision(False, None, None)
    assert ApprovalDecision.from_signal({"approved": True}).approved is True


def test_decision_fails_closed():
    assert ApprovalDecision.from_signal({}).approved is False
    assert ApprovalDecision.from_signal({"approved": "true"}).approved is False
    assert ApprovalDecision.from_signal({"approved": 1}).approved is False
    assert ApprovalDecision.from_signal(None).approved is False
    assert ApprovalDecision.from_signal(True).approved is True


def test_decision_empty_comment_is_none_and_unknown_keys_ignored():
    d = ApprovalDecision.from_signal({"approved": True, "comment": "  ", "extra": 1})
    assert d.comment is None and d.resolved_by is None


# --- exports ----------------------------------------------------------------


def test_exports():
    for mod in (ondemand, ondemand.shared):
        assert mod.notify is notifications.notify
        assert mod.NotificationError is NotificationError
        assert mod.ApprovalDecision is ApprovalDecision
        for name in ("notify", "NotificationError", "ApprovalDecision"):
            assert name in mod.__all__
