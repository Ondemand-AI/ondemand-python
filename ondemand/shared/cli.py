"""
CLI argument parsing and input retrieval for the agent.

Provides consistent argument handling across all phases.

Environment variables:
- SUPERVISOR_WEBHOOK_SECRET: Static API key for webhook authentication (set on worker)
- ONDEMAND_APP_URL: Base URL for the app (optional, defaults to https://app.ondemand-ai.com.br)
- ONDEMAND_INPUTS: JSON string with run inputs/parameters (set by worker per-job)

CLI arguments:
- --run-id: Run ID passed by worker (NOT from env var to avoid race conditions)
- --inputs: JSON string with inputs (for local testing)
- --inputs-file: Path to JSON file with inputs (for local testing)

Usage:
    # In worker (production):
    python src/process.py --run-id abc123
    # Inputs come from ONDEMAND_INPUTS env var

    # Local testing with inline JSON:
    python src/process.py --inputs '{"empresa": "Test Corp", "competencia": "2024-01"}'

    # Local testing with file:
    python src/process.py --inputs-file test_inputs.json
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Tuple, Optional, Dict, Any

# Default app URL
DEFAULT_APP_URL = "https://app.ondemand-ai.com.br"

# Cache for parsed inputs (avoid re-parsing)
_cached_inputs: Optional[Dict[str, Any]] = None
_cached_run_id: Optional[str] = None


def parse_args() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Parse command-line arguments and environment variables.

    Priority for each value:
    1. CLI arguments
    2. Environment variables

    The webhook_url is constructed from run_id if not explicitly provided.

    Returns:
        Tuple of (run_id, webhook_url, api_key) - all may be None for standalone mode
    """
    global _cached_run_id

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
        help="Ondemand webhook URL for status updates (constructed from run-id if not provided)",
    )

    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="API key for webhook authentication",
    )

    parser.add_argument(
        "--inputs",
        type=str,
        default=None,
        help="JSON string with run inputs (for local testing)",
    )

    parser.add_argument(
        "--inputs-file",
        type=str,
        default=None,
        help="Path to JSON file with run inputs (for local testing)",
    )

    args, _ = parser.parse_known_args()

    # Get run_id from CLI only (NOT env var - race conditions with parallel runs)
    run_id = args.run_id
    _cached_run_id = run_id

    # Get api_key from CLI or env (static, same for all runs)
    api_key = args.api_key or os.environ.get("SUPERVISOR_WEBHOOK_SECRET")

    # Get webhook_url from CLI, or construct from run_id
    webhook_url = args.webhook_url
    if not webhook_url and run_id:
        app_url = os.environ.get("ONDEMAND_APP_URL", DEFAULT_APP_URL)
        webhook_url = f"{app_url}/api/webhooks/supervisor/{run_id}"

    # Pre-parse inputs if provided via CLI (for local testing)
    if args.inputs or args.inputs_file:
        _parse_and_cache_inputs(args.inputs, args.inputs_file, run_id)

    return run_id, webhook_url, api_key


def get_inputs(save_to_file: bool = True) -> Dict[str, Any]:
    """
    Get run inputs/parameters.

    Priority:
    1. CLI --inputs argument (JSON string, for local testing)
    2. CLI --inputs-file argument (path to JSON file, for local testing)
    3. ONDEMAND_INPUTS environment variable (set by worker in production)

    Args:
        save_to_file: If True, saves inputs to inputs_received.json for auditing

    Returns:
        Dictionary with run inputs. Empty dict if no inputs provided.

    Example:
        from ondemand.shared.cli import get_inputs

        inputs = get_inputs()
        empresa = inputs.get("empresa", "Default Corp")
        competencia = inputs.get("competencia", "2024-01")
    """
    global _cached_inputs

    # Return cached if available
    if _cached_inputs is not None:
        return _cached_inputs

    # Try to get from CLI args first (may have been parsed already)
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", type=str, default=None)
    parser.add_argument("--inputs-file", type=str, default=None)
    parser.add_argument("--run-id", type=str, default=None)
    args, _ = parser.parse_known_args()

    run_id = args.run_id or _cached_run_id

    inputs = _parse_and_cache_inputs(args.inputs, args.inputs_file, run_id)

    # Save to file for auditing
    if save_to_file and inputs:
        _save_inputs_to_file(inputs, run_id)

    return inputs


def _parse_and_cache_inputs(
    cli_inputs: Optional[str],
    cli_inputs_file: Optional[str],
    run_id: Optional[str]
) -> Dict[str, Any]:
    """Parse inputs from various sources and cache them."""
    global _cached_inputs

    inputs = {}

    # Priority 1: CLI --inputs (inline JSON)
    if cli_inputs:
        try:
            inputs = json.loads(cli_inputs)
            print(f"[ondemand] Loaded inputs from --inputs argument")
        except json.JSONDecodeError as e:
            print(f"[ondemand] WARNING: Failed to parse --inputs JSON: {e}")

    # Priority 2: CLI --inputs-file (JSON file)
    elif cli_inputs_file:
        try:
            with open(cli_inputs_file, "r", encoding="utf-8") as f:
                inputs = json.load(f)
            print(f"[ondemand] Loaded inputs from file: {cli_inputs_file}")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"[ondemand] WARNING: Failed to load inputs file: {e}")

    # Priority 3: ONDEMAND_INPUTS env var (production)
    else:
        env_inputs = os.environ.get("ONDEMAND_INPUTS")
        if env_inputs:
            try:
                inputs = json.loads(env_inputs)
                print(f"[ondemand] Loaded inputs from ONDEMAND_INPUTS env var")
            except json.JSONDecodeError as e:
                print(f"[ondemand] WARNING: Failed to parse ONDEMAND_INPUTS: {e}")

    _cached_inputs = inputs
    return inputs


def _save_inputs_to_file(inputs: Dict[str, Any], run_id: Optional[str]) -> None:
    """Save inputs to a JSON file for auditing."""
    try:
        # Save to current directory (robot's base dir)
        output_dir = Path("output")
        output_dir.mkdir(exist_ok=True)

        filename = f"inputs_received_{run_id}.json" if run_id else "inputs_received.json"
        filepath = output_dir / filename

        audit_data = {
            "run_id": run_id,
            "received_at": datetime.utcnow().isoformat() + "Z",
            "inputs": inputs,
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(audit_data, f, indent=2, ensure_ascii=False)

        print(f"[ondemand] Inputs saved to {filepath}")

    except Exception as e:
        print(f"[ondemand] WARNING: Failed to save inputs to file: {e}")
