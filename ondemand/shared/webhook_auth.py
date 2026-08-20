"""Authentication headers for calls to the Ondemand supervisor webhook.

Every sender in this library posts to `/api/webhooks/supervisor/{run_id}`, but
only `request_approval` ever sent an Authorization header — step reports, log
streaming and artifact notifications sent none. The API side could therefore
never enforce authentication without breaking three of the four senders, so it
was left opt-in and never switched on.

The secret also went by different names on either side of the wire
(ONDEMAND_WEBHOOK_SECRET here, SUPERVISOR_WEBHOOK_SECRET in the worker config),
which is how it stayed unset everywhere without anyone noticing.

One name, one place to read it, one place to build the headers.
"""

import os

#: Environment variable holding the shared secret. The API accepts it as
#: `Authorization: Bearer <secret>` or `x-webhook-secret`.
WEBHOOK_SECRET_ENV = "ONDEMAND_WEBHOOK_SECRET"


def webhook_headers(extra: dict | None = None) -> dict:
    """Build headers for a supervisor webhook request.

    The Authorization header is omitted when no secret is configured, which
    keeps local runs and unauthenticated environments working. The API decides
    whether to require it.
    """
    headers = {"Content-Type": "application/json"}

    secret = os.environ.get(WEBHOOK_SECRET_ENV, "")
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    if extra:
        headers.update(extra)

    return headers
