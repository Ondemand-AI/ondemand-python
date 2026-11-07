"""Synchronous POST to the supervisor webhook, with retries.

Shared by the senders that need an answer back from the API before the robot
can continue: ``request_approval`` (the approval URLs) and ``notify`` (the
notification id). Both use the same run-scoped URL, the same headers and the
same retry policy, so that policy lives here once.

Retry policy:

- A transport error, a timeout or a non-200 status is retried, up to
  ``MAX_RETRIES`` attempts with ``RETRY_DELAY_SECONDS`` between them.
- A 200 is final. If its body is not what the caller expects, the caller's
  ``handle`` raises and that error propagates at once: the API already acted on
  the request, so sending it again would only create a duplicate (a second
  approval record, a second notification).
"""

import logging
import time
from typing import Any, Callable, Dict, Type, TypeVar

import httpx

from ondemand.shared.webhook_auth import webhook_headers

logger = logging.getLogger(__name__)

#: Attempts per call, the first one included.
MAX_RETRIES = 3
#: Pause between attempts, in seconds.
RETRY_DELAY_SECONDS = 2
#: Per-attempt HTTP timeout, in seconds.
REQUEST_TIMEOUT_SECONDS = 30.0

T = TypeVar("T")


def post_webhook_with_retries(
    webhook_url: str,
    payload: Dict[str, Any],
    *,
    handle: Callable[[Dict[str, Any]], T],
    error_cls: Type[Exception],
    what: str,
) -> T:
    """POST ``payload`` to the supervisor webhook and hand the 200 body to ``handle``.

    Args:
        webhook_url: The run's supervisor webhook URL.
        payload: The full JSON body (client, version, action, payload).
        handle: Called with the parsed JSON of a 200 response; its return value
            is returned. It raises ``error_cls`` when the body is unusable, and
            that error is not retried.
        error_cls: Raised once every attempt has failed.
        what: Short label for log lines and the final error ("Approval request").

    Raises:
        error_cls: After ``MAX_RETRIES`` failed attempts, or from ``handle``.
    """
    headers = webhook_headers()

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = client.post(webhook_url, json=payload, headers=headers)
        except httpx.TimeoutException:
            last_error = "Request timed out"
            logger.warning(
                "%s timed out (attempt %d/%d)", what, attempt + 1, MAX_RETRIES
            )
        except Exception as e:
            last_error = str(e)
            logger.warning(
                "%s error (attempt %d/%d): %s", what, attempt + 1, MAX_RETRIES, e
            )
        else:
            if response.status_code == 200:
                try:
                    body = response.json()
                except ValueError as e:
                    raise error_cls(
                        f"{what}: portal returned HTTP 200 with a non-JSON body: "
                        f"{response.text[:500]}"
                    ) from e
                if not isinstance(body, dict):
                    raise error_cls(f"{what}: portal returned an unexpected body: {body!r}")
                return handle(body)

            last_error = f"Portal returned HTTP {response.status_code}: {response.text}"
            logger.warning(
                "%s failed (attempt %d/%d): %s", what, attempt + 1, MAX_RETRIES, last_error
            )

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY_SECONDS)

    raise error_cls(
        f"{what} failed after {MAX_RETRIES} attempts. Last error: {last_error}"
    )
