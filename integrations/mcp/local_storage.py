"""Local filesystem storage adapter for attachments with safe path traversal protection."""
import os
import re
import uuid
from pathlib import Path

# Project data directory
_env_dir = os.environ.get("STORAGE_LOCAL_DIR")
DATA_ROOT = (
    Path(_env_dir)
    if _env_dir
    else Path(__file__).resolve().parent.parent.parent / "data" / "attachments"
)


def sanitize_filename(name: str) -> str:
    cleaned = Path(name).name
    # Keep alphanumeric, dot, dash, underscore
    cleaned = re.sub(r"[^\w\.\-]", "_", cleaned)
    return cleaned or "file"


class LocalStorage:
    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = root_dir or DATA_ROOT
        try:
            self.root_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.root_dir = Path("/tmp/pmt_attachments")  # noqa: S108
            self.root_dir.mkdir(parents=True, exist_ok=True)

    def save_file(
        self, workspace_id: str, issue_id: str, filename: str, content: bytes
    ) -> tuple[str, str]:
        safe_name = sanitize_filename(filename)
        stored_filename = f"{uuid.uuid4().hex[:12]}_{safe_name}"

        target_dir = self.root_dir / workspace_id / issue_id
        target_dir.mkdir(parents=True, exist_ok=True)

        target_file = target_dir / stored_filename
        target_file.write_bytes(content)

        relative_path = str(target_file.relative_to(self.root_dir))
        return relative_path, stored_filename

    def read_file(self, storage_path: str) -> bytes:
        # Prevent path traversal
        clean_path = os.path.normpath(storage_path).lstrip("/\\")
        target_file = (self.root_dir / clean_path).resolve()
        if not str(target_file).startswith(str(self.root_dir.resolve())):
            raise ValueError("Invalid storage path: path traversal detected.")
        if not target_file.is_file():
            raise FileNotFoundError(f"File not found: {storage_path}")
        return target_file.read_bytes()

    def delete_file(self, storage_path: str) -> bool:
        clean_path = os.path.normpath(storage_path).lstrip("/\\")
        target_file = (self.root_dir / clean_path).resolve()
        if not str(target_file).startswith(str(self.root_dir.resolve())):
            raise ValueError("Invalid storage path: path traversal detected.")
        if target_file.is_file():
            target_file.unlink()
            return True
        return False
