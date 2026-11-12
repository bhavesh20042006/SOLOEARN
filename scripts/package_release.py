import base64
import hashlib
import os
import zipfile
from pathlib import Path

root = Path(r"C:\Users\BHAVESH KHULLAR\Downloads\SOLOEARN_v0.1\SOLOEARN")
brain_dir = Path(r"C:\Users\BHAVESH KHULLAR\.gemini\antigravity\brain\ff0a42bf-3d56-447a-a07a-9b0c7434e21f")

out_zips = [
    Path(r"C:\Users\BHAVESH KHULLAR\Desktop\SOLOEARN_Friday_Agent_Core_Ready.zip"),
    Path(r"C:\Users\BHAVESH KHULLAR\Downloads\SOLOEARN_Friday_Agent_Core_Ready.zip"),
    Path(r"C:\Users\BHAVESH KHULLAR\Downloads\SOLOEARN_v0.1\SOLOEARN_Friday_Agent_Core_Ready.zip"),
    brain_dir / "SOLOEARN_Friday_Agent_Core_Ready.zip",
    Path(r"C:\Users\BHAVESH KHULLAR\Desktop\SOLOEARN_Autonomy_Profitability_M8_Ready.zip"),
    Path(r"C:\Users\BHAVESH KHULLAR\Downloads\SOLOEARN_Autonomy_Profitability_M8_Ready.zip"),
    Path(r"C:\Users\BHAVESH KHULLAR\Downloads\SOLOEARN_v0.1\SOLOEARN_Autonomy_Profitability_M8_Ready.zip"),
    brain_dir / "SOLOEARN_Autonomy_Profitability_M8_Ready.zip",
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

        # Prune data/invoices and data/upi_qr directory contents except .gitkeep
        if len(rel_dir.parts) >= 2 and rel_dir.parts[0] == "data" and rel_dir.parts[1] in {"invoices", "upi_qr"}:
            dirs.clear()
            if ".gitkeep" in filenames:
                files.append((cur_path / ".gitkeep", f"data/{rel_dir.parts[1]}/.gitkeep"))
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
    raw_bytes = out_zip.read_bytes()
    sha256_hash = hashlib.sha256(raw_bytes).hexdigest().upper()
    b64_str = base64.b64encode(raw_bytes).decode("ascii")

    # Write .b64.txt adjacent
    b64_file = out_zip.parent / f"{out_zip.name}.b64.txt"
    b64_file.write_text(b64_str, encoding="ascii")

    print(f"Created {out_zip} ({len(raw_bytes):,} bytes, SHA256={sha256_hash}, testzip={test_result})")
    print(f" -> Base64 ASCII copy written to {b64_file} ({len(b64_str):,} chars)")
