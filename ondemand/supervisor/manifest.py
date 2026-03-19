"""
Manifest Helpers for Dynamic Manifests

Provides utilities for building and updating manifests at runtime,
enabling dynamic step names based on data discovered during execution.

Usage:
    from ondemand.supervisor import build_manifest_step, update_manifest

    # Build dynamic steps based on discovered data
    dynamic_steps = []
    for company in companies:
        step = build_manifest_step(
            step_id=company["id"],
            title=f"Process {company['name']}",
            children=[
                build_manifest_step(f"{company['id']}_extract", "Extract Data"),
                build_manifest_step(f"{company['id']}_validate", "Validate"),
            ]
        )
        dynamic_steps.append(step)

    # Update manifest and send to Ondemand
    update_manifest(dynamic_steps, parent_step_id="Process")
"""

import json
import logging
import pathlib
from collections import defaultdict
from pathlib import Path
from typing import List, Optional, Dict, Any, Union
from dataclasses import dataclass, field

import pydantic
import yaml
from pydantic import BaseModel, field_validator

from ..shared import get_base_output_dir

logger = logging.getLogger(__name__)
DYNAMIC_MANIFEST = 'dynamic_manifest.yaml'

StepId = str



class RecordStatusColumns(BaseModel):
    """Custom column labels for record statuses in the UI."""
    succeeded: Optional[str] = None
    failed: Optional[str] = None
    warning: Optional[str] = None

    def __json__(self):
        return {k: v for k, v in self.model_dump().items() if v is not None}


class Step(BaseModel):
    """A single step in the manifest workflow tree."""
    step_id: str
    title: str
    description: Optional[str] = None
    columns: Optional[RecordStatusColumns] = None
    steps: Optional[List["Step"]] = None

    @field_validator("steps", mode="before")
    @classmethod
    def ensure_steps_list(cls, v):
        return v or []

    def __json__(self):
        return {
            "step_id": self.step_id,
            "title": self.title,
            "description": self.description,
            "steps": [s.__json__() for s in (self.steps or [])],
            "columns": self.columns.__json__() if self.columns else self.columns,
        }


class Manifest(BaseModel):
    """A workflow manifest, typically loaded from manifest.yaml.

    Defines the step hierarchy for progress tracking in the UI.
    """
    uid: str
    name: str
    description: Optional[str] = None
    author: Optional[str] = None
    source: str = ""
    version: Optional[str] = None
    columns: Optional[RecordStatusColumns] = None
    workflow: List[Step] = []

    @classmethod
    def from_file(cls, filename: Union[str, pathlib.Path]) -> "Manifest":
        """Load a Manifest from a YAML file."""
        try:
            with open(filename) as f:
                data = yaml.safe_load(f)
            manifest = cls.model_validate(data)
            manifest.workflow = cls._load_steps(manifest.workflow)
            return manifest
        except pydantic.ValidationError as ex:
            errors = ex.errors()
            msg = f"Invalid manifest: {len(errors)} validation error(s)\n"
            msg += "\n".join(
                f"  {' -> '.join(str(l) for l in e['loc'])}: {e['msg']}"
                for e in errors
            )
            raise ValueError(msg) from ex

    @classmethod
    def _load_steps(cls, step_dicts: List[Union[dict, Step]]) -> List[Step]:
        """Parse step dicts into Step models with validation."""
        steps = []
        for sd in step_dicts:
            try:
                steps.append(Step.model_validate(sd) if isinstance(sd, dict) else sd)
            except pydantic.ValidationError:
                logger.exception("Invalid step in manifest")
        return steps

    def __json__(self) -> Dict[str, Any]:
        keys = ["uid", "name", "description", "author", "source", "columns"]
        result = {k: v for k, v in self.__dict__.items() if k in keys}
        result["workflow"] = [s.__json__() for s in self.workflow]
        return result

    def write_to_json_file(self, json_path: Union[str, pathlib.Path]) -> None:
        """Write the manifest as JSON."""
        with open(json_path, "w") as f:
            json.dump(self.__json__(), f)



@dataclass
class ManifestStep:
    """
    Represents a step in the manifest workflow.

    Attributes:
        step_id: Unique identifier for the step (used in step_scope/step decorator)
        title: Human-readable title shown in the UI
        description: Optional description of what the step does
        step_type: Type of step (default: "sequential")
        steps: Nested child steps
    """
    step_id: str
    title: str
    description: str = ""
    step_type: str = "sequential"
    steps: List["ManifestStep"] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for YAML/JSON serialization."""
        result = {
            "step_id": self.step_id,
            "title": self.title,
            "description": self.description,
            "step_type": self.step_type,
        }
        if self.steps:
            result["steps"] = [s.to_dict() for s in self.steps]
        else:
            result["steps"] = []
        return result


def _manifest_step_representer(dumper, step):
    """YAML representer for ManifestStep - converts to dict automatically."""
    return dumper.represent_dict(step.to_dict())


# Register representer so yaml.dump/safe_dump work automatically with ManifestStep
yaml.add_representer(ManifestStep, _manifest_step_representer)
yaml.SafeDumper.add_representer(ManifestStep, _manifest_step_representer)


def build_manifest_step(
    step_id: str,
    title: str,
    description: str = "",
    step_type: str = "sequential",
    children: Optional[List[ManifestStep]] = None,
) -> ManifestStep:
    """
    Build a manifest step with optional children.

    Args:
        step_id: Unique identifier (must match what you use in step_scope)
        title: Human-readable title for the UI
        description: What this step does
        step_type: Type of step (default: "sequential")
        children: Nested child steps

    Returns:
        ManifestStep object

    Example:
        # Simple step
        step = build_manifest_step("login", "Login to Portal")

        # Step with children
        company_step = build_manifest_step(
            "company_abc",
            "Process Company ABC",
            children=[
                build_manifest_step("company_abc_extract", "Extract Data"),
                build_manifest_step("company_abc_validate", "Validate"),
            ]
        )
    """
    return ManifestStep(
        step_id=step_id,
        title=title,
        description=description,
        step_type=step_type,
        steps=children or [],
    )


def load_manifest(manifest_path: Union[str, Path] = "manifest.yaml") -> Dict[str, Any]:
    """
    Load the base manifest from a YAML file.

    Args:
        manifest_path: Path to the manifest file

    Returns:
        Manifest as a dictionary
    """
    path = Path(manifest_path)
    if not path.is_absolute():
        # Try working directory first, then check common project root markers
        if not path.exists():
            # Walk up from this file's location to find the manifest
            search = Path.cwd()
            for _ in range(5):
                candidate = search / manifest_path
                if candidate.exists():
                    path = candidate
                    break
                search = search.parent
    with open(path, "r") as f:
        return yaml.safe_load(f)


def update_manifest(
    dynamic_steps: List[ManifestStep],
    parent_step_id: Optional[str] = None,
    manifest_path: Union[str, Path] = "manifest.yaml",
    send_to_ondemand: bool = True,
) -> Dict[str, Any]:
    """
    Update the manifest with dynamic steps and optionally send to Ondemand.

    This function:
    1. Loads the base manifest
    2. Finds the parent step (if specified) or appends to workflow root
    3. Adds the dynamic steps as children
    4. Sends the updated manifest to Ondemand

    Args:
        dynamic_steps: List of ManifestStep objects to add
        parent_step_id: The step_id of the parent to add children to.
                        If None, appends to workflow root.
        manifest_path: Path to the base manifest file
        send_to_ondemand: Whether to send the updated manifest to Ondemand

    Returns:
        The updated manifest dictionary

    Example:
        # Build dynamic steps based on data
        companies = ["ABC Corp", "XYZ Inc", "123 Ltd"]
        dynamic_steps = []

        for company in companies:
            company_id = company.lower().replace(" ", "_")
            step = build_manifest_step(
                step_id=company_id,
                title=f"Process {company}",
                children=[
                    build_manifest_step(f"{company_id}_extract", "Extract Data"),
                    build_manifest_step(f"{company_id}_validate", "Validate"),
                    build_manifest_step(f"{company_id}_upload", "Upload Results"),
                ]
            )
            dynamic_steps.append(step)

        # Update manifest with dynamic steps under "process" parent
        update_manifest(dynamic_steps, parent_step_id="process")
    """
    # Load base manifest
    try:
        manifest = load_manifest(manifest_path)
    except FileNotFoundError:
        import os
        if not os.environ.get("ONDEMAND_JOB_ID"):
            logger.warning("manifest.yaml not found — skipping manifest update (running locally)")
            return {"workflow": [s.to_dict() for s in dynamic_steps]}
        raise

    # Convert steps to dict format
    steps_dict = [s.to_dict() for s in dynamic_steps]

    if parent_step_id:
        # Find the parent step and add children
        _add_steps_to_parent(manifest["workflow"], parent_step_id, steps_dict)
    else:
        # Append to workflow root
        manifest["workflow"].extend(steps_dict)

    # Save to base output directory (shared across tasks)
    output_file = get_base_output_dir() / DYNAMIC_MANIFEST
    with open(output_file, "w") as f:
        yaml.safe_dump(manifest, f, default_flow_style=False, allow_unicode=True)
    logger.info(f"Dynamic manifest saved to {output_file}")

    # Send to Ondemand if connected
    if send_to_ondemand:
        from .connector import send_manifest
        send_manifest(manifest)

    logger.info(f"Manifest updated with {len(dynamic_steps)} dynamic steps")
    return manifest


def _add_steps_to_parent(
    workflow: List[Dict[str, Any]],
    parent_step_id: str,
    steps_to_add: List[Dict[str, Any]],
) -> bool:
    """
    Recursively find parent step and add children.

    Returns True if parent was found and steps were added.
    """
    for step in workflow:
        if step.get("step_id") == parent_step_id:
            # Found parent - add steps as children
            if "steps" not in step:
                step["steps"] = []
            step["steps"].extend(steps_to_add)
            return True

        # Recursively search children
        if "steps" in step and step["steps"]:
            if _add_steps_to_parent(step["steps"], parent_step_id, steps_to_add):
                return True

    return False


def build_dynamic_manifest(
    base_manifest_path: Union[str, Path],
    output_path: Union[str, Path],
    dynamic_steps: List[ManifestStep],
    parent_step_id: Optional[str] = None,
) -> Path:
    """
    Build a complete dynamic manifest file.

    This is useful if you need to save the manifest to disk
    (e.g., for Robocorp storage upload).

    Args:
        base_manifest_path: Path to the base manifest
        output_path: Where to save the dynamic manifest
        dynamic_steps: Dynamic steps to add
        parent_step_id: Parent step to add children to

    Returns:
        Path to the generated manifest file
    """
    manifest = load_manifest(base_manifest_path)
    steps_dict = [s.to_dict() for s in dynamic_steps]

    if parent_step_id:
        _add_steps_to_parent(manifest["workflow"], parent_step_id, steps_dict)
    else:
        manifest["workflow"].extend(steps_dict)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with open(output, "w") as f:
        yaml.safe_dump(manifest, f, default_flow_style=False, allow_unicode=True)

    logger.info(f"Dynamic manifest saved to {output}")
    return output
