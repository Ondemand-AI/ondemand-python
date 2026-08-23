"""
HITL Approval Requests for Ondemand robots.

Allows robots to pause execution and wait for human approval.
The robot calls request_approval(), gets back approval/rejection URLs,
sends notifications however it wants, and then exits the step.
The Temporal workflow pauses until the human responds.

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
    )

    # Developer sends notification however they want
    send_email(to="reviewer@client.com", body=f"Approve: {approval_url}")

    # Step exits normally after this — workflow pauses in Temporal
"""
import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

import httpx

from ondemand.shared.webhook_auth import webhook_headers
from ondemand.shared.run_context import current_webhook_url

logger = logging.getLogger(__name__)

# Max retries for the webhook call
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2


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

    Passing a tone is optional. The portal already infers one from common key
    names (alertas/alerts, erros/errors, falhas/failures, "sem confiança", …),
    so an existing robot gets sensible colours without changing anything. Use
    this when the key name would not give it away, or to override the guess.

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
) -> Tuple[str, str]:
    """
    Request human approval before continuing the workflow.

    Sends a webhook to the portal which creates an approval record
    and returns tokenized approval/rejection URLs. The robot developer
    is responsible for delivering these URLs to the reviewer (email,
    Slack, WhatsApp, etc.).

    After calling this function and sending notifications, the step
    should exit normally. The Temporal workflow will pause and wait
    for the human to approve or reject via the URLs.

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

    Returns:
        Tuple of (approval_url, rejection_url).

    Raises:
        ApprovalRequestError: If the webhook call fails after all retries.
    """
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

    # Build the webhook payload
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
        },
    }

    # Send the webhook with retries
    headers = webhook_headers()

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    webhook_url,
                    json=payload,
                    headers=headers,
                )

                if response.status_code == 200:
                    result = response.json()
                    approval_url = result.get("approval_url")
                    rejection_url = result.get("rejection_url")

                    if not approval_url or not rejection_url:
                        raise ApprovalRequestError(
                            f"Portal returned success but missing URLs: {result}"
                        )

                    logger.info(
                        f"Approval requested for step '{step_name}': {message}"
                    )

                    return approval_url, rejection_url

                else:
                    last_error = f"Portal returned HTTP {response.status_code}: {response.text}"
                    logger.warning(
                        f"Approval request failed (attempt {attempt + 1}/{MAX_RETRIES}): {last_error}"
                    )

        except httpx.TimeoutException:
            last_error = "Request timed out"
            logger.warning(
                f"Approval request timed out (attempt {attempt + 1}/{MAX_RETRIES})"
            )
        except Exception as e:
            last_error = str(e)
            logger.warning(
                f"Approval request error (attempt {attempt + 1}/{MAX_RETRIES}): {e}"
            )

        # Wait before retry (except on last attempt)
        if attempt < MAX_RETRIES - 1:
            import time
            time.sleep(RETRY_DELAY_SECONDS)

    raise ApprovalRequestError(
        f"Failed to request approval after {MAX_RETRIES} attempts. "
        f"Last error: {last_error}"
    )
