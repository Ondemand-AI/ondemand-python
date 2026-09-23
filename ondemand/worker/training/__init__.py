"""
Generic LoRA fine-tuning workflow, shared by every client's training robot.

This is the first ``@workflow.defn`` that lives in the library instead of a
robot. A per-client training robot generates the dataset (client-specific), and
then starts ``TrainingWorkflow`` as a child on the ``training`` task queue with a
**job spec** — the dataset key, base model, GPU type and hyperparameters. The
workflow provisions a RunPod GPU, runs training in a training image, and lands
the adapter in R2. Nothing here is client-specific.

    from ondemand.worker.training import TrainingWorkflow, TrainingInput, training_activities
"""

from ondemand.worker.training.input import TrainingInput, TrainingResult
from ondemand.worker.training.workflow import TrainingWorkflow
from ondemand.worker.training.activities import run_training_job, training_activities

__all__ = [
    "TrainingWorkflow",
    "TrainingInput",
    "TrainingResult",
    "run_training_job",
    "training_activities",
]
