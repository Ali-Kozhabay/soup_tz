"""Package source, documentation, and every evidence run; exclude local state and secrets."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
root_files = [
    "README.md",
    "pyproject.toml",
    "uv.lock",
    "Dockerfile",
    "compose.yaml",
    "Makefile",
    "alembic.ini",
    ".env.example",
    ".gitignore",
    ".dockerignore",
    ".python-version",
]
files = [root / name for name in root_files]
for directory in ("src", "tests", "scripts", "migrations", "docs", "results", ".github"):
    files.extend(
        path
        for path in (root / directory).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
destination = root / "dist" / "soup-queue-submission.zip"
destination.parent.mkdir(exist_ok=True)
with ZipFile(destination, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
    for path in sorted(files):
        archive.write(path, arcname=str(Path("soup-queue") / path.relative_to(root)))
with ZipFile(destination) as archive:
    assert archive.testzip() is None
    assert not any(
        name[-5:] == "/.env" or "/.local/" in name or "/.venv/" in name
        for name in archive.namelist()
    )
    print(f"Verified {len(archive.namelist())} files in {destination}")
