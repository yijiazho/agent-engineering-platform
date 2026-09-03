from pathlib import Path

import pytest

from aep.dogfood_runtime import _pinned_workspace_reader
from aep.planning_evidence import PlanningEvidenceInspectionError


REVISION = "5ac8aaf2ce6ce00b1b69b461a033456a6b4192cc"


def test_checkout_reader_accepts_exact_ceiling_and_rejects_one_byte_over(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "execution-plan.md"
    target.parent.mkdir()
    target.write_bytes(b"x" * 17_595)
    reader = _pinned_workspace_reader(tmp_path, REVISION)

    assert len(reader("docs/execution-plan.md", REVISION, 17_595)) == 17_595
    with pytest.raises(PlanningEvidenceInspectionError) as captured:
        reader("docs/execution-plan.md", REVISION, 17_594)
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

    inspected = _pinned_workspace_reader(tmp_path, REVISION).inspect(
        "task.md", REVISION, max_bytes=256 * 1024,
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

    inspected = _pinned_workspace_reader(tmp_path, REVISION).inspect(
        "task.md", REVISION, max_bytes=10_000,
        strategy="STRUCTURED_STATUS_FIELD_SCAN", status_scan_bytes=64,
    )

    assert inspected.inspected_bytes == target.stat().st_size
    assert inspected.status_fields == (("In Progress", 1),)


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda path: None, "TARGET_MISSING"),
        (lambda path: path.mkdir(), "NON_REGULAR_FILE"),
        (lambda path: path.write_bytes(b"a\x00b"), "BINARY_CONTENT"),
        (lambda path: path.write_bytes(b"\xff"), "INVALID_UTF8"),
    ],
)
def test_checkout_reader_uses_stable_safe_classifications(
    tmp_path: Path, setup, reason: str
) -> None:
    target = tmp_path / "target"
    setup(target)
    with pytest.raises(PlanningEvidenceInspectionError) as captured:
        _pinned_workspace_reader(tmp_path, REVISION)("target", REVISION, 100)
    assert captured.value.reason == reason
    assert "content" not in repr(captured.value.metadata)


def test_checkout_reader_rejects_revision_and_unsafe_path(tmp_path: Path) -> None:
    reader = _pinned_workspace_reader(tmp_path, REVISION)
    with pytest.raises(ValueError, match="revision"):
        reader("target", "0" * 40, 100)
    with pytest.raises(ValueError, match="unsafe"):
        reader("../target", REVISION, 100)
