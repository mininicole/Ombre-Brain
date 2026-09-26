#!/usr/bin/env python3
"""Create an encrypted, consistency-aware backup of Evan's Ombre data on Fly.

Adapted from Gale's create_encrypted_backup.py. Differences for Evan:
- the frozen Gale subtree (data/gale) stays on Fly as Gale's rollback point;
- ChatNest's Claude CLI session transcripts under .claude/projects are kept
  (needed to resume /chat conversations), other .claude files are not;
- SQLite files are detected by header, not only by the .db suffix;
- output defaults to /tmp so the Fly volume is never written.

The archive is encrypted before it leaves the Fly Machine. SQLite databases are
copied with the online backup API.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path


EXCLUDED_DATA_ROOT_NAMES = {
    ".guardian_presence.json",
    ".migration-freeze-all",
    ".migration-read-only",
    "_migration_backups",
    "backups",
}
# Top-level entries of the data root that belong to someone else.
EXCLUDED_TOP_LEVEL = {"gale"}
# Only this part of .claude is data; the rest is CLI cache/credentials.
CLAUDE_KEEP_PREFIX = (".claude", "projects")
EXCLUDED_SUFFIXES = {".db-shm", ".db-wal", ".db-journal", "-shm", "-wal", "-journal"}
SQLITE_MAGIC = b"SQLite format 3\x00"
SOURCE_ROOT_NAMES = {
    "chatnest",
    "play",
}
SOURCE_FILE_SUFFIXES = {".py", ".sh", ".txt", ".json", ".yaml", ".yml", ".html", ".css", ".js", ".png"}
RUNTIME_PACKAGE_NAMES = ("night_fall",)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_excluded_data_path(relative: Path) -> bool:
    parts = relative.parts
    if parts[0] in EXCLUDED_TOP_LEVEL:
        return True
    if parts[0] == ".claude" and parts[: len(CLAUDE_KEEP_PREFIX)] != CLAUDE_KEEP_PREFIX:
        return True
    if any(part in EXCLUDED_DATA_ROOT_NAMES for part in parts):
        return True
    return any(str(relative).endswith(suffix) for suffix in EXCLUDED_SUFFIXES)


def is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def sqlite_online_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst, pages=256, sleep=0.05)
        integrity = dst.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise RuntimeError(f"SQLite integrity check failed for {source.name}")
    finally:
        dst.close()
        src.close()


def copy_data(data_root: Path, stage_data: Path) -> dict[str, int]:
    copied_files = 0
    copied_bytes = 0
    for source in sorted(data_root.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(data_root)
        if is_excluded_data_path(relative):
            continue
        destination = stage_data / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if is_sqlite(source):
            sqlite_online_backup(source, destination)
        else:
            shutil.copy2(source, destination)
        copied_files += 1
        copied_bytes += destination.stat().st_size
    return {"files": copied_files, "bytes": copied_bytes}


def copy_source(app_root: Path, stage_source: Path) -> dict[str, int]:
    copied_files = 0
    copied_bytes = 0
    for source in sorted(app_root.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(app_root)
        if relative.parts[0] in {"buckets", "__pycache__", ".git", ".claude"}:
            continue
        if len(relative.parts) == 1:
            allowed = source.suffix in SOURCE_FILE_SUFFIXES or source.name in {"Dockerfile"}
        else:
            allowed = relative.parts[0] in SOURCE_ROOT_NAMES and source.suffix in SOURCE_FILE_SUFFIXES
        if not allowed:
            continue
        destination = stage_source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied_files += 1
        copied_bytes += destination.stat().st_size
    return {"files": copied_files, "bytes": copied_bytes}


def copy_runtime_packages(stage_root: Path) -> list[dict[str, object]]:
    packages: list[dict[str, object]] = []
    for name in RUNTIME_PACKAGE_NAMES:
        spec = importlib.util.find_spec(name)
        if spec is None or not spec.submodule_search_locations:
            raise RuntimeError(f"Required runtime package is unavailable: {name}")
        roots = [Path(item).resolve(strict=True) for item in spec.submodule_search_locations]
        if len(roots) != 1:
            raise RuntimeError(f"Ambiguous runtime package roots: {name}")
        source_root = roots[0]
        destination_root = stage_root / name
        copied_files = 0
        copied_bytes = 0
        for source in sorted(source_root.rglob("*")):
            if not source.is_file() or "__pycache__" in source.parts or source.suffix not in {".py", ".json"}:
                continue
            destination = destination_root / source.relative_to(source_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied_files += 1
            copied_bytes += destination.stat().st_size
        packages.append(
            {
                "name": name,
                "version": importlib.metadata.version(name.replace("_", "-")),
                "files": copied_files,
                "bytes": copied_bytes,
            }
        )
    return packages


def manifest_entries(stage_root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(stage_root.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        entries.append(
            {
                "path": path.relative_to(stage_root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    return entries


def write_tar_gz(stage_root: Path, output_stream) -> None:
    with tarfile.open(fileobj=output_stream, mode="w|gz", format=tarfile.PAX_FORMAT) as archive:
        for child in sorted(stage_root.iterdir()):
            archive.add(child, arcname=child.name, recursive=True)


def encrypt_archive(stage_root: Path, certificate: Path, partial_output: Path) -> None:
    command = [
        "openssl",
        "cms",
        "-encrypt",
        "-binary",
        "-aes-256-cbc",
        "-outform",
        "DER",
        "-out",
        str(partial_output),
        str(certificate),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    assert process.stdin is not None
    try:
        write_tar_gz(stage_root, process.stdin)
    finally:
        process.stdin.close()
    if process.wait() != 0:
        raise RuntimeError("OpenSSL CMS encryption failed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", default="/app")
    parser.add_argument("--data-root", default="/app/buckets")
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--output-directory", default="/tmp/evan-backup")
    arguments = parser.parse_args()

    app_root = Path(arguments.app_root).resolve(strict=True)
    data_root = Path(arguments.data_root).resolve(strict=True)
    certificate = Path(arguments.certificate).resolve(strict=True)
    Path(arguments.output_directory).mkdir(parents=True, exist_ok=True)
    output_directory = Path(arguments.output_directory).resolve(strict=True)
    if data_root == Path("/") or output_directory == Path("/"):
        raise RuntimeError("Refusing a filesystem-root data or output directory")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"evan-ombre-{timestamp}.tar.gz.cms"
    final_output = output_directory / filename
    partial_output = output_directory / f".{filename}.partial"
    checksum_output = output_directory / f"{filename}.sha256"
    if final_output.exists() or checksum_output.exists() or partial_output.exists():
        raise RuntimeError("Backup output already exists")

    output_directory.mkdir(parents=True, exist_ok=True)
    os.chmod(output_directory, 0o700)
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        with tempfile.TemporaryDirectory(prefix="evan-ombre-stage-", dir="/tmp") as stage_name:
            stage_root = Path(stage_name)
            data_summary = copy_data(data_root, stage_root / "data")
            source_summary = copy_source(app_root, stage_root / "source")
            runtime_packages = copy_runtime_packages(stage_root / "runtime_packages")
            manifest = {
                "schema": "ombre-migration-backup-v2",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "capture_started_at": started_at,
                "source_summary": source_summary,
                "data_summary": data_summary,
                "runtime_packages": runtime_packages,
                "excluded": sorted(EXCLUDED_DATA_ROOT_NAMES | EXCLUDED_SUFFIXES | EXCLUDED_TOP_LEVEL) + [".claude (except projects/)"],
                "environment_variable_names": sorted(os.environ),
                "entries": manifest_entries(stage_root),
            }
            manifest_path = stage_root / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            os.chmod(manifest_path, 0o600)
            encrypt_archive(stage_root, certificate, partial_output)
        os.chmod(partial_output, 0o600)
        os.replace(partial_output, final_output)
        checksum = sha256(final_output)
        checksum_output.write_text(f"{checksum}  {filename}\n", encoding="ascii")
        os.chmod(checksum_output, 0o600)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "archive": filename,
                    "encrypted_bytes": final_output.stat().st_size,
                    "sha256": checksum,
                    "source_files": source_summary["files"],
                    "data_files": data_summary["files"],
                    "runtime_packages": [item["name"] for item in runtime_packages],
                },
                sort_keys=True,
            )
        )
    except Exception:
        partial_output.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()
