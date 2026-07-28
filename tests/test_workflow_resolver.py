import json
from pathlib import Path
import shutil

import pytest

from aep.github_events import normalize_github_issue_created
from aep.resource_loader import ResourceCollection, ResourceLoader, ResourceRef
from aep.workflow_resolver import (
    AmbiguousWorkflowMatchError,
    InvalidNormalizedEventError,
    InvalidTriggerConfigurationError,
    resolve_workflow_for_event,
)


REPO_ROOT = Path(__file__).parents[1]
RESOLVER_FIXTURE = REPO_ROOT / "fixtures" / "resource-loader" / "workflow-resolution"
GITHUB_FIXTURE = REPO_ROOT / "fixtures" / "github" / "issue-created.json"


def test_resolves_github_issue_created_to_explicit_issue_to_pr_version() -> None:
    resources = ResourceLoader(RESOLVER_FIXTURE).load()
    event = normalized_event()

    resolution = resolve_workflow_for_event(event, resources)

    assert resolution.matched
    assert resolution.workflow_ref == ResourceRef("Workflow", "issue-to-pr", "1.0.0")
    assert resolution.workflow_refs == (
        ResourceRef("Workflow", "issue-to-pr", "1.0.0"),
    )


def test_returns_clear_no_match_result() -> None:
    resources = ResourceLoader(RESOLVER_FIXTURE).load()

    resolution = resolve_workflow_for_event(
        {"source": "github", "type": "github.pull_request.opened"},
        resources,
    )

    assert not resolution.matched
    assert resolution.workflow_ref is None
    assert resolution.workflow_refs == ()


def test_rejects_ambiguous_matches_unless_fan_out_is_allowed(tmp_path: Path) -> None:
    fixture_root = tmp_path / "repository"
    shutil.copytree(RESOLVER_FIXTURE, fixture_root)
    second_workflow = json.loads(
        (
            fixture_root / ".ai" / "workflows" / "issue-to-pr.yaml"
        ).read_text(encoding="utf-8")
    )
    second_workflow["metadata"]["name"] = "issue-to-analysis"
    (
        fixture_root / ".ai" / "workflows" / "issue-to-analysis.yaml"
    ).write_text(json.dumps(second_workflow), encoding="utf-8")
    resources = ResourceLoader(fixture_root).load()
    event = normalized_event()

    with pytest.raises(AmbiguousWorkflowMatchError) as raised:
        resolve_workflow_for_event(event, resources)

    assert raised.value.as_dict()["code"] == "ambiguous_workflow_match"
    assert raised.value.as_dict()["details"]["workflowRefs"] == [
        {"kind": "Workflow", "name": "issue-to-analysis", "version": "1.0.0"},
        {"kind": "Workflow", "name": "issue-to-pr", "version": "1.0.0"},
    ]

    fan_out = resolve_workflow_for_event(event, resources, allow_fan_out=True)
    assert fan_out.workflow_refs == (
        ResourceRef("Workflow", "issue-to-analysis", "1.0.0"),
        ResourceRef("Workflow", "issue-to-pr", "1.0.0"),
    )


def test_reports_unresolved_trigger_as_structured_configuration_error() -> None:
    resources = ResourceLoader(RESOLVER_FIXTURE).load()
    workflow = resources.by_kind("Workflow")[0]
    invalid_collection = ResourceCollection(
        workspace=resources.workspace,
        resources=tuple(
            resource for resource in resources.resources if resource.kind != "Event"
        ),
    )

    with pytest.raises(InvalidTriggerConfigurationError) as raised:
        resolve_workflow_for_event(normalized_event(), invalid_collection)

    assert raised.value.as_dict() == {
        "code": "invalid_trigger_configuration",
        "message": (
            "Workflow/issue-to-pr:1.0.0: spec.triggers[0].eventRef cannot resolve "
            "Event/github-issue-created:1.0.0"
        ),
        "details": {
            "workflowRef": {
                "kind": workflow.kind,
                "name": workflow.name,
                "version": workflow.version,
            },
            "eventRef": {
                "kind": "Event",
                "name": "github-issue-created",
                "version": "1.0.0",
            },
        },
    }


def test_reports_duplicate_trigger_as_structured_configuration_error(
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "repository"
    shutil.copytree(RESOLVER_FIXTURE, fixture_root)
    workflow_path = fixture_root / ".ai" / "workflows" / "issue-to-pr.yaml"
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    workflow["spec"]["triggers"].append(workflow["spec"]["triggers"][0])
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    resources = ResourceLoader(fixture_root).load()

    with pytest.raises(InvalidTriggerConfigurationError) as raised:
        resolve_workflow_for_event(normalized_event(), resources)

    assert raised.value.as_dict()["code"] == "invalid_trigger_configuration"
    assert "duplicates Event/github-issue-created:1.0.0" in str(raised.value)


@pytest.mark.parametrize(
    "event, field",
    [
        ({}, "source"),
        ({"source": "github"}, "type"),
        ({"source": "", "type": "github.issue.created"}, "source"),
    ],
)
def test_rejects_invalid_normalized_event(event: dict[str, str], field: str) -> None:
    resources = ResourceLoader(RESOLVER_FIXTURE).load()

    with pytest.raises(InvalidNormalizedEventError) as raised:
        resolve_workflow_for_event(event, resources)

    assert raised.value.as_dict()["details"]["field"] == field


def normalized_event() -> dict[str, object]:
    payload = json.loads(GITHUB_FIXTURE.read_text(encoding="utf-8"))
    return normalize_github_issue_created(payload, delivery_id="delivery-123")
