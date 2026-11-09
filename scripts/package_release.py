import os
import zipfile
from pathlib import Path

root = Path(r"C:\Users\BHAVESH KHULLAR\Downloads\SOLOEARN_v0.1\SOLOEARN")
out_zips = [
    Path(r"C:\Users\BHAVESH KHULLAR\Desktop\SOLOEARN_Milestone6.1_Ready.zip"),
    Path(r"C:\Users\BHAVESH KHULLAR\Downloads\SOLOEARN_Milestone6.1_Ready.zip"),
    Path(r"C:\Users\BHAVESH KHULLAR\Downloads\SOLOEARN_v0.1\SOLOEARN_Milestone6.1_Ready.zip"),
    Path(r"C:\Users\BHAVESH KHULLAR\Desktop\SOLOEARN_Milestone6_Ready.zip"),
    Path(r"C:\Users\BHAVESH KHULLAR\Downloads\SOLOEARN_Milestone6_Ready.zip"),
    Path(r"C:\Users\BHAVESH KHULLAR\Downloads\SOLOEARN_v0.1\SOLOEARN_Milestone6_Ready.zip"),
]

exclude_dirs = {".git", ".venv", "__pycache__", ".pytest_cache"}
exclude_extensions = {".pyc", ".pyo", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm"}

def should_exclude(rel_path: Path):
    parts = rel_path.parts
    if any(p in exclude_dirs for p in parts):
        return True
    if rel_path.name in {".env"} or rel_path.suffix in exclude_extensions:
        return True
    if len(parts) >= 2 and parts[0] == "workspaces" and parts[1] != ".gitkeep":
        return True
    if len(parts) >= 3 and parts[0] == "data" and parts[1] == "invoices" and parts[2] != ".gitkeep":
        return True
    if len(parts) >= 2 and parts[0] == "data" and rel_path.name.endswith(".sqlite3"):
        return True
    if rel_path.name.endswith(".zip"):
        return True
    return False

for out_zip in out_zips:
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(root.rglob("*")):
            # Skip any symbolic link or files within symlinked directories
            if file_path.is_symlink():
                continue
            if any(p.is_symlink() for p in file_path.parents if p != root and root in p.parents):
                continue
            if file_path.is_file():
                rel = file_path.relative_to(root)
                if not should_exclude(rel):
                    arcname = f"SOLOEARN/{rel.as_posix()}"
                    zf.write(file_path, arcname)
    test_result = zipfile.ZipFile(out_zip, "r").testzip()
    print(f"Created {out_zip} ({out_zip.stat().st_size} bytes), testzip={test_result}")
