from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = PROJECT_ROOT / "data"
WORKSPACES_DIR = PROJECT_ROOT / "workspaces"
LEADS_DROPZONE_DIR = DATA_DIR / "leads_dropzone"
UPI_QR_DIR = DATA_DIR / "upi_qr"
DB_PATH = DATA_DIR / "soloearn.sqlite3"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b-instruct")
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "90"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    LEADS_DROPZONE_DIR.mkdir(parents=True, exist_ok=True)
    UPI_QR_DIR.mkdir(parents=True, exist_ok=True)


def safe_workspace_path(*parts: str | Path) -> Path:
    """Resolve paths only inside the project's workspaces directory.
    Normalizes both Windows (\\) and POSIX (/) separators and rejects path traversal and symlink escapes."""
    ensure_directories()
    root = WORKSPACES_DIR.resolve()
    if root.is_symlink():
        raise ValueError("Workspaces root directory cannot be a symbolic link")

    clean_parts: list[str] = []
    for p in parts:
        p_str = str(p).strip()
        as_path = Path(p_str)
        if as_path.is_absolute():
            if as_path.is_symlink():
                raise ValueError("Workspace symlink escape blocked")
            for parent in as_path.parents:
                if parent == root:
                    break
                if parent.is_symlink():
                    raise ValueError("Workspace symlink escape blocked")
            candidate = as_path.resolve()
            if candidate != root and root not in candidate.parents:
                raise ValueError("Workspace path escapes the approved workspace directory")
            if candidate.is_symlink():
                raise ValueError("Workspace symlink escape blocked")
            return candidate

        # Normalize both Windows and POSIX path separators portably
        norm = p_str.replace("\\", "/")
        norm_segs = [seg for seg in norm.split("/") if seg and seg != "."]
        if ".." in norm_segs:
            raise ValueError("Workspace path escapes the approved workspace directory")
        clean_parts.extend(norm_segs)

    raw_candidate = root.joinpath(*clean_parts)
    if raw_candidate.is_symlink():
        raise ValueError("Workspace symlink escape blocked")
    for parent in raw_candidate.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("Workspace symlink escape blocked")

    candidate = raw_candidate.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Workspace path escapes the approved workspace directory")
    if candidate.is_symlink():
        raise ValueError("Workspace symlink escape blocked")
    return candidate
