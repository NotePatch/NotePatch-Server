from pathlib import Path

import pytest

from scripts.migrate_monorepo_runtime import RuntimeMigrationError, migrate_runtime_data


def _sources(tmp_path: Path) -> tuple[Path, Path]:
    weights = tmp_path / "old-weights"
    weights.mkdir()
    for name in ("seg.pth", "geotr.pth", "illtr.pth"):
        (weights / name).write_bytes(name.encode())
    runtime = tmp_path / "old-runtime" / "users" / "user-1"
    (runtime / "workspace").mkdir(parents=True)
    (runtime / "workspace" / "note.md").write_text("hello", encoding="utf-8")
    return weights, runtime.parents[1]


def test_runtime_migration_dry_run_does_not_write(tmp_path):
    weights, runtime = _sources(tmp_path)
    destination = tmp_path / "data"
    report = migrate_runtime_data(
        doctr_weights_source=weights,
        openclaw_runtime_source=runtime,
        data_root=destination,
    )
    assert report.dry_run is True
    assert report.files_planned == 4
    assert not destination.exists()


def test_runtime_migration_is_verified_and_idempotent(tmp_path):
    weights, runtime = _sources(tmp_path)
    destination = tmp_path / "data"
    first = migrate_runtime_data(
        doctr_weights_source=weights,
        openclaw_runtime_source=runtime,
        data_root=destination,
        apply=True,
    )
    second = migrate_runtime_data(
        doctr_weights_source=weights,
        openclaw_runtime_source=runtime,
        data_root=destination,
        apply=True,
    )
    assert first.files_copied == 4
    assert second.files_copied == 0
    assert second.files_unchanged == 4
    assert (destination / "models" / "doctr" / "geotr.pth").read_bytes() == b"geotr.pth"
    assert (destination / "openclaw" / "users" / "user-1" / "workspace" / "note.md").read_text() == "hello"


def test_runtime_migration_rolls_back_new_files_on_conflict(tmp_path):
    weights, runtime = _sources(tmp_path)
    destination = tmp_path / "data"
    conflict = destination / "models" / "doctr" / "geotr.pth"
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"different")
    with pytest.raises(RuntimeMigrationError, match="differs"):
        migrate_runtime_data(
            doctr_weights_source=weights,
            openclaw_runtime_source=runtime,
            data_root=destination,
            apply=True,
        )
    assert not (destination / "models" / "doctr" / "seg.pth").exists()
    assert conflict.read_bytes() == b"different"


def test_runtime_migration_can_atomically_update_existing_files(tmp_path):
    weights, runtime = _sources(tmp_path)
    destination = tmp_path / "data"
    migrate_runtime_data(
        doctr_weights_source=weights,
        openclaw_runtime_source=runtime,
        data_root=destination,
        apply=True,
    )
    source_note = runtime / "users" / "user-1" / "workspace" / "note.md"
    source_note.write_text("updated", encoding="utf-8")
    report = migrate_runtime_data(
        doctr_weights_source=weights,
        openclaw_runtime_source=runtime,
        data_root=destination,
        apply=True,
        update_existing=True,
    )
    assert report.files_updated == 1
    assert (destination / "openclaw" / "users" / "user-1" / "workspace" / "note.md").read_text() == "updated"
