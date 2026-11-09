import os
import zipfile
from pathlib import Path

root = Path(r"C:\Users\BHAVESH KHULLAR\Downloads\SOLOEARN_v0.1\SOLOEARN")
out_zips = [
    Path(r"C:\Users\BHAVESH KHULLAR\Desktop\SOLOEARN_Milestone6.2_Ready.zip"),
    Path(r"C:\Users\BHAVESH KHULLAR\Downloads\SOLOEARN_Milestone6.2_Ready.zip"),
    Path(r"C:\Users\BHAVESH KHULLAR\Downloads\SOLOEARN_v0.1\SOLOEARN_Milestone6.2_Ready.zip"),
    Path(r"C:\Users\BHAVESH KHULLAR\Desktop\SOLOEARN_Milestone6.1.2_Ready.zip"),
    Path(r"C:\Users\BHAVESH KHULLAR\Downloads\SOLOEARN_Milestone6.1.2_Ready.zip"),
    Path(r"C:\Users\BHAVESH KHULLAR\Downloads\SOLOEARN_v0.1\SOLOEARN_Milestone6.1.2_Ready.zip"),
]

exclude_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", ".streamlit"}
exclude_extensions = {".pyc", ".pyo", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm"}


def get_files_to_pack():
    files = []
    for dirpath, dirs, filenames in os.walk(root):
        # Prune excluded directories immediately
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        cur_path = Path(dirpath)
        rel_dir = cur_path.relative_to(root)

        # Prune workspaces directory contents except .gitkeep
        if len(rel_dir.parts) >= 1 and rel_dir.parts[0] == "workspaces":
            dirs.clear()
            if ".gitkeep" in filenames:
                files.append((cur_path / ".gitkeep", "workspaces/.gitkeep"))
            continue

        # Prune data/invoices directory contents except .gitkeep
        if len(rel_dir.parts) >= 2 and rel_dir.parts[0] == "data" and rel_dir.parts[1] == "invoices":
            dirs.clear()
            if ".gitkeep" in filenames:
                files.append((cur_path / ".gitkeep", "data/invoices/.gitkeep"))
            continue

        for f in filenames:
            f_path = cur_path / f
            # Skip symlinks
            if f_path.is_symlink():
                continue
            if f in {".env"} or f_path.suffix in exclude_extensions or f.endswith(".zip"):
                continue
            rel = f_path.relative_to(root)
            files.append((f_path, rel.as_posix()))
    return sorted(files, key=lambda x: x[1])


files_to_pack = get_files_to_pack()
print(f"Collected {len(files_to_pack)} files to package.")

for out_zip in out_zips:
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for f_path, arc_subpath in files_to_pack:
            arcname = f"SOLOEARN/{arc_subpath}"
            zf.write(f_path, arcname)
    test_result = zipfile.ZipFile(out_zip, "r").testzip()
    print(f"Created {out_zip} ({out_zip.stat().st_size} bytes), testzip={test_result}")
