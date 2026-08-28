from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from aep.publication_policy import (
    PUBLICATION_EVIDENCE_BOOLEAN_FIELDS,
    PUBLICATION_EVIDENCE_FIELDS,
)

from aep.resource_loader import (
    MissingResourceReferenceError,
    ResourceCollection,
    ResourceLoader,
    ResourceRef,
)
from aep.task_dag import resolve_task_dag
from aep.workflow_resolver import resolve_workflow_for_event


ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = ROOT / "fixtures" / "self-hosting"
EXPECTED = json.loads((FIXTURE_ROOT / "expected-bundle.json").read_text(encoding="utf-8"))
TASK_ORDER = tuple(EXPECTED["taskOrder"])


@pytest.fixture(scope="module")
def resources() -> ResourceCollection:
    return ResourceLoader(ROOT).load()


def test_complete_bundle_loads_with_exact_immutable_inventory(
    resources: ResourceCollection,
) -> None:
    assert {
        kind: len(resources.by_kind(kind))
        for kind in EXPECTED["resourceCounts"]
    } == EXPECTED["resourceCounts"]
    assert resources.workspace.ref == ResourceRef(
        "Workspace", "agent-engineering-platform", "1.0.0"
    )
    assert sorted(item.name for item in resources.by_kind("Agent")) == EXPECTED[
        "agentNames"
    ]
    assert all(resource.version != "latest" for resource in resources.resources)
    assert all(
        reference.version != "latest"
        for resource in resources.resources
        for reference in resource.references
    )


def test_normalized_issue_selects_only_six_task_workflow_in_order(
    resources: ResourceCollection,
) -> None:
    event = json.loads(
        (FIXTURE_ROOT / "normalized-issue.json").read_text(encoding="utf-8")
    )
    resolution = resolve_workflow_for_event(event, resources)
    assert resolution.workflow_refs == (
        ResourceRef(
            "Workflow", "issue-to-pr", EXPECTED["resourceVersions"]["workflow"]
        ),
    )

    workflow = resources.get(resolution.workflow_refs[0])
    assert workflow is not None
    plan = resolve_task_dag(workflow, resources)
    assert tuple(ref.name for ref in plan.topological_order) == TASK_ORDER
    assert tuple(tuple(ref.name for ref in group) for group in plan.ready_groups) == tuple(
        (name,) for name in TASK_ORDER
    )


def test_context_and_agent_boundaries_are_explicit(
    resources: ResourceCollection,
) -> None:
    assert resources.get(
        ResourceRef(
            "Task",
            "analyze-issue",
            EXPECTED["resourceVersions"]["analyzeIssueTask"],
        )
    ) is not None
    tasks = {item.name: item.data["spec"] for item in resources.by_kind("Task")}
    assert {"event", "issue", "candidate-files"} <= set(
        tasks["analyze-issue"]["requiredContext"]
    )
    assert {"repository-inventory", "documentation", "knowledge"} == set(
        tasks["analyze-issue"]["optionalContext"]
    )
    assert tasks["analyze-issue"]["inputContextTokenBudget"] == 32_000
    assert tasks["create-pull-request"]["inputContextTokenBudget"] == 32_000
    assert {"issue", "candidate-files", "prior-artifacts"} <= set(
        tasks["build-implementation-plan"]["requiredContext"]
    )
    assert {"prior-artifacts", "policies"} <= set(
        tasks["generate-patch"]["requiredContext"]
    )
    assert "agentRef" not in tasks["run-validation"]
    assert "agentRef" not in tasks["evaluate-acceptance"]

    agents = {item.name: item.data["spec"] for item in resources.by_kind("Agent")}
    assert "toolRefs" not in agents["issue-analyzer"]
    assert "toolRefs" not in agents["planner"]
    assert agents["code-generator"]["toolRefs"] == [
        {"kind": "Tool", "name": "filesystem", "version": "1.1.0"},
        {"kind": "Tool", "name": "git", "version": "1.0.0"},
    ]
    assert "toolRefs" not in agents["pr-writer"]
    for agent in agents.values():
        assert "repository.retrieve" not in {
            capability
            for ref in agent.get("toolRefs", [])
            for capability in resources.get(ResourceRef(**_ref_kwargs(ref))).data["spec"][
                "capabilities"
            ]
        }

    model = resources.get(
        ResourceRef(
            "Model", "default-reasoning", EXPECTED["resourceVersions"]["model"]
        )
    )
    assert model is not None
    assert "parameters" not in model.data["spec"]
    assert model.data["spec"]["timeoutMs"] == 120000
    assert model.data["spec"]["tokenLimit"] == 32000
    assert model.data["spec"]["retryPolicy"]["maxAttempts"] == 1
    assert model.data["spec"]["rateLimitPolicy"] == {
        "requestsPerMinute": 2,
        "tokensPerMinute": 80000,
    }


def test_capabilities_fail_closed_and_publication_is_exclusive(
    resources: ResourceCollection,
) -> None:
    tasks = {item.name: item.data["spec"] for item in resources.by_kind("Task")}
    assert tasks["generate-patch"]["policies"] == [
        {"kind": "Policy", "name": "workspace-write", "version": "1.0.0"}
    ]
    assert tasks["run-validation"]["policies"] == [
        {
            "kind": "Policy",
            "name": "validation-capabilities",
            "version": "1.0.0",
        }
    ]
    assert {ref["name"] for ref in tasks["create-pull-request"]["policies"]} == {
        "publication-evidence",
        "publication-capabilities",
    }

    policies = {item.name: item.data["spec"] for item in resources.by_kind("Policy")}
    allowed = {
        capability
        for policy in policies.values()
        for rule in policy["rules"]
        if rule["effect"] == "allow"
        for capability in rule.get("capabilities", [])
    }
    assert allowed == {
        "filesystem.read",
        "filesystem.write",
        "git.read",
        "docker.run",
        *EXPECTED["publicationCapabilities"],
    }
    denied = {
        capability
        for rule in policies["agent-safety"]["rules"]
        for capability in rule["capabilities"]
    }
    assert denied == set(EXPECTED["forbiddenCapabilities"])

    publication_tools = tasks["create-pull-request"]["publication"]
    assert resources.get(ResourceRef(**_ref_kwargs(publication_tools["gitToolRef"]))).data[
        "spec"
    ]["capabilities"] == ["git.push"]
    assert resources.get(
        ResourceRef(**_ref_kwargs(publication_tools["githubToolRef"]))
    ).data["spec"]["capabilities"] == ["github.create_pr"]


def test_publication_policy_semantically_matches_canonical_runtime_evidence(
    resources: ResourceCollection,
) -> None:
    task = resources.get(
        ResourceRef(
            "Task",
            "create-pull-request",
            EXPECTED["resourceVersions"]["createPullRequestTask"],
        )
    )
    policy = resources.get(
        ResourceRef(
            "Policy",
            "publication-evidence",
            EXPECTED["resourceVersions"]["publicationEvidencePolicy"],
        )
    )
    assert task is not None and policy is not None
    assert {
        "kind": "Policy",
        "name": policy.name,
        "version": policy.version,
    } in task.data["spec"]["policies"]
    evidence = {
        name: True for name in PUBLICATION_EVIDENCE_BOOLEAN_FIELDS
    } | {"failures": []}
    assert tuple(evidence) == PUBLICATION_EVIDENCE_FIELDS
    conditions = policy.data["spec"]["rules"][0]["conditions"]
    assert Draft202012Validator(conditions).is_valid(
        {
            "candidateAction": {"action": "github.create_pr"},
            "evidence": evidence,
        }
    )


def test_knowledge_and_validation_are_repository_bound_and_bounded(
    resources: ResourceCollection,
) -> None:
    knowledge = resources.by_kind("KnowledgeBase")[0].data["spec"]
    assert resources.by_kind("KnowledgeBase")[0].version == "1.1.0"
    assert all(1 <= source["limit"] <= 8 for source in knowledge["sources"])
    paths = {source["path"] for source in knowledge["sources"]}
    assert {
        "README.md",
        "docs/architecture/",
        "docs/adr/",
        "docs/tasks/",
        "schemas/",
        "src/",
        "tests/",
    } <= paths

    validation = resources.get(ResourceRef("Task", "run-validation", "1.1.0")).data[
        "spec"
    ]["validation"]
    assert validation["image"].count("@sha256:") == 1
    assert len(validation["image"].split("@sha256:")[1]) == 64
    assert validation["commands"][1] == {
        "type": "test",
        "argv": EXPECTED["validation"]["testCommand"],
    }
    assert validation["commands"][0] == {
        "type": "build",
        "argv": EXPECTED["validation"]["buildCommand"],
    }
    assert validation["workspaceMount"] == {
        "containerPath": "/workspace",
        "readOnly": False,
    }
    docker = resources.get(ResourceRef("Tool", "docker-validation", "1.1.0"))
    assert "network:none" in docker.data["spec"]["permissions"]
    assert validation["resources"] == {
        "cpuLimit": 2,
        "memoryBytes": 1073741824,
    }
    assert validation["timeoutMs"] == 600000


def test_validation_image_lock_matches_the_consumed_resource_and_build_source(
    resources: ResourceCollection,
) -> None:
    validation_root = ROOT / "deploy" / "validation"
    image_lock = json.loads((validation_root / "image.lock.json").read_text(encoding="utf-8"))
    dockerfile = (validation_root / "Dockerfile").read_text(encoding="utf-8")
    validation = resources.get(ResourceRef("Task", "run-validation", "1.1.0")).data[
        "spec"
    ]["validation"]

    assert validation["image"] == image_lock["image"] == EXPECTED["validation"]["image"]
    assert image_lock["baseImage"] == EXPECTED["validation"]["baseImage"]
    assert image_lock["aptSnapshot"] == EXPECTED["validation"]["aptSnapshot"]
    assert f"FROM {image_lock['baseImage']}" in dockerfile
    assert image_lock["aptSnapshot"] in dockerfile
    for package, version in image_lock["packages"].items():
        assert f"{package}={version}" in dockerfile


def test_offline_wheelhouse_exactly_matches_hash_locked_artifacts() -> None:
    validation_root = ROOT / "deploy" / "validation"
    lock_text = (validation_root / "offline-requirements.txt").read_text(
        encoding="utf-8"
    )
    expected_hashes = {
        value.split("sha256:", 1)[1]
        for line in lock_text.splitlines()
        for value in line.split()
        if value.startswith("--hash=sha256:")
    }
    wheels = tuple(sorted((validation_root / "wheelhouse").glob("*.whl")))
    actual_hashes = {
        sha256(wheel.read_bytes()).hexdigest()
        for wheel in wheels
    }

    assert len(wheels) == EXPECTED["validation"]["offlineArtifactCount"]
    assert actual_hashes == expected_hashes
    assert "--no-index" in (
        validation_root / "offline_bootstrap.py"
    ).read_text(encoding="utf-8")


def test_configured_build_and_test_commands_execute_in_fresh_offline_environment(
    tmp_path: Path, resources: ResourceCollection
) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(ROOT / "src", workspace / "src")
    shutil.copytree(ROOT / "deploy" / "validation", workspace / "deploy" / "validation")
    shutil.copy2(ROOT / "pyproject.toml", workspace / "pyproject.toml")
    tests = workspace / "tests"
    tests.mkdir()
    (tests / "test_installed_environment.py").write_text(
        """from importlib.metadata import version

import jsonschema
import yaml


def test_locked_project_and_dependencies_are_installed():
    assert version(\"agent-engineering-platform\") == \"0.1.0\"
    assert jsonschema.__version__
    assert yaml.__version__ == \"6.0.3\"
""",
        encoding="utf-8",
    )
    environment_root = tmp_path / "validation-venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(environment_root)],
        check=True,
        timeout=60,
    )
    python = (
        environment_root / "Scripts" / "python.exe"
        if os.name == "nt"
        else environment_root / "bin" / "python"
    )
    validation = resources.get(ResourceRef("Task", "run-validation", "1.1.0")).data[
        "spec"
    ]["validation"]
    command_environment = {
        **os.environ,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "HTTP_PROXY": "http://127.0.0.1:1",
        "HTTPS_PROXY": "http://127.0.0.1:1",
    }
    completed = []
    for configured in validation["commands"]:
        argv = [
            str(python) if value == "python" else value.replace("/workspace", str(workspace))
            for value in configured["argv"]
        ]
        completed.append(
            subprocess.run(
                argv,
                cwd=workspace,
                env=command_environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        )

    assert [result.returncode for result in completed] == [0, 0], [
        result.stdout + result.stderr for result in completed
    ]
    assert "1 passed" in completed[1].stdout


def test_resources_contain_no_secrets_or_runtime_objects(
    resources: ResourceCollection,
) -> None:
    forbidden_keys = {
        "apikey",
        "credential",
        "generatedartifactid",
        "modelinvocationid",
        "privatekey",
        "runtimestate",
        "token",
        "webhooksecret",
        "workflowexecutionid",
    }
    declarative_kinds = set(EXPECTED["resourceCounts"])
    for resource in resources.resources:
        assert resource.kind in declarative_kinds
        assert not (forbidden_keys & _normalized_keys(resource.data))


@pytest.mark.parametrize(
    "relative_path",
    [
        ".ai/prompts/issue-analysis.yaml",
        ".ai/policies/publication-evidence.yaml",
        ".ai/evaluations/repository-tests.yaml",
    ],
)
def test_loading_fails_when_a_required_reference_is_removed(
    tmp_path: Path, relative_path: str
) -> None:
    shutil.copytree(ROOT / ".ai", tmp_path / ".ai")
    (tmp_path / relative_path).unlink()
    with pytest.raises(MissingResourceReferenceError):
        ResourceLoader(tmp_path, schema_root=ROOT / "schemas" / "resources" / "v1").load()


def _ref_kwargs(value: dict[str, str]) -> dict[str, str]:
    return {
        "kind": value["kind"],
        "name": value["name"],
        "version": value["version"],
    }


def _normalized_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {
            "".join(character for character in key.casefold() if character.isalnum())
            for key in value
        } | set().union(*(_normalized_keys(child) for child in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_normalized_keys(child) for child in value), set())
    return set()
