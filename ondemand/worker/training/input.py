"""
Job spec for the generic training workflow, and its result.

The dataset travels as an **R2 key**, never inline (Temporal history stays
small). The GPU and base model travel as **job-spec fields**, never baked into
the dataset — that is what lets the one workflow serve a bigger GPU or another
base model for a different job.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ondemand.worker.input import WorkflowInput

# The spike's final base model and hyperparameters (07-cost-and-deployment.md).
DEFAULT_BASE_MODEL = "unsloth/Qwen2.5-7B-Instruct-bnb-4bit"


@dataclass
class TrainingInput(WorkflowInput):
    """Job spec, read from the portal ``inputs`` dict.

    Platform fields (workflow_id, process_code, organization_id, webhook_url,
    inputs) come from WorkflowInput. Everything below is a property over
    ``inputs`` so the contract stays a plain dict on the wire.
    """

    @property
    def dataset_key(self) -> str:
        """R2 key of the training JSONL (alpaca format). Required."""
        return self.inputs.get("dataset_key", "")

    @property
    def client_code(self) -> str:
        """Ondemand client (accounting firm), e.g. 'gse'. For manifest + output path."""
        return self.inputs.get("client_code", "")

    @property
    def company_code(self) -> str:
        """Company within the client's portfolio. One adapter per company."""
        return self.inputs.get("company_code", "")

    @property
    def base_model(self) -> str:
        return self.inputs.get("base_model", DEFAULT_BASE_MODEL)

    @property
    def gpu_type(self) -> str:
        """RunPod GPU type id. VERIFY the exact id string against the account."""
        return self.inputs.get("gpu_type", "NVIDIA GeForce RTX 4090")

    @property
    def training_image(self) -> str:
        """Docker image for the training pod (Unsloth entrypoint)."""
        return self.inputs.get("training_image", "")

    @property
    def output_prefix(self) -> str:
        """R2 prefix for the adapter. Defaults to adapters/{client}/{company}/{workflow_id}."""
        explicit = self.inputs.get("output_prefix")
        if explicit:
            return explicit.rstrip("/")
        return f"adapters/{self.client_code}/{self.company_code}/{self.workflow_id}"

    @property
    def manifest_prefix(self) -> str:
        """Where the latest.json manifest points. adapters/{client}/{company}."""
        return f"adapters/{self.client_code}/{self.company_code}"

    @property
    def region(self) -> Optional[str]:
        return self.inputs.get("region")

    @property
    def volume_name(self) -> Optional[str]:
        """Optional network-volume cache name. If unset, no volume is attached."""
        return self.inputs.get("volume_name")

    @property
    def hyperparams(self) -> Dict[str, Any]:
        """Full LoRA/SFT hyperparameter set, with the spike's defaults.

        Every knob is overridable via ``inputs["hyperparams"]``; anything omitted
        keeps the default (the spike's proven values). This mirrors the surface
        Valkyrie exposes — same Unsloth LoRA method, all the dials. The whole dict
        is handed to the training pod as ``HYPERPARAMS_JSON``.
        """
        hp = {
            # LoRA
            "lora_r": 32,
            "lora_alpha": 64,
            "lora_dropout": 0.0,
            "target_modules": [
                "q_proj", "k_proj", "v_proj", "o_proj",
                "gate_proj", "up_proj", "down_proj",
            ],
            "use_rslora": False,
            # Schedule / duration
            "epochs": 5,
            "max_steps": -1,            # -1 = use epochs; >0 overrides epochs
            "learning_rate": 1e-4,
            "lr_scheduler_type": "cosine",
            "warmup_ratio": 0.03,
            "warmup_steps": 0,
            "weight_decay": 0.01,
            # Batch
            "per_device_train_batch_size": 4,
            "gradient_accumulation_steps": 4,
            # Optimizer / precision / misc
            "optim": "adamw_8bit",
            "seq_len": 2048,
            "seed": 3407,
            "logging_steps": 10,
            "packing": False,
            "gradient_checkpointing": True,
        }
        hp.update(self.inputs.get("hyperparams", {}))
        return hp


@dataclass
class TrainingResult:
    """What the workflow returns once the adapter is in R2."""

    status: str = "unknown"          # "success" | "failed"
    adapter_prefix: str = ""         # R2 prefix holding the adapter files
    base_model: str = ""
    manifest_key: str = ""           # R2 key of latest.json
    error: str = ""
    hyperparams: Dict[str, Any] = field(default_factory=dict)
