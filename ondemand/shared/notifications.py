"""Notifications from a robot to the people who follow its runs.

The automation decides when to notify and whom to e-mail; the portal only
displays what the robot sends. Two entry points carry notifications:

- ``request_approval(..., notify=True, email=[...])`` notifies the run's
  organization that an approval is waiting, and e-mails the given addresses
  with Aprovar / Rejeitar buttons.
- ``notify(title, ...)`` sends a standalone notification (in the portal, and
  optionally by e-mail) at any point of a run.

Usage:
    from ondemand import notify

    notify(
        "Conciliação concluída",
        body="1.284 transações, 18 sem confiança.",
        level="success",
        email="financeiro@cliente.com.br",
        link="https://app.ondemand-ai.com.br/runs/...",
    )
"""
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Union

from ondemand.shared.run_context import current_webhook_url
from ondemand.shared.webhook_client import post_webhook_with_retries

logger = logging.getLogger(__name__)

#: Levels the portal understands for a notification.
NOTIFICATION_LEVELS = ("info", "success", "warning", "error")

#: Longest title the portal accepts, after trimming.
MAX_TITLE_LENGTH = 200

# Deliberately loose: one "@", no whitespace or list separators, and a dot in
# the domain. It catches typos and pasted lists ("a@x.com; b@y.com"), not every
# RFC 5322 corner case; the mail provider is the real judge.
_EMAIL_RE = re.compile(r"^[^@\s,;<>()\[\]\"]+@[^@\s,;<>()\[\]\"]+\.[^@\s,;<>()\[\]\".]+$")

EmailArg = Union[str, Iterable[str], None]


class NotificationError(Exception):
    """Raised when a notification could not be delivered to the portal."""
    pass


def normalize_emails(email: EmailArg) -> List[str]:
    """Turn the ``email`` argument into a clean list of addresses.

    Accepts None, one address or a list (any iterable) of addresses. Each one
    is stripped and checked; duplicates are dropped, compared without regard to
    case, keeping the first spelling and the original order.

    Raises:
        ValueError: On a value that is not a string, or an address that is
            empty or obviously malformed.
    """
    if email is None:
        return []
    if isinstance(email, str):
        candidates: List[Any] = [email]
    elif isinstance(email, (bytes, dict)):
        raise ValueError(f"email must be a string or a list of strings, got {type(email).__name__}")
    else:
        try:
            candidates = list(email)
        except TypeError:
            raise ValueError(
                f"email must be a string or a list of strings, got {type(email).__name__}"
            ) from None

    out: List[str] = []
    seen = set()
    for raw in candidates:
        if not isinstance(raw, str):
            raise ValueError(f"Invalid e-mail address {raw!r}: not a string")
        address = raw.strip()
        if not _EMAIL_RE.match(address):
            raise ValueError(f"Invalid e-mail address {raw!r}")
        key = address.lower()
        if key not in seen:
            seen.add(key)
            out.append(address)
    return out


def _clean_optional_text(value: Optional[str], name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string, got {type(value).__name__}")
    value = value.strip()
    return value or None


def notify(
    title: str,
    body: Optional[str] = None,
    level: str = "info",
    email: EmailArg = None,
    link: Optional[str] = None,
) -> str:
    """Send a notification about the current run.

    The notification appears in the portal for the run's organization. When
    ``email`` is given, those addresses also receive it by e-mail.

    Call it from inside a step (an activity), like ``request_approval``: it
    posts the ``NOTIFY`` action to the run's supervisor webhook, with the same
    headers and retry policy.

    A notification is usually not worth failing a run over. If that is the
    case for yours, catch ``NotificationError``:

        try:
            notify("Relatório pronto", level="success")
        except NotificationError:
            logger.warning("notification not delivered", exc_info=True)

    Args:
        title: Short headline, required. Trimmed; at most 200 characters.
        body: Optional longer text.
        level: One of ``info``, ``success``, ``warning``, ``error``.
        email: One address or a list of addresses to e-mail as well. Stripped,
            validated and de-duplicated.
        link: Optional URL the notification points to: ``https://``,
            ``http://`` or a portal path starting with ``/``.

    Returns:
        The ``notification_id`` the portal assigned.

    Raises:
        ValueError: On an invalid title, level, address or link. Raised before
            any HTTP call.
        NotificationError: Outside an Ondemand run, when the webhook fails
            after all retries, or when the portal answers without a
            ``notification_id`` (an API that predates ``NOTIFY``).
    """
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title is required")
    title = title.strip()
    if len(title) > MAX_TITLE_LENGTH:
        raise ValueError(
            f"title is {len(title)} characters long; the maximum is {MAX_TITLE_LENGTH}"
        )
    if level not in NOTIFICATION_LEVELS:
        raise ValueError(
            f"Invalid notification level {level!r}; expected one of {', '.join(NOTIFICATION_LEVELS)}"
        )
    body = _clean_optional_text(body, "body")
    link = _clean_optional_text(link, "link")
    if link is not None and not (
        link.startswith(("https://", "http://")) or (link.startswith("/") and not link.startswith("//"))
    ):
        raise ValueError(f"Invalid link {link!r}: use an http(s) URL or a path starting with '/'")
    emails = normalize_emails(email)

    webhook_url = current_webhook_url()
    if not webhook_url:
        raise NotificationError(
            "No run context. Cannot send a notification outside of an Ondemand execution."
        )

    payload: Dict[str, Any] = {
        "client": "ondemand-python",
        "version": "2.0.0",
        "action": "NOTIFY",
        "payload": {
            "title": title,
            "body": body,
            "level": level,
            "email": emails,
            "link": link,
        },
    }

    def _handle(result: Dict[str, Any]) -> str:
        notification_id = result.get("notification_id")
        if not notification_id:
            raise NotificationError(
                "Portal returned success but no notification_id (does the API "
                f"support the NOTIFY action yet?): {result}"
            )
        logger.info("Notification sent (%s): %s", level, title)
        return str(notification_id)

    return post_webhook_with_retries(
        webhook_url,
        payload,
        handle=_handle,
        error_cls=NotificationError,
        what="Notification",
    )
