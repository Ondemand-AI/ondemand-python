"""The reviewer's answer to an approval request, as a workflow receives it.

When someone approves or rejects (in the portal, from an e-mail button or from
the tokenized URLs), the API sends the workflow a Temporal signal named
``human_approval`` whose argument is a dict:

    {"approved": bool, "comment": str | None, "resolved_by": str | None}

APIs before notifications sent only ``{"approved": bool}``; the other two keys
are simply absent there.

This module imports nothing but the standard library, so it is safe inside the
Temporal workflow sandbox:

    from temporalio import workflow

    with workflow.unsafe.imports_passed_through():
        from ondemand.shared.approval_decision import ApprovalDecision

    @workflow.defn
    class MyWorkflow:
        def __init__(self) -> None:
            self._decision: Optional[ApprovalDecision] = None

        @workflow.signal
        async def human_approval(self, data: dict) -> None:
            self._decision = ApprovalDecision.from_signal(data)

        @workflow.run
        async def run(self, input):
            ...
            await workflow.wait_condition(lambda: self._decision is not None)
            if not self._decision.approved:
                workflow.logger.info("Rejected: %s", self._decision.comment)
"""
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ApprovalDecision:
    """A typed view of the ``human_approval`` signal payload.

    Attributes:
        approved: True only when the reviewer approved.
        comment: What the reviewer wrote, if anything (None when empty).
        resolved_by: Who decided, as the API records it, if known.
    """

    approved: bool
    comment: Optional[str] = None
    resolved_by: Optional[str] = None

    @classmethod
    def from_signal(cls, data: Any) -> "ApprovalDecision":
        """Build a decision from the signal argument.

        Fails closed: anything other than ``approved`` set to the boolean
        ``True`` is a rejection. A bare boolean is accepted as ``approved``.
        Unknown keys are ignored, so a newer API cannot break an older robot.
        """
        if isinstance(data, bool):
            return cls(approved=data)
        if not isinstance(data, dict):
            return cls(approved=False)

        comment = data.get("comment")
        if comment is not None:
            comment = str(comment).strip() or None

        resolved_by = data.get("resolved_by")
        if resolved_by is not None:
            resolved_by = str(resolved_by).strip() or None

        return cls(
            approved=data.get("approved") is True,
            comment=comment,
            resolved_by=resolved_by,
        )
