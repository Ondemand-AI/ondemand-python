"""
CloudFlare R2 Storage for Ondemand Artifacts

This module provides functionality to upload artifacts to CloudFlare R2
at the end of a run. R2 credentials should be set in environment variables:
- R2_ENDPOINT: CloudFlare R2 endpoint URL
- R2_ACCESS_KEY: Access key ID
- R2_SECRET_KEY: Secret access key
- R2_BUCKET: Bucket name
"""

import os
import logging
import mimetypes
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Try to import boto3, but don't fail if not installed
try:
    import boto3
    from botocore.exceptions import ClientError
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False
    logger.warning("boto3 not installed. Artifact upload to R2 will not be available.")


class R2StorageClient:
    """Client for uploading artifacts to CloudFlare R2."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        bucket: Optional[str] = None,
    ):
        """
        Initialize R2 storage client.

        Args:
            endpoint: R2 endpoint URL (defaults to R2_ENDPOINT env var)
            access_key: Access key ID (defaults to R2_ACCESS_KEY env var)
            secret_key: Secret access key (defaults to R2_SECRET_KEY env var)
            bucket: Bucket name (defaults to R2_BUCKET env var)
        """
        self.endpoint = endpoint or os.environ.get("R2_ENDPOINT")
        self.access_key = access_key or os.environ.get("R2_ACCESS_KEY")
        self.secret_key = secret_key or os.environ.get("R2_SECRET_KEY")
        self.bucket = bucket or os.environ.get("R2_BUCKET")
        self._client = None

    def is_configured(self) -> bool:
        """Check if R2 storage is properly configured."""
        return all([
            BOTO3_AVAILABLE,
            self.endpoint,
            self.access_key,
            self.secret_key,
            self.bucket,
        ])

    def _get_client(self):
        """Get or create the S3 client for R2."""
        if self._client is None:
            if not BOTO3_AVAILABLE:
                raise RuntimeError("boto3 is not installed. Run: pip install boto3")

            if not self.is_configured():
                missing = []
                if not self.endpoint:
                    missing.append("R2_ENDPOINT")
                if not self.access_key:
                    missing.append("R2_ACCESS_KEY")
                if not self.secret_key:
                    missing.append("R2_SECRET_KEY")
                if not self.bucket:
                    missing.append("R2_BUCKET")
                raise RuntimeError(f"R2 storage not configured. Missing: {', '.join(missing)}")

            from botocore.config import Config
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name="auto",
                config=Config(
                    connect_timeout=10,
                    read_timeout=50,
                    retries={"max_attempts": 1},
                ),
            )
        return self._client

    def _get_mime_type(self, file_path: Path) -> str:
        """Detect MIME type from file extension."""
        mime_type, _ = mimetypes.guess_type(str(file_path))
        return mime_type or "application/octet-stream"

    def download_file(
        self,
        key: str,
        dest_path: Path,
    ) -> Dict[str, Any]:
        """
        Download a single file from R2.

        Args:
            key: S3 key (path in bucket)
            dest_path: Local path to save the file

        Returns:
            Dict with download result (key, filename, size)
        """
        client = self._get_client()

        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        client.download_file(self.bucket, key, str(dest_path))

        file_size = dest_path.stat().st_size
        logger.debug(f"Downloaded r2://{self.bucket}/{key} to {dest_path}")

        return {
            "key": key,
            "filename": dest_path.name,
            "size": file_size,
        }

    def upload_file(
        self,
        file_path: Path,
        key: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Upload a single file to R2.

        Args:
            file_path: Local path to the file
            key: S3 key (path in bucket)
            metadata: Optional metadata to attach to the object

        Returns:
            Dict with upload result (key, size, mime_type)
        """
        client = self._get_client()

        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        mime_type = self._get_mime_type(file_path)
        file_size = file_path.stat().st_size

        extra_args = {
            "ContentType": mime_type,
        }
        if metadata:
            extra_args["Metadata"] = metadata

        client.upload_file(
            str(file_path),
            self.bucket,
            key,
            ExtraArgs=extra_args,
        )

        logger.debug(f"Uploaded {file_path} to r2://{self.bucket}/{key}")

        return {
            "key": key,
            "filename": file_path.name,
            "size": file_size,
            "mime_type": mime_type,
        }

    def copy_object(
        self,
        source_key: str,
        dest_key: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Copy an object within the same bucket.

        Args:
            source_key: Source S3 key
            dest_key: Destination S3 key
            metadata: Optional new metadata (replaces source metadata if provided)

        Returns:
            Dict with copy result (source_key, dest_key)
        """
        client = self._get_client()

        copy_source = {"Bucket": self.bucket, "Key": source_key}
        extra_args = {}
        if metadata:
            extra_args["Metadata"] = metadata
            extra_args["MetadataDirective"] = "REPLACE"

        client.copy_object(
            Bucket=self.bucket,
            CopySource=copy_source,
            Key=dest_key,
            **extra_args,
        )

        logger.debug(f"Copied r2://{self.bucket}/{source_key} -> {dest_key}")

        return {
            "source_key": source_key,
            "dest_key": dest_key,
        }

    def upload_directory(
        self,
        local_dir: Path,
        prefix: str,
        run_id: str,
    ) -> List[Dict[str, Any]]:
        """
        Upload all files from a directory to R2, preserving folder structure.

        Args:
            local_dir: Local directory to upload
            prefix: Prefix for S3 keys (e.g., "artifacts")
            run_id: Run ID for organizing artifacts

        Returns:
            List of uploaded file info dicts
        """
        local_dir = Path(local_dir)
        if not local_dir.exists():
            logger.warning(f"Directory not found: {local_dir}")
            return []

        uploaded_files = []

        # Walk through all files in the directory
        for file_path in local_dir.rglob("*"):
            if file_path.is_file():
                # Create relative path from local_dir
                relative_path = file_path.relative_to(local_dir)

                # Build S3 key: prefix/run_id/relative_path
                key = f"{prefix}/{run_id}/{relative_path}".replace("\\", "/")

                try:
                    result = self.upload_file(
                        file_path,
                        key,
                        metadata={
                            "run-id": run_id,
                            "original-path": str(relative_path),
                        },
                    )
                    # Add folder info for UI display
                    result["folder"] = str(relative_path.parent) if relative_path.parent != Path(".") else ""
                    uploaded_files.append(result)
                    logger.info(f"Uploaded artifact: {key} ({result['size']} bytes)")
                except Exception as e:
                    logger.error(f"Failed to upload {file_path}: {e}")

        return uploaded_files


# Global instance
_r2_client: Optional[R2StorageClient] = None


def get_r2_client() -> R2StorageClient:
    """Get the global R2 storage client instance."""
    global _r2_client
    if _r2_client is None:
        _r2_client = R2StorageClient()
    return _r2_client


def upload_run_artifacts(
    output_dir: Path,
    run_id: str,
    prefix: str = "artifacts",
) -> List[Dict[str, Any]]:
    """
    Upload all artifacts from a run's output directory to R2.

    Args:
        output_dir: Base output directory containing task folders
        run_id: Run ID
        prefix: S3 key prefix (default: "artifacts")

    Returns:
        List of uploaded file info dicts
    """
    client = get_r2_client()

    if not client.is_configured():
        logger.warning("R2 storage not configured. Skipping artifact upload.")
        return []

    logger.info(f"Uploading artifacts from {output_dir} to R2...")

    try:
        uploaded = client.upload_directory(output_dir, prefix, run_id)
        logger.info(f"Successfully uploaded {len(uploaded)} artifacts to R2")
        return uploaded
    except Exception as e:
        logger.error(f"Failed to upload artifacts to R2: {e}")
        return []


def upload_task_artifacts(
    task_output_dir: Path,
    run_id: str,
    task_name: str,
    prefix: str = "artifacts",
    exclude: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Upload artifacts from a specific task's output directory to R2.

    This is called after each task completes to enable incremental artifact uploads.

    Args:
        task_output_dir: Task-specific output directory (e.g., output/{run_id}/{task}/)
        run_id: Run ID
        task_name: Name of the task (used for folder organization)
        prefix: S3 key prefix (default: "artifacts")
        exclude: List of filenames to skip (e.g., ["console.txt"])

    Returns:
        List of uploaded file info dicts
    """
    client = get_r2_client()
    exclude_set = set(exclude or [])

    if not client.is_configured():
        logger.warning("R2 storage not configured. Skipping task artifact upload.")
        return []

    task_output_dir = Path(task_output_dir)
    if not task_output_dir.exists():
        logger.debug(f"Task output directory not found: {task_output_dir}")
        return []

    uploaded_files = []

    # Walk through all files in the task directory
    for file_path in task_output_dir.rglob("*"):
        if file_path.is_file():
            if file_path.name in exclude_set:
                logger.debug(f"Skipping excluded file: {file_path.name}")
                continue
            # Create relative path from task_output_dir
            relative_path = file_path.relative_to(task_output_dir)

            # Build S3 key: prefix/run_id/task_name/relative_path
            key = f"{prefix}/{run_id}/{task_name}/{relative_path}".replace("\\", "/")

            try:
                result = client.upload_file(
                    file_path,
                    key,
                    metadata={
                        "run-id": run_id,
                        "task": task_name,
                        "original-path": str(relative_path),
                    },
                )
                # Add folder info for UI display (task_name/subfolder)
                if relative_path.parent != Path("."):
                    result["folder"] = f"{task_name}/{relative_path.parent}".replace("\\", "/")
                else:
                    result["folder"] = task_name
                uploaded_files.append(result)
                logger.info(f"Uploaded task artifact: {key} ({result['size']} bytes)")
            except Exception as e:
                logger.error(f"Failed to upload {file_path}: {e}")

    if uploaded_files:
        logger.info(f"Successfully uploaded {len(uploaded_files)} artifacts for task '{task_name}'")

    return uploaded_files


def upload_root_artifacts(
    base_output_dir: Path,
    run_id: str,
    prefix: str = "artifacts",
    exclude: Optional[List[str]] = None,
    skip_subdirs: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """
    Upload shared files from the run's output directory to R2.

    Recursively walks base_output_dir, uploading all files except those
    in task output directories (already uploaded by upload_task_artifacts)
    and excluded filenames.

    Args:
        base_output_dir: Base output directory (e.g., output/{run_id}/)
        run_id: Run ID
        prefix: S3 key prefix (default: "artifacts")
        exclude: List of filenames to skip
        skip_subdirs: Set of immediate subdirectory names to skip
                      (e.g., task output dirs already uploaded separately)

    Returns:
        List of uploaded file info dicts
    """
    client = get_r2_client()
    exclude_set = set(exclude or [])
    skip_set = skip_subdirs or set()

    if not client.is_configured():
        logger.warning("R2 storage not configured. Skipping root artifact upload.")
        return []

    base_output_dir = Path(base_output_dir)
    if not base_output_dir.exists():
        logger.debug(f"Base output directory not found: {base_output_dir}")
        return []

    uploaded_files = []

    for file_path in base_output_dir.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path.name in exclude_set:
            continue

        # Skip files inside task output directories (uploaded separately)
        relative = file_path.relative_to(base_output_dir)
        if len(relative.parts) > 1 and relative.parts[0] in skip_set:
            continue

        key = f"{prefix}/{run_id}/{relative}".replace("\\", "/")

        try:
            result = client.upload_file(
                file_path,
                key,
                metadata={
                    "run-id": run_id,
                    "original-path": str(relative),
                },
            )
            # Folder for UI display
            if relative.parent != Path("."):
                result["folder"] = str(relative.parent).replace("\\", "/")
            else:
                result["folder"] = ""
            uploaded_files.append(result)
            logger.info(f"Uploaded root artifact: {key} ({result['size']} bytes)")
        except Exception as e:
            logger.error(f"Failed to upload {file_path}: {e}")

    if uploaded_files:
        logger.info(f"Successfully uploaded {len(uploaded_files)} root artifacts")

    return uploaded_files


def download_input_files(
    inputs: Dict[str, Any],
    dest_dir: Path,
    run_id: Optional[str] = None,
    artifacts_prefix: str = "artifacts",
) -> Dict[str, Any]:
    """
    Download all file-type inputs from R2 to a local directory.

    Scans the inputs dict for values that look like R2 storage keys
    (starting with 'inputs/') and downloads each to dest_dir.

    If run_id is provided, also copies each file to the artifacts folder
    (artifacts/{run_id}/inputs/{filename}) so they appear in the run's
    artifacts tab. The temp originals are kept for retry support.

    Args:
        inputs: The ONDEMAND_INPUTS dict (or subset)
        dest_dir: Local directory to save downloaded files
        run_id: Optional run ID for copying files to artifacts
        artifacts_prefix: R2 key prefix for artifacts (default: "artifacts")

    Returns:
        Dict mapping input key names to their local file paths
    """
    client = get_r2_client()

    if not client.is_configured():
        logger.warning("R2 storage not configured. Cannot download input files.")
        return {}

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    downloaded: Dict[str, Path] = {}

    for key, value in inputs.items():
        # Normalize to list of storage keys (supports single string or array)
        storage_keys: List[str] = []
        if isinstance(value, str) and value.startswith("inputs/"):
            storage_keys = [value]
        elif isinstance(value, list):
            storage_keys = [v for v in value if isinstance(v, str) and v.startswith("inputs/")]

        if not storage_keys:
            continue

        downloaded_paths: List[Path] = []
        for storage_key in storage_keys:
            # Extract original filename from the storage key
            filename = storage_key.rsplit("/", 1)[-1] if "/" in storage_key else storage_key
            local_path = dest_dir / filename

            try:
                result = client.download_file(storage_key, local_path)
                downloaded_paths.append(local_path)
                logger.info(f"Downloaded input '{key}': {filename} ({result['size']} bytes)")

                # Copy to artifacts folder for visibility in the UI
                if run_id:
                    try:
                        dest_key = f"{artifacts_prefix}/{run_id}/inputs/{filename}"
                        client.copy_object(
                            source_key=storage_key,
                            dest_key=dest_key,
                            metadata={
                                "run-id": run_id,
                                "input-key": key,
                                "original-path": filename,
                            },
                        )
                        logger.info(f"Copied input to artifacts: {dest_key}")
                    except Exception as e:
                        logger.warning(f"Failed to copy input '{key}' to artifacts: {e}")

            except Exception as e:
                logger.error(f"Failed to download input '{key}' from {storage_key}: {e}")

        # Single file returns Path, multiple files returns list of Paths
        if len(downloaded_paths) == 1:
            downloaded[key] = downloaded_paths[0]
        elif len(downloaded_paths) > 1:
            downloaded[key] = downloaded_paths  # type: ignore[assignment]

    return downloaded
