from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path


DOCTR_WEIGHT_NAMES = ("seg.pth", "geotr.pth", "illtr.pth")


class RuntimeMigrationError(RuntimeError):
    pass


@dataclass
class MigrationReport:
    dry_run: bool
    files_planned: int = 0
    files_copied: int = 0
    files_unchanged: int = 0
    files_updated: int = 0
    bytes_planned: int = 0
    bytes_copied: int = 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_files(source: Path) -> list[Path]:
    if not source.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise RuntimeMigrationError(f"Refusing to migrate symlink: {path}")
        if path.is_file():
            files.append(path)
    return files


def _runtime_source_files(users_root: Path) -> list[Path]:
    if not users_root.is_dir():
        return []
    selected: list[Path] = []
    root_files = {".env", "docker-compose.yml", "notepatch-runtime.json"}
    for user_root in sorted(path for path in users_root.iterdir() if path.is_dir()):
        candidates = [user_root / "workspace", user_root / "home" / ".openclaw"]
        for candidate in candidates:
            selected.extend(_source_files(candidate))
        for name in root_files:
            candidate = user_root / name
            if candidate.is_symlink():
                raise RuntimeMigrationError(f"Refusing to migrate symlink: {candidate}")
            if candidate.is_file():
                selected.append(candidate)
    return sorted(selected)


def _copy_verified(
    source: Path,
    destination: Path,
    created: list[Path],
    backups: list[tuple[Path, Path]],
    *,
    update_existing: bool,
) -> str:
    source_hash = sha256_file(source)
    if destination.exists():
        if not destination.is_file():
            raise RuntimeMigrationError(f"Destination differs from source: {destination}")
        if sha256_file(destination) == source_hash:
            return "unchanged"
        if not update_existing:
            raise RuntimeMigrationError(f"Destination differs from source: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.notepatch-migrate-{os.getpid()}")
    try:
        shutil.copy2(source, temporary)
        if sha256_file(temporary) != source_hash:
            raise RuntimeMigrationError(f"Checksum verification failed: {source}")
        if destination.exists():
            backup = destination.with_name(f".{destination.name}.notepatch-backup-{os.getpid()}")
            backup.unlink(missing_ok=True)
            os.replace(destination, backup)
            backups.append((destination, backup))
            os.replace(temporary, destination)
            return "updated"
        os.replace(temporary, destination)
        created.append(destination)
        return "copied"
    finally:
        temporary.unlink(missing_ok=True)


def migrate_runtime_data(
    *,
    doctr_weights_source: Path,
    openclaw_runtime_source: Path,
    data_root: Path,
    apply: bool = False,
    update_existing: bool = False,
) -> MigrationReport:
    report = MigrationReport(dry_run=not apply)
    weights_destination = data_root / "models" / "doctr"
    runtime_source = (
        openclaw_runtime_source / "users"
        if (openclaw_runtime_source / "users").is_dir()
        else openclaw_runtime_source
    )
    runtime_destination = data_root / "openclaw" / "users"

    operations: list[tuple[Path, Path]] = []
    for name in DOCTR_WEIGHT_NAMES:
        source = doctr_weights_source / name
        if not source.is_file():
            raise RuntimeMigrationError(f"Required DocTr weight is missing: {source}")
        operations.append((source, weights_destination / name))
    for source in _runtime_source_files(runtime_source):
        operations.append((source, runtime_destination / source.relative_to(runtime_source)))

    report.files_planned = len(operations)
    report.bytes_planned = sum(source.stat().st_size for source, _ in operations)
    if not apply:
        return report

    created: list[Path] = []
    backups: list[tuple[Path, Path]] = []
    try:
        for source, destination in operations:
            result = _copy_verified(
                source,
                destination,
                created,
                backups,
                update_existing=update_existing,
            )
            if result == "copied":
                report.files_copied += 1
                report.bytes_copied += source.stat().st_size
            elif result == "updated":
                report.files_updated += 1
                report.bytes_copied += source.stat().st_size
            else:
                report.files_unchanged += 1
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        for destination, backup in reversed(backups):
            destination.unlink(missing_ok=True)
            if backup.exists():
                os.replace(backup, destination)
        raise
    for _destination, backup in backups:
        backup.unlink(missing_ok=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy NotePatch runtime data into the monorepo data root.")
    parser.add_argument(
        "--doctr-weights-source",
        type=Path,
        default=Path("/home/usr/dev-debug-env/docserver/vendor/DocTr/model_pretrained"),
    )
    parser.add_argument(
        "--openclaw-runtime-source",
        type=Path,
        default=Path("/home/usr/notepatch-openclaw-users"),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.getenv("NOTEPATCH_DATA_ROOT", "/home/usr/notepatch-data")),
    )
    parser.add_argument("--apply", action="store_true", help="Perform the copy. Without this flag only plan it.")
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="Atomically update destination files that changed since the first copy.",
    )
    args = parser.parse_args()
    report = migrate_runtime_data(
        doctr_weights_source=args.doctr_weights_source.resolve(),
        openclaw_runtime_source=args.openclaw_runtime_source.resolve(),
        data_root=args.data_root.resolve(),
        apply=args.apply,
        update_existing=args.update_existing,
    )
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
