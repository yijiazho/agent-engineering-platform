from hashlib import sha256

import pytest

from aep.localized_patch import LocalizedPatchError, apply_localized_operations
from aep.generate_patch import _validated_changes


REVISION = "a" * 40


def operation(preimage: str, **values):
    return {
        "operation": "insert", "path": "README.md", "repositoryRevision": REVISION,
        "preimageSha256": sha256(preimage.encode()).hexdigest(), "anchor": "## Repository Layout\n",
        "expectedMatchCount": 1, "placement": "after", "content": "src/    runtime\ntests/  tests\n",
        "regionId": "repository-layout", **values,
    }


def apply(preimage: str, operations, **kwargs):
    return apply_localized_operations(
        path="README.md", preimage=preimage,
        preimage_sha256=sha256(preimage.encode()).hexdigest(), repository_revision=REVISION,
        operations=operations, region_id="repository-layout", **kwargs,
    )


def test_issue_88_style_insertion_preserves_more_than_one_hundred_unrelated_lines() -> None:
    preimage = "## Repository Layout\n" + "layout\n" + "## Key Documents\n" + "".join(f"unrelated {i}\n" for i in range(121))
    result = apply(preimage, [operation(preimage)])

    assert result.content.startswith("## Repository Layout\nsrc/    runtime\ntests/  tests\nlayout\n")
    assert "".join(f"unrelated {i}\n" for i in range(121)) in result.content
    assert result.preservation["unchangedBytes"] == len(preimage.encode())
    assert result.preservation["postimageSha256"] == sha256(result.content.encode()).hexdigest()


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda item: item.update(anchor="missing"), "ANCHOR_MISSING"),
        (lambda item: item.update(anchor="same", expectedMatchCount=1), "ANCHOR_AMBIGUOUS"),
        (lambda item: item.update(preimageSha256="0" * 64), "STALE_PREIMAGE"),
        (lambda item: item.update(repositoryRevision="b" * 40), "STALE_REVISION"),
    ],
)
def test_anchor_and_evidence_failures_are_stable(mutator, code) -> None:
    preimage = "same\n## Repository Layout\nsame\n"
    item = operation(preimage)
    mutator(item)
    with pytest.raises(LocalizedPatchError) as error:
        apply(preimage, [item])
    assert error.value.code == code


def test_overlapping_and_out_of_order_operations_fail_before_output() -> None:
    preimage = "## Repository Layout\nanchor\n"
    first = operation(preimage, operation="replace", anchor="Repository Layout", content="Layout")
    second = operation(preimage, operation="replace", anchor="Layout", content="Other")
    with pytest.raises(LocalizedPatchError, match="OVERLAPPING_EDITS"):
        apply(preimage, [first, second])


def test_out_of_order_and_non_utf8_operations_have_stable_diagnostics() -> None:
    preimage = "## Repository Layout\nfirst\nsecond\n"
    later = operation(preimage, operation="replace", anchor="second", content="two")
    earlier = operation(preimage, operation="replace", anchor="first", content="one")
    with pytest.raises(LocalizedPatchError, match="OUT_OF_ORDER_EDITS"):
        apply(preimage, [later, earlier])
    invalid = operation(preimage, content="\ud800")
    with pytest.raises(LocalizedPatchError, match="UNSAFE_CONTENT"):
        apply(preimage, [invalid])


def test_delete_requires_whole_file_and_region_boundary_is_enforced() -> None:
    preimage = "# Repository Layout\ninside\n# Elsewhere\noutside\n"
    digest = sha256(preimage.encode()).hexdigest()
    delete = operation(preimage, operation="delete", anchor="inside", content="", expectedMatchCount=1)
    with pytest.raises(LocalizedPatchError, match="DELETE_NOT_AUTHORIZED"):
        apply_localized_operations(
            path="README.md", preimage=preimage, preimage_sha256=digest,
            repository_revision=REVISION, operations=[delete], region_id="repository-layout",
            region={"kind": "MARKDOWN_SECTION", "name": "Repository Layout"},
        )
    outside = operation(preimage, operation="replace", anchor="outside", content="changed", expectedMatchCount=1)
    with pytest.raises(LocalizedPatchError, match="OUT_OF_REGION"):
        apply_localized_operations(
            path="README.md", preimage=preimage, preimage_sha256=digest,
            repository_revision=REVISION, operations=[outside], region_id="repository-layout",
            region={"kind": "MARKDOWN_SECTION", "name": "Repository Layout"},
        )


def test_empty_preimage_can_create_without_an_existing_anchor() -> None:
    preimage = ""
    result = apply_localized_operations(
        path="new.md", preimage=preimage, preimage_sha256=sha256(b"").hexdigest(),
        repository_revision=REVISION,
        operations=[{"operation": "rewrite", "path": "new.md", "repositoryRevision": REVISION,
                     "preimageSha256": sha256(b"").hexdigest(), "regionId": "new-file", "content": "created\n"}],
        target_exists=False,
    )
    assert result.content == "created\n"


def test_overlapping_anchor_occurrences_are_ambiguous() -> None:
    preimage = "aaa"
    item = operation(preimage, anchor="aa", content="x")
    with pytest.raises(LocalizedPatchError, match="ANCHOR_AMBIGUOUS"):
        apply(preimage, [item])


def test_explicit_rewrite_is_never_inferred_from_size() -> None:
    preimage = "## Repository Layout\n" + "old\n" * 100
    item = operation(preimage, operation="rewrite", content="new\n")
    with pytest.raises(LocalizedPatchError, match="REWRITE_NOT_AUTHORIZED"):
        apply(preimage, [item])
    result = apply(preimage, [item], allow_rewrite=True)
    assert result.content == "new\n"


def test_line_endings_and_repeated_application_are_deterministic() -> None:
    preimage = "## Repository Layout\r\nbody\r\n"
    item = operation(preimage, anchor="## Repository Layout\r\n", content="added\r\n")
    first = apply(preimage, [item])
    repeat = apply(preimage, [item])
    assert first == repeat


def test_generate_patch_materializes_localized_operations_before_write() -> None:
    preimage = "## Repository Layout\nbody\n"
    digest = sha256(preimage.encode()).hexdigest()
    operations = [operation(preimage, regionId="Repository Layout")]
    changes = _validated_changes(
        {"changes": operations}, ("README.md",),
        ({"path": "README.md", "content": preimage, "preimageSha256": digest},),
        repository_revision=REVISION,
        regions_by_path={"README.md": {"kind": "MARKDOWN_SECTION", "name": "Repository Layout"}},
    )
    assert changes[0]["operation"] == "write"
    assert changes[0]["content"] == "## Repository Layout\nsrc/    runtime\ntests/  tests\nbody\n"


def test_generate_patch_rejects_replace_without_plan_operation_identity() -> None:
    preimage = "## Repository Layout\nbody\n"
    digest = sha256(preimage.encode()).hexdigest()
    with pytest.raises(Exception, match="plan-operation evidence"):
        _validated_changes(
            {"changes": [operation(preimage, operation="replace", anchor="body", content="other")]},
            ("README.md",), ({"path": "README.md", "content": preimage, "preimageSha256": digest},),
            repository_revision=REVISION,
        )


def test_generate_patch_accepts_each_required_insertion_for_one_path() -> None:
    preimage = "first\nsecond\n"
    digest = sha256(preimage.encode()).hexdigest()
    changes = _validated_changes(
        {"changes": [
            {**operation(preimage, path="README.md", anchor="first\n", content="one\n"), "regionId": "r"},
            {**operation(preimage, path="README.md", anchor="second\n", content="two\n"), "regionId": "r"},
        ]},
        ("README.md",), ({"path": "README.md", "content": preimage, "preimageSha256": digest},),
        repository_revision=REVISION,
        required_insertions=({"path": "README.md", "value": "one\n"}, {"path": "README.md", "value": "two\n"}),
    )
    assert changes[0]["content"] == "first\none\nsecond\ntwo\n"


def test_generate_patch_accepts_one_subtree_insert_for_many_required_values() -> None:
    preimage = "## Repository Layout\nexisting\n"
    digest = sha256(preimage.encode()).hexdigest()
    subtree = "deploy/\ndeploy/local/\ndeploy/self-hosting/\ndeploy/validation/\n"
    changes = _validated_changes(
        {"changes": [operation(preimage, content=subtree)]}, ("README.md",),
        ({"path": "README.md", "content": preimage, "preimageSha256": digest},),
        repository_revision=REVISION,
        required_insertions=tuple({"path": "README.md", "value": value} for value in subtree.splitlines()),
    )
    assert all(value in changes[0]["content"] for value in subtree.splitlines())
