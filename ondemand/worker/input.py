"""
The workflow input contract — what the portal sends when it starts a robot.

Why this exists
---------------
Every robot used to redeclare this dataclass by hand, and they drifted. Before
this module existed, `ondemand-auto-demo` declared a `webhook_secret` the portal
never sends (it was `""` from day one and nobody noticed) and ignored
`process_code` and `organization_id`, while `GSE1-conciliacao-de-contas`
declared those two and not the secret. Two robots, two different contracts for
the same payload.

The cost showed up when the platform renamed `run_id` to `workflow_id` in August
2026: a change that belongs to the platform had to be made in every automation
repo. With the contract in one place, the next one does not — robots upgrade
`ondemand-ai` at container start, so they pick it up without a rebuild.

Usage
-----
Subclass it and add only what belongs to your robot::

    from ondemand.worker import WorkflowInput

    @dataclass
    class GSEInput(WorkflowInput):
        @property
        def period(self) -> str:
            return self.inputs.get("period", "")

Temporal needs a concrete type to deserialize into, so the robot still declares
and inherits. What goes away is the duplication of the platform's own fields.

A note on the defaults
----------------------
Every field here has a default, including `workflow_id`. That is not laziness:
Python 3.9 has no `kw_only` for dataclasses, and a field without a default
cannot follow one that has it — so a defaulted base field would make any
subclass field mandatory-by-position, which is worse. Give your own fields
defaults too, or use `inputs` and properties as above.
"""

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class WorkflowInput:
    """Fields the portal sends on every workflow start.

    Mirrors the object built in ondemand-infra `executor.js`. Anything the
    portal collected from the user arrives inside `inputs`, keyed by the
    process property name.
    """

    #: The Temporal Workflow ID — process_runs.id in the portal database. Not
    #: the Temporal Run ID, which identifies one execution of this workflow and
    #: is not sent (read it with `current_temporal_run_id()` when inside an
    #: activity).
    workflow_id: str = ""

    #: The process code, which is also the Temporal task queue.
    process_code: str = ""

    #: The organization that owns this run. Also the Temporal namespace.
    organization_id: str = ""

    #: Where to POST step reports, logs and artifacts.
    webhook_url: str = ""

    #: User-supplied values, keyed by process property name.
    inputs: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Tolerate an explicit null from the wire; robots index into this
        # without checking and a None here would surface far from the cause.
        if self.inputs is None:
            self.inputs = {}
