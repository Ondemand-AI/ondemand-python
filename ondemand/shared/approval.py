"""
HITL Approval Requests for Ondemand robots.

Allows robots to pause execution and wait for human approval.
The robot calls request_approval(), the portal records the request, notifies
the run's organization and e-mails the reviewers the robot names, and the
robot gets back the approval/rejection URLs. Then the step exits and the
Temporal workflow pauses until the human responds.

Usage:
    from ondemand import request_approval, fact

    approval_url, rejection_url = request_approval(
        message="3 divergências encontradas. Revisar?",
        data={
            "Transações": 1284,
            "Sem confiança": 18,
            "Alertas": fact(4, "warn"),      # coloured amber in the portal
            "Erros": fact(0, "danger"),      # zero is never coloured
            "Período": "07/2026",
        },
        show_buttons=True,
        email=["revisor@cliente.com.br"],   # gets Aprovar / Rejeitar buttons
    )

    # Step exits normally after this; the workflow pauses in Temporal
"""
import logging
from typing import Any, Dict, Optional, Tuple

from ondemand.shared.notifications import EmailArg, normalize_emails
from ondemand.shared.run_context import current_webhook_url
from ondemand.shared.webhook_client import (  # noqa: F401  (re-exported, kept for callers)
    MAX_RETRIES,
    RETRY_DELAY_SECONDS,
    post_webhook_with_retries,
)

logger = logging.getLogger(__name__)


class ApprovalRequestError(Exception):
    """Raised when the approval request fails after all retries."""
    pass


# Tones the portal understands for a fact. Anything else is ignored and the
# value renders neutral, so a typo degrades rather than breaking the card.
FACT_TONES = ("ok", "warn", "danger", "info", "neutral")


def fact(value: Any, tone: Optional[str] = None) -> Dict[str, Any]:
    """
    Mark one entry of an approval's `data` so the portal colours its value.

    The portal renders `data` as a strip of figures above the approve/reject
    buttons — the numbers the reviewer decides on. By default every figure is
    neutral; this flags the one that matters.

    A tone colours the value only, never the whole tile: one amber number among
    four grey ones is what makes it findable, and colouring everything would
    make none of it stand out.

        data={
            "Notas fiscais": 9,
            "Alertas": fact(4, "warn"),
            "Erros": fact(2, "danger"),
        }

    The tone is the ONLY thing that colours a figure. A bare value is always
    neutral — the portal does not guess from the key name.

    It used to: a table of regexes matched alertas/erros/falhas/sucesso and
    coloured accordingly, so a robot got colours for free. That was removed
    because the rule was invisible from both ends. "success_rate" came out green
    for no reason other than starting with "success", while a key like
    "notas_recusadas" stayed grey because no pattern happened to cover it — two
    figures of equal weight, coloured differently, and nothing on screen or in
    the robot explained why. If a figure should be coloured, say so here.

    Zero is never coloured, whatever the tone: "Alertas: 0" is good news, and
    an amber zero would cry wolf.

    Args:
        value: The figure to show. Numbers, strings and booleans render;
               nested structures are skipped by the portal.
        tone: One of FACT_TONES, or None to let the portal infer.

    Returns:
        The dict shape the portal expects for a toned fact.
    """
    if tone is not None and tone not in FACT_TONES:
        # Not fatal — a bad tone should never lose the figure itself.
        logger.warning(
            "Unknown approval fact tone %r; rendering neutral. Expected one of %s",
            tone,
            ", ".join(FACT_TONES),
        )
        tone = None
    out: Dict[str, Any] = {"value": value}
    if tone is not None:
        out["tone"] = tone
    return out


def request_approval(
    message: str,
    data: Optional[Dict[str, Any]] = None,
    show_buttons: bool = True,
    step_name: Optional[str] = None,
    timeout_days: int = 7,
    *,
    notify: bool = True,
    email: EmailArg = None,
) -> Tuple[str, str]:
    """
    Request human approval before continuing the workflow.

    Sends a webhook to the portal, which creates an approval record and
    returns tokenized approval/rejection URLs. The portal also tells the
    reviewers: with ``notify=True`` (the default) it creates an in-portal
    notification for the run's organization, and every address in ``email``
    receives an approval e-mail with Aprovar / Rejeitar buttons. The robot no
    longer has to deliver the URLs itself, though it still can (Slack,
    WhatsApp, its own e-mail): they are returned as before.

    ``notify`` defaults to True, so a robot built on 1.20.0 or later gets a
    portal notification for every approval without asking for it. Pass
    ``notify=False`` to opt out. An API that predates notifications ignores
    the extra ``notify`` key and behaves exactly as before.

    After calling this function, the step should exit normally. The Temporal
    workflow will pause and wait for the human to approve or reject.

    Reading the decision: the API resumes the workflow with the
    ``human_approval`` signal, whose argument is a dict
    ``{"approved": bool, "comment": str | None, "resolved_by": str | None}``.
    ``ApprovalDecision.from_signal(data)`` turns it into a typed value:

        @workflow.signal
        async def human_approval(self, data: dict) -> None:
            self._decision = ApprovalDecision.from_signal(data)
            # self._decision.approved, .comment, .resolved_by

    Import it in the workflow from ``ondemand.shared.approval_decision``
    (standard library only, sandbox safe). Handlers that read
    ``data.get("approved")`` directly keep working.

    Args:
        message: Human-readable message explaining what needs approval.
        data: Figures for the reviewer, rendered as a strip above the
              approve/reject buttons. Keys become the labels, insertion order is
              preserved, and the portal caps the strip at 8. Values may be
              plain (``"Notas": 9``) or toned via ``fact()``
              (``"Alertas": fact(4, "warn")``). Nested structures are skipped.
        show_buttons: If True, the portal UI shows approve/reject buttons
                      inline. If False, only the external links work.
        step_name: Step name (auto-detected from current task if not provided).
        timeout_days: How many days to wait for approval before timing out (default: 7).
        notify: Keyword only. Create an in-portal notification for the run's
                organization (default: True).
        email: Keyword only. One address or a list of addresses that receive
               the approval e-mail. Stripped, validated and de-duplicated.

    Returns:
        Tuple of (approval_url, rejection_url).

    Raises:
        ValueError: On an invalid e-mail address, before any HTTP call.
        ApprovalRequestError: Outside an Ondemand run, or if the webhook call
            fails after all retries.
    """
    emails = normalize_emails(email)

    # Resolved from the run context; auth headers come from the shared builder
    webhook_url = current_webhook_url()

    if not webhook_url:
        raise ApprovalRequestError(
            "No run context. Cannot request approval outside of an Ondemand execution."
        )

    # Auto-detect step name from the shared module if not provided
    if not step_name:
        try:
            from ondemand.shared.artifacts import get_current_task
            step_name = get_current_task() or "unknown"
        except Exception:
            step_name = "unknown"

    payload = {
        "client": "ondemand-python",
        "version": "2.0.0",
        "action": "APPROVAL_REQUESTED",
        "payload": {
            "message": message,
            "data": data or {},
            "show_buttons": show_buttons,
            "step_name": step_name,
            "timeout_days": timeout_days,
            "notify": {"portal": bool(notify), "email": emails},
        },
    }

    def _handle(result: Dict[str, Any]) -> Tuple[str, str]:
        approval_url = result.get("approval_url")
        rejection_url = result.get("rejection_url")
        if not approval_url or not rejection_url:
            raise ApprovalRequestError(
                f"Portal returned success but missing URLs: {result}"
            )
        logger.info(f"Approval requested for step '{step_name}': {message}")
        return approval_url, rejection_url

    return post_webhook_with_retries(
        webhook_url,
        payload,
        handle=_handle,
        error_cls=ApprovalRequestError,
        what="Approval request",
    )
