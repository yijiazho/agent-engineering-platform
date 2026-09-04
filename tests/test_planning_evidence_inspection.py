from pathlib import Path
import subprocess

import pytest

from aep.dogfood_runtime import _pinned_workspace_reader
from aep.planning_evidence import PlanningEvidenceInspectionError


def commit_repository(root: Path) -> str:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "AEP Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture", "--allow-empty"], check=True)
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, check=True, text=True,
    ).stdout.strip()


def test_checkout_reader_accepts_exact_ceiling_and_rejects_one_byte_over(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "execution-plan.md"
    target.parent.mkdir()
    target.write_bytes(b"x" * 17_595)
    revision = commit_repository(tmp_path)
    reader = _pinned_workspace_reader(tmp_path, revision)

    assert len(reader("docs/execution-plan.md", revision, 17_595)) == 17_595
    with pytest.raises(PlanningEvidenceInspectionError) as captured:
        reader("docs/execution-plan.md", revision, 17_594)
    assert captured.value.metadata == {
        "reason": "SIZE_LIMIT_EXCEEDED", "path": "docs/execution-plan.md",
        "blobSize": 17_595, "appliedTrustedCeiling": 17_594,
        "predicateType": None, "inspectionStrategy": None,
        "evaluationComplete": False,
    }


def test_status_scanner_streams_large_blob_without_materializing_body(tmp_path: Path) -> None:
    target = tmp_path / "task.md"
    body = b"**Status:** In Progress\n" + b"small line\n" * 8_000
    target.write_bytes(body)

    revision = commit_repository(tmp_path)
    inspected = _pinned_workspace_reader(tmp_path, revision).inspect(
        "task.md", revision, max_bytes=256 * 1024,
        strategy="STRUCTURED_STATUS_FIELD_SCAN", status_scan_bytes=64 * 1024,
    )

    assert len(body) > 64 * 1024
    assert inspected.content == ""
    assert inspected.blob_size == len(body)
    assert inspected.inspected_bytes == len(body)
    assert inspected.status_fields == (("In Progress", 1),)


def test_status_scanner_stops_matching_at_cumulative_search_bound(tmp_path: Path) -> None:
    target = tmp_path / "task.md"
    target.write_text(
        "**Status:** In Progress\n" + "short line\n" * 20
        + "**Status:** Completed\n", encoding="utf-8",
    )

    revision = commit_repository(tmp_path)
    inspected = _pinned_workspace_reader(tmp_path, revision).inspect(
        "task.md", revision, max_bytes=10_000,
        strategy="STRUCTURED_STATUS_FIELD_SCAN", status_scan_bytes=64,
    )

    assert inspected.inspected_bytes == inspected.blob_size
    assert inspected.status_fields == (("In Progress", 1),)


def test_status_scanner_does_not_match_truncated_boundary_line(tmp_path: Path) -> None:
    target = tmp_path / "task.md"
    target.write_text("header\n**Status:** Completed but not verified\n", encoding="utf-8")
    revision = commit_repository(tmp_path)

    inspected = _pinned_workspace_reader(tmp_path, revision).inspect(
        "task.md", revision, max_bytes=100,
        strategy="STRUCTURED_STATUS_FIELD_SCAN", status_scan_bytes=28,
    )

    assert inspected.status_fields == ()


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda path: None, "TARGET_MISSING"),
        (lambda path: path.write_bytes(b"a\x00b"), "BINARY_CONTENT"),
        (lambda path: path.write_bytes(b"\xff"), "INVALID_UTF8"),
    ],
)
def test_checkout_reader_uses_stable_safe_classifications(
    tmp_path: Path, setup, reason: str
) -> None:
    target = tmp_path / "target"
    setup(target)
    revision = commit_repository(tmp_path)
    with pytest.raises(PlanningEvidenceInspectionError) as captured:
        _pinned_workspace_reader(tmp_path, revision)("target", revision, 100)
    assert captured.value.reason == reason
    assert "content" not in repr(captured.value.metadata)


def test_checkout_reader_rejects_revision_and_unsafe_path(tmp_path: Path) -> None:
    revision = commit_repository(tmp_path)
    reader = _pinned_workspace_reader(tmp_path, revision)
    with pytest.raises(PlanningEvidenceInspectionError, match="REVISION_MISMATCH"):
        reader("target", "0" * 40, 100)
    with pytest.raises(PlanningEvidenceInspectionError, match="UNSAFE_PATH"):
        reader("../target", revision, 100)
    with pytest.raises(PlanningEvidenceInspectionError, match="UNSAFE_PATH"):
        reader.verify_absent("../target", revision)


def test_checkout_reader_rejects_non_regular_git_entry(tmp_path: Path) -> None:
    revision = commit_repository(tmp_path)
    blob = subprocess.run(
        ["git", "-C", str(tmp_path), "hash-object", "-w", "--stdin"],
        input=b"destination", capture_output=True, check=True,
    ).stdout.decode().strip()
    subprocess.run([
        "git", "-C", str(tmp_path), "update-index", "--add", "--cacheinfo",
        f"120000,{blob},link",
    ], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "symlink"], check=True)
    revision = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True, check=True, text=True,
    ).stdout.strip()

    reader = _pinned_workspace_reader(tmp_path, revision)
    with pytest.raises(PlanningEvidenceInspectionError, match="NON_REGULAR_FILE"):
        reader("link", revision, 100)
    with pytest.raises(PlanningEvidenceInspectionError, match="NON_REGULAR_FILE"):
        reader.verify_absent("link", revision)


def test_checkout_reader_uses_pinned_blob_not_changed_worktree(tmp_path: Path) -> None:
    target = tmp_path / "task.md"
    target.write_text("original", encoding="utf-8")
    revision = commit_repository(tmp_path)
    target.write_text("uncommitted replacement", encoding="utf-8")

    inspected = _pinned_workspace_reader(tmp_path, revision).inspect(
        "task.md", revision, max_bytes=100,
        strategy="COMPLETE_BLOB_SCAN", status_scan_bytes=50,
    )

    assert inspected.content == "original"


def test_absence_probe_distinguishes_ignored_regular_blob(tmp_path: Path) -> None:
    target = tmp_path / "generated" / "task.md"
    target.parent.mkdir()
    target.write_text("tracked but inventory-ignored", encoding="utf-8")
    revision = commit_repository(tmp_path)
    reader = _pinned_workspace_reader(tmp_path, revision)

    assert reader.verify_absent("generated/task.md", revision) is False
    inspected = reader.inspect(
        "generated/task.md", revision, max_bytes=100,
        strategy="COMPLETE_BLOB_SCAN", status_scan_bytes=50,
    )
    assert inspected.content == "tracked but inventory-ignored"
