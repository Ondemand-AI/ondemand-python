"""
CLI argument parsing for the agent.

Provides consistent argument handling across all phases.
"""

import argparse
from typing import Tuple, Optional


def parse_args() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Parse command-line arguments.

    Returns:
        Tuple of (run_id, webhook_url, api_key) - all may be None for standalone mode
    """
    parser = argparse.ArgumentParser(
        description="Ondemand Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Ondemand run ID (for state isolation and reporting)",
    )

    parser.add_argument(
        "--webhook-url",
        type=str,
        default=None,
        help="Ondemand webhook URL for status updates",
    )

    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="API key for webhook authentication (or set ONDEMAND_API_KEY env var)",
    )

    args = parser.parse_args()

    return args.run_id, args.webhook_url, args.api_key
