from hashlib import sha256

import pytest

from aep.localized_patch import LocalizedPatchError, apply_localized_operations
from aep.generate_patch import (
    GeneratePatchContractError,
    RejectedPatchCandidateError,
    _validated_changes,
)


REVISION = "a" * 40


def operation(preimage: str, **values):
    return {
        "operation": "insert", "path": "README.md", "repositoryRevision": REVISION,
        "preimageSha256": sha256(preimage.encode()).hexdigest(), "anchor": "## Repository Layout\n",
        "expectedMatchCount": 1, "placement": "after", "content": "src/    runtime\ntests/  tests\n",
        "regionId": "repository-layout", **values,
    }


def apply(preimage: str, operations, **kwargs):
    region = kwargs.pop("region", {"kind": "WHOLE_FILE", "name": "WHOLE_FILE"})
    return apply_localized_operations(
        path="README.md", preimage=preimage,
        preimage_sha256=sha256(preimage.encode()).hexdigest(), repository_revision=REVISION,
        operations=operations, region=region, **kwargs,
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
            repository_revision=REVISION, operations=[delete],
            region={"kind": "MARKDOWN_SECTION", "name": "Repository Layout"},
        )
    outside = operation(preimage, operation="replace", anchor="outside", content="changed", expectedMatchCount=1)
    with pytest.raises(LocalizedPatchError, match="OUT_OF_REGION"):
        apply_localized_operations(
            path="README.md", preimage=preimage, preimage_sha256=digest,
            repository_revision=REVISION, operations=[outside],
            region={"kind": "MARKDOWN_SECTION", "name": "Repository Layout"},
        )


def test_empty_preimage_can_create_without_an_existing_anchor() -> None:
    preimage = ""
    result = apply_localized_operations(
        path="new.md", preimage=preimage, preimage_sha256=sha256(b"").hexdigest(),
        repository_revision=REVISION,
        operations=[{"operation": "rewrite", "path": "new.md", "repositoryRevision": REVISION,
                     "preimageSha256": sha256(b"").hexdigest(), "regionId": "new-file", "content": "created\n"}],
        region={"kind": "WHOLE_FILE", "name": "WHOLE_FILE",
                "selectionId": "planselection-new", "planArtifactId": "artifact-new"},
        target_exists=False,
    )
    assert result.content == "created\n"
    assert result.operations[0]["modelDeclaredRegionId"] == "new-file"
    assert result.operations[0]["trustedRegion"]["selectionId"] == "planselection-new"


@pytest.mark.parametrize("label", ["x" * 129, "bad\nlabel", "bad\tlabel"])
def test_model_region_label_is_bounded_body_free_diagnostic(label) -> None:
    preimage = "## Repository Layout\nbody\n"
    with pytest.raises(LocalizedPatchError, match="bounded diagnostic identifier"):
        apply(preimage, [operation(preimage, regionId=label)])


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


@pytest.mark.parametrize("label", ["readme-repository-layout:add-deploy", "Repository Layout", None])
def test_model_region_label_is_diagnostic_and_trusted_span_is_evidence(label) -> None:
    preimage = "## Repository Layout\nbody\n## Elsewhere\noutside\n"
    item = operation(preimage)
    if label is None:
        item.pop("regionId")
    else:
        item["regionId"] = label
    result = apply_localized_operations(
        path="README.md", preimage=preimage, preimage_sha256=sha256(preimage.encode()).hexdigest(),
        repository_revision=REVISION, operations=[item],
        region={"kind": "MARKDOWN_SECTION", "name": "Repository Layout", "selectionId": "planselection-1", "planArtifactId": "artifact-1"},
    )
    evidence = result.operations[0]
    assert evidence["modelDeclaredRegionId"] == label
    assert evidence["trustedRegion"] == {"kind": "MARKDOWN_SECTION", "name": "Repository Layout", "selectionId": "planselection-1", "planArtifactId": "artifact-1", "span": [0, len("## Repository Layout\nbody\n")]}


def test_matching_model_label_cannot_bypass_trusted_region() -> None:
    preimage = "## Repository Layout\ninside\n## Elsewhere\noutside\n"
    item = operation(preimage, anchor="outside", content="changed", regionId="Repository Layout")
    with pytest.raises(LocalizedPatchError) as error:
        apply_localized_operations(
            path="README.md", preimage=preimage, preimage_sha256=sha256(preimage.encode()).hexdigest(),
            repository_revision=REVISION, operations=[item],
            region={"kind": "MARKDOWN_SECTION", "name": "Repository Layout"},
        )
    assert error.value.code == "OUT_OF_REGION"


def test_outside_anchor_cannot_collapse_to_trusted_boundary() -> None:
    preimage = "outside\n## Repository Layout\ninside\n"
    item = operation(preimage, anchor="outside\n", placement="after", content="escaped\n")
    with pytest.raises(LocalizedPatchError) as error:
        apply_localized_operations(
            path="README.md", preimage=preimage,
            preimage_sha256=sha256(preimage.encode()).hexdigest(),
            repository_revision=REVISION, operations=[item],
            region={"kind": "MARKDOWN_SECTION", "name": "Repository Layout"},
        )
    assert error.value.code == "OUT_OF_REGION"


def test_insert_before_first_fence_body_line_stays_inside_trusted_region() -> None:
    preimage = "before\n```layout\nfirst\n```\nafter\n"
    item = operation(
        preimage,
        anchor="first\n",
        placement="before",
        content="added\n",
    )

    result = apply(
        preimage,
        [item],
        region={"kind": "MARKDOWN_FENCE", "name": "layout"},
    )

    assert result.content == "before\n```layout\nadded\nfirst\n```\nafter\n"


def test_section_end_insert_must_preserve_next_heading_boundary() -> None:
    preimage = "## Repository Layout\nlast\n## Elsewhere\noutside\n"
    item = operation(
        preimage,
        anchor="last\n",
        placement="after",
        content="joined",
    )

    with pytest.raises(LocalizedPatchError) as error:
        apply(
            preimage,
            [item],
            region={"kind": "MARKDOWN_SECTION", "name": "Repository Layout"},
        )

    assert error.value.code == "OUT_OF_REGION"


def test_section_end_insert_with_line_break_preserves_next_heading() -> None:
    preimage = "## Repository Layout\nlast\n## Elsewhere\noutside\n"
    item = operation(
        preimage,
        anchor="last\n",
        placement="after",
        content="added\n",
    )

    result = apply(
        preimage,
        [item],
        region={"kind": "MARKDOWN_SECTION", "name": "Repository Layout"},
    )

    assert result.content == (
        "## Repository Layout\nlast\nadded\n## Elsewhere\noutside\n"
    )


def test_section_end_insert_cannot_hide_next_heading_inside_new_fence() -> None:
    preimage = (
        "## Repository Layout\nlast\n"
        "## Elsewhere\n```\noutside\n```\n"
    )
    item = operation(
        preimage,
        anchor="last\n",
        placement="after",
        content="wanted\n```\n",
    )

    with pytest.raises(LocalizedPatchError) as error:
        apply(
            preimage,
            [item],
            region={"kind": "MARKDOWN_SECTION", "name": "Repository Layout"},
        )

    assert error.value.code == "OUT_OF_REGION"


def test_nonrewrite_requires_explicit_trusted_region() -> None:
    preimage = "## Repository Layout\nbody\n"
    with pytest.raises(LocalizedPatchError) as error:
        apply_localized_operations(
            path="README.md", preimage=preimage,
            preimage_sha256=sha256(preimage.encode()).hexdigest(),
            repository_revision=REVISION, operations=[operation(preimage)],
        )
    assert error.value.code == "TRUSTED_REGION_MISSING"


def test_region_candidate_failure_is_not_an_immutable_resource_configuration_error() -> None:
    preimage = "## Repository Layout\ninside\n## Elsewhere\noutside\n"
    with pytest.raises(RejectedPatchCandidateError, match="OUT_OF_REGION"):
        _validated_changes(
            {"changes": [operation(preimage, anchor="outside", regionId="Repository Layout")]},
            ("README.md",),
            ({"path": "README.md", "content": preimage, "preimageSha256": sha256(preimage.encode()).hexdigest()},),
            repository_revision=REVISION,
            regions_by_path={"README.md": {"kind": "MARKDOWN_SECTION", "name": "Repository Layout"}},
        )


def test_unresolvable_trusted_selector_fails_before_operations() -> None:
    preimage = "## Repository Layout\nbody\n## Repository Layout\nother\n"
    with pytest.raises(LocalizedPatchError) as error:
        apply_localized_operations(
            path="README.md", preimage=preimage, preimage_sha256=sha256(preimage.encode()).hexdigest(),
            repository_revision=REVISION, operations=[operation(preimage)],
            region={"kind": "MARKDOWN_SECTION", "name": "Repository Layout"},
        )
    assert error.value.code == "TRUSTED_REGION_UNRESOLVED"
    with pytest.raises(GeneratePatchContractError, match="TRUSTED_REGION_UNRESOLVED"):
        _validated_changes(
            {"changes": [operation(preimage)]}, ("README.md",),
            ({"path": "README.md", "content": preimage,
              "preimageSha256": sha256(preimage.encode()).hexdigest()},),
            repository_revision=REVISION,
            regions_by_path={"README.md": {
                "kind": "MARKDOWN_SECTION", "name": "Repository Layout"
            }},
        )


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
        regions_by_path={"README.md": {"kind": "WHOLE_FILE", "name": "WHOLE_FILE"}},
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
        regions_by_path={"README.md": {"kind": "WHOLE_FILE", "name": "WHOLE_FILE"}},
        required_insertions=tuple({"path": "README.md", "value": value} for value in subtree.splitlines()),
    )
    assert all(value in changes[0]["content"] for value in subtree.splitlines())
