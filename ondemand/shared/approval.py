"""
HITL Approval Requests for Ondemand robots.

Allows robots to pause execution and wait for human approval.
The robot calls request_approval(), gets back approval/rejection URLs,
sends notifications however it wants, and then exits the step.
The Temporal workflow pauses until the human responds.

Usage:
    from ondemand import request_approval

    approval_url, rejection_url = request_approval(
        message="3 divergências encontradas. Revisar?",
        data={"total": 15000, "items": [...]},
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

logger = logging.getLogger(__name__)

# Max retries for the webhook call
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2


class ApprovalRequestError(Exception):
    """Raised when the approval request fails after all retries."""
    pass


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
        data: Optional context data for the reviewer (shown in portal UI).
        show_buttons: If True, the portal UI shows approve/reject buttons
                      inline. If False, only the external links work.
        step_name: Step name (auto-detected from current task if not provided).
        timeout_days: How many days to wait for approval before timing out (default: 7).

    Returns:
        Tuple of (approval_url, rejection_url).

    Raises:
        ApprovalRequestError: If the webhook call fails after all retries.
    """
    # Get webhook URL and secret from environment
    webhook_url = os.environ.get("ONDEMAND_WEBHOOK_URL")
    webhook_secret = os.environ.get("ONDEMAND_WEBHOOK_SECRET", "")

    if not webhook_url:
        raise ApprovalRequestError(
            "ONDEMAND_WEBHOOK_URL not set. Cannot request approval outside of an Ondemand execution."
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
    headers = {
        "Content-Type": "application/json",
    }
    if webhook_secret:
        headers["Authorization"] = f"Bearer {webhook_secret}"

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
