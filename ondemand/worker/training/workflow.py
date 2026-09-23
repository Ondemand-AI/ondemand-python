"""
The generic training workflow.

Thin orchestration: a single long activity provisions the GPU, runs the
fine-tune and publishes the adapter. Kept as one activity so the RunPod teardown
guarantee lives with the provisioning (the client's context manager), instead of
being split across activities where a crash between them would leak a GPU.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from ondemand.worker.training.input import TrainingInput, TrainingResult
    from ondemand.worker.training.activities import run_training_job

# Training is long; retry sparingly (each attempt spends GPU) and let the marker
# poll inside the activity own the wait. maximum_attempts=2 gives one retry for a
# transient provisioning error without re-billing training many times over.
TRAINING_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=2,
)

# Comfortably above the worker auto-heartbeat interval (~10s).
HEARTBEAT_TIMEOUT = timedelta(minutes=2)


@workflow.defn
class TrainingWorkflow:
    @workflow.run
    async def run(self, input: TrainingInput) -> TrainingResult:
        return await workflow.execute_activity(
            run_training_job,
            args=[input],
            # Bounds the total training wait (provision + train + publish).
            start_to_close_timeout=timedelta(hours=2),
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=TRAINING_RETRY,
        )
