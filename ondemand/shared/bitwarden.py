"""
Bitwarden wrapper for Ondemand workers.

Thin facade over t_vault that suppresses transient urllib3 connection warnings
produced while the Bitwarden CLI server is starting up (expected during bw_connect).
Robots should import from here instead of importing t_vault directly.
"""

import contextlib
import logging
from typing import Any


@contextlib.contextmanager
def _quiet_urllib3():
    """Raise urllib3 log level to ERROR for the duration of the block."""
    loggers = [
        logging.getLogger("urllib3"),
        logging.getLogger("urllib3.connectionpool"),
    ]
    original = [(lg, lg.level) for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.ERROR)
    try:
        yield
    finally:
        for lg, level in original:
            lg.setLevel(level)


def bw_connect() -> None:
    """Connect and unlock Bitwarden using BW_CLIENTID / BW_CLIENTSECRET / BW_PASSWORD env vars.

    Suppresses the urllib3 connection-refused warnings that appear while the
    Bitwarden CLI server is warming up — those retries are expected and handled
    internally by t_vault.
    """
    from t_vault import bw_login_from_env
    with _quiet_urllib3():
        bw_login_from_env()


def bw_get_item(item_name: str, collection_name: str | None = None) -> Any:
    """Get a vault item by name. Requires bw_connect() to have been called first."""
    from t_vault import bw_get_item as _bw_get_item
    return _bw_get_item(item_name, collection_name)
