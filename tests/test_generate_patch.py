from __future__ import annotations

import json
from hashlib import sha256
import os
from pathlib import Path
import subprocess

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.context_builder import ContextBuilder
from aep.filesystem_tool import (
    FILESYSTEM_INPUT_SCHEMA,
    FILESYSTEM_OUTPUT_SCHEMA,
    FilesystemTool,
)
from aep.generate_patch import GeneratePatchContractError, GeneratePatchTaskHandler
from aep.generated_artifact_store import InMemoryGeneratedArtifactStore
from aep.git_tool import (
    GIT_INPUT_SCHEMA,
    GIT_OUTPUT_SCHEMA,
    GitSandboxCommandResult,
    GitSandboxTimeout,
    GitTool,
    GitToolAdapter,
    InMemoryGitCommandLogStore,
)
from aep.model_invocation import FakeModelAdapter, ModelResponse, ModelUsage
from aep.repository_knowledge import (
    InMemoryRepositoryKnowledgeProvider,
    RepositoryFile,
    RepositoryKnowledgeSnapshot,
    SourceProvenance,
)
from aep.resource_loader import Resource, ResourceCollection, ResourceRef
from aep.runtime_store import InMemoryRuntimeObjectStore, RuntimeStoreError
from aep.task_execution import FailureClass


TIMESTAMP = "2026-08-06T12:00:00Z"
WORKFLOW_ID = "workflowexecution-aaaaaaaaaaaa"
PRODUCER_ID = "taskexecution-bbbbbbbbbbbb"
TASK_EXECUTION_ID = "taskexecution-cccccccccccc"
PLAN_INVOCATION_ID = "agentinvocation-dddddddddddd"
PLAN_EVALUATION_ID = "evaluationresult-eeeeeeeeeeee"
PLAN_ARTIFACT_ID = "generatedartifact-ffffffffffff"
BRANCH = "agent/work"
ROOT = Path(__file__).parents[1]

CHANGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["changes"],
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "content", "preimageSha256"],
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                    "preimageSha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
            },
        }
    },
}


class LocalGitSandbox:
    disabled_hooks_path = os.devnull
    null_device_path = os.devnull

    def run(
        self,
        *,
        repository: Path,
        arguments,
        environment,
        timeout_ms: int,
        stdin: bytes | None = None,
    ) -> GitSandboxCommandResult:
        process_environment = dict(environment)
        if os.name == "nt":
            process_environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        try:
            completed = subprocess.run(
                ("git", *arguments),
                cwd=repository,
                env=process_environment,
                input=stdin,
                stdin=subprocess.DEVNULL if stdin is None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_ms / 1000,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise GitSandboxTimeout(
                stdout=error.stdout or b"", stderr=error.stderr or b""
            ) from error
        return GitSandboxCommandResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def test_success_persists_patch_changed_files_tool_evidence_and_evaluation(
    tmp_path: Path,
) -> None:
    store, handler, task, artifact_store, workspace, adapter = setup_handler(
        tmp_path,
        {"changes": [{"path": "src/app.py", "content": "value = 2\n"}]},
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is True
    assert len(adapter.requests) == 1
    context = adapter.requests[0].input["contextPackage"]
    prior = [item for item in context["elements"] if item["type"] == "artifact"]
    assert prior[0]["content"]["metadata"]["artifactType"] == "IMPLEMENTATION_PLAN"
    assert prior[0]["content"]["content"]["intendedFiles"] == ["src/app.py"]
    assert (workspace / "src/app.py").read_text(encoding="utf-8") == "value = 2\n"

    execution = store.get(TASK_EXECUTION_ID)
    assert len(execution["toolInvocationIds"]) == 6
    invocations = [store.get(item) for item in execution["toolInvocationIds"]]
    assert [item["toolRef"]["name"] for item in invocations] == [
        "git",
        "filesystem",
        "filesystem",
        "filesystem",
        "git",
        "git",
    ]
    assert [item["input"]["operation"] for item in invocations] == [
        "diff",
        "read",
        "read",
        "compare_write",
        "diff",
        "check_patch",
    ]
    for invocation in invocations:
        validate_runtime("ToolInvocation", invocation)
    evaluation = store.get(execution["evaluationResultIds"][0])
    artifact = artifact_store.get(execution["generatedArtifactIds"][0])
    assert evaluation["outcome"] == "PASS"
    assert evaluation["evidence"]["git"]["toolInvocationId"] == invocations[5]["id"]
    assert evaluation["target"] == {"type": "GeneratedArtifact", "id": artifact["id"]}
    assert artifact["artifactType"] == "PATCH"
    assert artifact["changedFiles"] == ["src/app.py"]
    validate_runtime("GeneratedArtifact", artifact)
    patch = artifact_store.get_content(artifact["id"]).decode("utf-8")
    assert "-value = 1" in patch
    assert "+value = 2" in patch


def test_retry_reuses_filesystem_diff_and_patch_check_evidence(tmp_path: Path) -> None:
    store, handler, task, artifact_store, _workspace, adapter = setup_handler(
        tmp_path,
        {"changes": [{"path": "src/app.py", "content": "value = 2\n"}]},
    )

    first = handler.execute(task, store.get(TASK_EXECUTION_ID))
    first_execution = store.get(TASK_EXECUTION_ID)
    first_invocations = list(first_execution["toolInvocationIds"])
    first_artifact = artifact_store.get(first_execution["generatedArtifactIds"][0])
    second = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert first.succeeded is True
    assert second.succeeded is True
    assert len(adapter.requests) == 1
    assert store.get(TASK_EXECUTION_ID)["toolInvocationIds"] == first_invocations
    assert artifact_store.get(first_artifact["id"]) == first_artifact


def test_disallowed_model_path_is_rejected_before_any_workspace_mutation(
    tmp_path: Path,
) -> None:
    store, handler, task, artifact_store, workspace, _adapter = setup_handler(
        tmp_path,
        {"changes": [{"path": "private/secret.txt", "content": "secret\n"}]},
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.POLICY
    assert "outside IMPLEMENTATION_PLAN.intendedFiles" in result.message
    assert not (workspace / "private/secret.txt").exists()
    execution = store.get(TASK_EXECUTION_ID)
    assert [store.get(item)["input"]["operation"] for item in execution["toolInvocationIds"]] == ["diff", "read"]
    assert artifact_store.list_by_task_execution(TASK_EXECUTION_ID) == ()


def test_denied_filesystem_capability_records_denial_without_writing(
    tmp_path: Path,
) -> None:
    store, handler, task, artifact_store, workspace, _adapter = setup_handler(
        tmp_path,
        {"changes": [{"path": "src/app.py", "content": "value = 2\n"}]},
        authorize_filesystem=lambda _request: False,
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.POLICY
    assert (workspace / "src/app.py").read_text(encoding="utf-8") == "value = 1\n"
    execution = store.get(TASK_EXECUTION_ID)
    invocation = store.get(execution["toolInvocationIds"][1])
    assert invocation["resultStatus"] == "DENIED"
    assert invocation["failure"]["class"] == "POLICY"
    assert artifact_store.list_by_task_execution(TASK_EXECUTION_ID) == ()


def test_empty_diff_is_an_evaluation_failure_without_patch_artifact(
    tmp_path: Path,
) -> None:
    store, handler, task, artifact_store, _workspace, _adapter = setup_handler(
        tmp_path,
        {"changes": [{"path": "src/app.py", "content": "value = 1\n"}]},
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.EVALUATION
    assert "empty patch" in result.message
    assert artifact_store.list_by_task_execution(TASK_EXECUTION_ID) == ()


def test_patch_evaluation_git_denial_is_persisted_and_blocks_artifact(
    tmp_path: Path,
) -> None:
    store, handler, task, artifact_store, _workspace, _adapter = setup_handler(
        tmp_path,
        {"changes": [{"path": "src/app.py", "content": "value = 2\n"}]},
        authorize_git=lambda request: request.input["operation"] != "check_patch",
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.EVALUATION
    execution = store.get(TASK_EXECUTION_ID)
    invocations = [store.get(item) for item in execution["toolInvocationIds"]]
    check = next(
        item for item in invocations if item["input"]["operation"] == "check_patch"
    )
    assert check["resultStatus"] == "DENIED"
    assert check["failure"]["class"] == "POLICY"
    validate_runtime("ToolInvocation", check)
    assert artifact_store.list_by_task_execution(TASK_EXECUTION_ID) == ()


def test_filesystem_tool_failure_is_classified_and_persisted(tmp_path: Path) -> None:
    store, handler, task, artifact_store, workspace, _adapter = setup_handler(
        tmp_path,
        {"changes": [{"path": "src/missing/app.py", "content": "value = 2\n"}]},
        intended_files=["src"],
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.CONFIGURATION
    assert "could not be materialized" in result.message
    assert not (workspace / "src/missing/app.py").exists()
    invocation = store.get(store.get(TASK_EXECUTION_ID)["toolInvocationIds"][1])
    assert invocation["status"] == "FAILED"
    assert artifact_store.list_by_task_execution(TASK_EXECUTION_ID) == ()


def test_missing_prior_plan_fails_before_model_or_tools(tmp_path: Path) -> None:
    store, handler, task, _artifact_store, _workspace, adapter = setup_handler(
        tmp_path,
        {"changes": [{"path": "src/app.py", "content": "value = 2\n"}]},
        publish_plan=False,
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.CONFIGURATION
    assert "IMPLEMENTATION_PLAN" in result.message
    assert adapter.requests == []


def test_planned_new_file_uses_absent_preimage_and_is_created(tmp_path: Path) -> None:
    empty_digest = sha256(b"").hexdigest()
    store, handler, task, artifact_store, workspace, adapter = setup_handler(
        tmp_path,
        {"changes": [{
            "path": "src/new_test.py",
            "content": "def test_new():\n    assert True\n",
            "preimageSha256": empty_digest,
        }]},
        intended_files=["src/new_test.py"],
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is True
    editable = next(
        item
        for item in adapter.requests[0].input["contextPackage"]["elements"]
        if item["type"] == "editable-target"
    )
    assert editable["content"]["preimageState"] == "ABSENT"
    assert editable["content"]["exists"] is False
    assert editable["content"]["content"] == ""
    assert editable["content"]["preimageSha256"] == empty_digest
    assert (workspace / "src/new_test.py").read_text(encoding="utf-8") == (
        "def test_new():\n    assert True\n"
    )
    execution = store.get(TASK_EXECUTION_ID)
    reads = [
        store.get(item)
        for item in execution["toolInvocationIds"]
        if store.get(item)["input"]["operation"] == "read"
    ]
    assert len(reads) == 2
    assert all(item["toolRef"]["version"] == "1.0.0" for item in reads)
    assert [item["failure"]["class"] for item in reads] == ["PERMANENT", "PERMANENT"]
    assert artifact_store.list_by_task_execution(TASK_EXECUTION_ID)


def test_planned_new_file_creates_missing_parent_directories(tmp_path: Path) -> None:
    empty_digest = sha256(b"").hexdigest()
    store, handler, task, _artifacts, workspace, _adapter = setup_handler(
        tmp_path,
        {"changes": [{
            "path": "src/generated/new_test.py",
            "content": "def test_new():\n    assert True\n",
            "preimageSha256": empty_digest,
        }]},
        intended_files=["src/generated/new_test.py"],
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is True
    assert (workspace / "src/generated/new_test.py").read_text(encoding="utf-8")


def test_dirty_checkout_fails_before_editable_reads_or_model_invocation(tmp_path: Path) -> None:
    store, handler, task, _artifacts, workspace, model = setup_handler(
        tmp_path, {"changes": [{"path": "src/app.py", "content": "value = 2\n"}]}
    )
    (workspace / "src/app.py").write_text("dirty\n", encoding="utf-8")

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert "clean checkout" in result.message
    assert model.requests == []


def test_ignored_planned_file_fails_before_editable_read(tmp_path: Path) -> None:
    store, handler, task, _artifacts, workspace, model = setup_handler(
        tmp_path,
        {"changes": [{"path": "ignored.env", "content": "safe\n"}]},
        intended_files=["ignored.env"],
    )
    (workspace / ".git/info/exclude").write_text("ignored.env\n", encoding="utf-8")
    (workspace / "ignored.env").write_text("SECRET=value\n", encoding="utf-8")

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert "clean checkout" in result.message
    assert model.requests == []


@pytest.mark.parametrize("path", ["src/./app.py", "src//app.py"])
def test_noncanonical_planned_path_fails_before_model(tmp_path: Path, path: str) -> None:
    store, handler, task, _artifacts, _workspace, model = setup_handler(
        tmp_path,
        {"changes": [{"path": path, "content": "value = 2\n"}]},
        intended_files=[path],
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert "normalized repository-relative" in result.message
    assert model.requests == []


def test_utf8_decodable_binary_target_is_rejected(tmp_path: Path) -> None:
    store, handler, _task, _artifacts, workspace, _model = setup_handler(
        tmp_path, {"changes": [{"path": "src/app.py", "content": "value = 2\n"}]}
    )
    (workspace / "src/app.py").write_bytes(b"text\x00binary")

    with pytest.raises(GeneratePatchContractError, match="binary NUL"):
        handler._read_editable_targets(
            task_execution=store.get(TASK_EXECUTION_ID),
            repository_revision=store.get(WORKFLOW_ID)["repositoryRevision"],
            paths=("src/app.py",),
            tool_ref=ref("Tool", "filesystem"),
        )


def test_generated_binary_content_is_rejected_before_mutation(tmp_path: Path) -> None:
    store, handler, task, _artifacts, workspace, _model = setup_handler(
        tmp_path,
        {"changes": [{"path": "src/app.py", "content": "text\x00binary"}]},
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert "binary NUL" in result.message
    assert (workspace / "src/app.py").read_bytes() == b"value = 1\n"


def test_no_change_requires_exact_content_evidence(tmp_path: Path) -> None:
    store, handler, task, _artifacts, _workspace, model = setup_handler(
        tmp_path,
        {"changes": []},
        no_change_files=["src/app.py"],
        required_insertions=[],
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert "not deterministically satisfied" in result.message
    assert model.requests == []


def test_no_change_accepts_required_text_present_in_exact_content(tmp_path: Path) -> None:
    store, handler, task, _artifacts, _workspace, _model = setup_handler(
        tmp_path,
        {"changes": []},
        no_change_files=["src/app.py"],
        required_insertions=[{"path": "src/app.py", "value": "value = 1"}],
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.failure_class is FailureClass.EVALUATION
    assert "empty patch" in result.message


def test_committed_head_drift_fails_before_editable_read(tmp_path: Path) -> None:
    store, handler, task, _artifacts, workspace, model = setup_handler(
        tmp_path, {"changes": [{"path": "src/app.py", "content": "value = 2\n"}]}
    )
    (workspace / "src/app.py").write_text("descendant\n", encoding="utf-8")
    git(workspace, "add", "src/app.py")
    git(workspace, "commit", "-m", "drift")

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert "clean checkout" in result.message
    assert model.requests == []


def test_attachment_failure_rolls_back_successful_write(tmp_path: Path) -> None:
    store, handler, task, _artifacts, workspace, _model = setup_handler(
        tmp_path, {"changes": [{"path": "src/app.py", "content": "value = 2\n"}]}
    )
    original_update = store.update_status
    failed = False

    def fail_write_attachment(object_id, status, **kwargs):
        nonlocal failed
        changes = kwargs.get("changes", {})
        if object_id == TASK_EXECUTION_ID and "toolInvocationIds" in changes and not failed:
            ids = changes["toolInvocationIds"]
            if any(
                (store.get(value) or {}).get("input", {}).get("operation")
                == "compare_write"
                for value in ids
            ):
                failed = True
                raise RuntimeStoreError("attachment checkpoint failed")
        return original_update(object_id, status, **kwargs)

    store.update_status = fail_write_attachment  # type: ignore[method-assign]
    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert (workspace / "src/app.py").read_bytes() == b"value = 1\n"


def setup_handler(
    tmp_path: Path,
    output: object,
    *,
    authorize_filesystem=lambda _request: True,
    authorize_git=lambda _request: True,
    intended_files: list[str] | None = None,
    publish_plan: bool = True,
    no_change_files: list[str] | None = None,
    required_insertions: list[dict[str, str]] | None = None,
):
    workspace, evaluation_workspace, revision = repositories(tmp_path)
    resources, task = resource_collection()
    store = InMemoryRuntimeObjectStore()
    store.create(workflow_execution(revision), deterministic_key="workflow")
    store.create(producer_execution(revision), deterministic_key="producer")
    store.create(task_execution(revision), deterministic_key="generate")
    store.create(plan_evaluation(revision), deterministic_key="plan-evaluation")
    artifact_store = InMemoryGeneratedArtifactStore(runtime_store=store)
    if publish_plan:
        artifact_store.publish(
            plan_metadata(revision),
            implementation_plan(
                intended_files or ["src/app.py"],
                no_change_files=no_change_files,
                required_insertions=required_insertions,
            ),
        )
    if isinstance(output, dict) and isinstance(output.get("changes"), list):
        output = json.loads(json.dumps(output))
        for change in output["changes"]:
            change.setdefault("preimageSha256", "0" * 64)
            if change.get("path") == "src/app.py":
                change["preimageSha256"] = sha256(b"value = 1\n").hexdigest()
    model = FakeModelAdapter(
        [ModelResponse(output=output, usage=ModelUsage(30, 20), latency_ms=5)]
    )
    handler = GeneratePatchTaskHandler(
        resources=resources,
        runtime_store=store,
        context_builder=ContextBuilder(
            repository_knowledge=repository_provider(revision),
            artifact_store=artifact_store,
            runtime_store=store,
        ),
        artifact_store=artifact_store,
        model_adapter=model,
        event_resolver=lambda _event_id: None,
        clock=lambda: TIMESTAMP,
        filesystem_tool=FilesystemTool(workspace, store),
        workspace_git_tool=GitTool(git_adapter(workspace, revision), store),
        evaluation_git_tool=GitTool(git_adapter(evaluation_workspace, revision), store),
        authorize_filesystem=authorize_filesystem,
        authorize_git=authorize_git,
        working_branch=BRANCH,
    )
    return store, handler, task, artifact_store, workspace, model


def repositories(tmp_path: Path) -> tuple[Path, Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.name", "AEP Test")
    git(source, "config", "user.email", "aep@example.test")
    git(source, "config", "core.autocrlf", "false")
    (source / "src").mkdir()
    (source / "src/app.py").write_bytes(b"value = 1\n")
    git(source, "add", "src/app.py")
    git(source, "commit", "-m", "fixture")
    revision = git(source, "rev-parse", "HEAD")
    workspace = tmp_path / "workspace"
    evaluation_workspace = tmp_path / "evaluation-workspace"
    git(tmp_path, "-c", "core.autocrlf=false", "clone", str(source), str(workspace))
    git(
        tmp_path,
        "-c",
        "core.autocrlf=false",
        "clone",
        str(source),
        str(evaluation_workspace),
    )
    git(workspace, "switch", "-c", BRANCH)
    git(evaluation_workspace, "switch", "-c", BRANCH)
    return workspace, evaluation_workspace, revision


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.decode("utf-8").strip()


def git_adapter(root: Path, revision: str) -> GitToolAdapter:
    return GitToolAdapter(
        repository=root,
        repository_id="octo/repo",
        expected_revision=revision,
        working_branch=BRANCH,
        log_store=InMemoryGitCommandLogStore(),
        sandbox=LocalGitSandbox(),
    )


def resource_collection() -> tuple[ResourceCollection, Resource]:
    workspace = resource("Workspace", "local", {"repository": "octo/repo"})
    task = resource(
        "Task",
        "generate-patch",
        {
            "objective": "Generate scoped code and test changes.",
            "agentRef": ref("Agent", "code-generator"),
            "outputs": CHANGE_SCHEMA,
            "requiredContext": ["editable-targets", "prior-artifacts", "repository-inventory", "policies"],
            "inputContextTokenBudget": 32_000,
            "evaluations": [ref("Evaluation", "patch-safety")],
            "policies": [ref("Policy", "workspace-write")],
        },
    )
    agent = resource(
        "Agent",
        "code-generator",
        {
            "role": "Code Generator",
            "promptRef": ref("Prompt", "generate-patch"),
            "modelRef": ref("Model", "fake-generator"),
            "toolRefs": [ref("Tool", "filesystem"), ref("Tool", "git")],
            "outputSchema": CHANGE_SCHEMA,
        },
    )
    prompt = resource(
        "Prompt",
        "generate-patch",
        {
            "system": "Use only the immutable ContextPackage.",
            "formatting": "Return structured file changes.",
        },
    )
    model = resource(
        "Model",
        "fake-generator",
        {
            "provider": "local",
            "model": "fake-generator-v1",
            "parameters": {"temperature": 0},
            "tokenLimit": 4096,
            "timeoutMs": 5000,
        },
    )
    filesystem = resource(
        "Tool",
        "filesystem",
        {
            "category": "execution",
            "capabilities": ["filesystem.write"],
            "inputSchema": FILESYSTEM_INPUT_SCHEMA,
            "outputSchema": FILESYSTEM_OUTPUT_SCHEMA,
        },
    )
    git_tool = resource(
        "Tool",
        "git",
        {
            "category": "execution",
            "capabilities": ["git.read"],
            "inputSchema": GIT_INPUT_SCHEMA,
            "outputSchema": GIT_OUTPUT_SCHEMA,
        },
    )
    evaluation = resource("Evaluation", "patch-safety", {"type": "patch"})
    policy = resource(
        "Policy",
        "workspace-write",
        {
            "type": "pre-execution-capability",
            "rules": [
                {
                    "effect": "allow",
                    "capabilities": ["filesystem.write", "git.read"],
                }
            ],
        },
    )
    values = (workspace, task, agent, prompt, model, filesystem, git_tool, evaluation, policy)
    return ResourceCollection(workspace=workspace, resources=values), task


def workflow_execution(revision: str) -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "WorkflowExecution",
        "id": WORKFLOW_ID,
        "traceId": "trace-generate-patch",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "workflow-controller",
            "repositoryRevision": revision,
            "resourceRefs": [],
        },
        "workflowRef": ref("Workflow", "issue-to-pr"),
        "repositoryRevision": revision,
        "knowledgeGraphVersion": "snapshot-patch-v1",
        "status": "RUNNING",
        "startedAt": TIMESTAMP,
        "taskExecutionIds": [PRODUCER_ID, TASK_EXECUTION_ID],
    }


def producer_execution(revision: str) -> dict:
    return {
        **task_execution(revision),
        "id": PRODUCER_ID,
        "taskRef": ref("Task", "build-implementation-plan"),
        "status": "SUCCEEDED",
        "dependencyTaskExecutionIds": [],
        "agentInvocationIds": [PLAN_INVOCATION_ID],
        "evaluationResultIds": [PLAN_EVALUATION_ID],
        "generatedArtifactIds": [PLAN_ARTIFACT_ID],
        "completedAt": TIMESTAMP,
        "provenance": {
            "actor": "workflow-scheduler",
            "workflowExecutionId": WORKFLOW_ID,
            "repositoryRevision": revision,
            "resourceRefs": [ref("Task", "build-implementation-plan")],
        },
    }


def task_execution(revision: str) -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "TaskExecution",
        "id": TASK_EXECUTION_ID,
        "traceId": "trace-generate-patch",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "workflow-scheduler",
            "workflowExecutionId": WORKFLOW_ID,
            "repositoryRevision": revision,
            "resourceRefs": [ref("Task", "generate-patch")],
        },
        "workflowExecutionId": WORKFLOW_ID,
        "taskRef": ref("Task", "generate-patch"),
        "attempt": 1,
        "status": "RUNNING",
        "dependencyTaskExecutionIds": [PRODUCER_ID],
        "startedAt": TIMESTAMP,
    }


def plan_evaluation(revision: str) -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "EvaluationResult",
        "id": PLAN_EVALUATION_ID,
        "traceId": "trace-generate-patch",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "schema-evaluator",
            "workflowExecutionId": WORKFLOW_ID,
            "taskExecutionId": PRODUCER_ID,
            "repositoryRevision": revision,
            "resourceRefs": [ref("Evaluation", "implementation-plan-schema")],
        },
        "taskExecutionId": PRODUCER_ID,
        "evaluationRef": ref("Evaluation", "implementation-plan-schema"),
        "target": {"type": "AgentInvocation", "id": PLAN_INVOCATION_ID},
        "status": "SUCCEEDED",
        "outcome": "PASS",
        "startedAt": TIMESTAMP,
        "completedAt": TIMESTAMP,
    }


def plan_metadata(revision: str) -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "GeneratedArtifact",
        "id": PLAN_ARTIFACT_ID,
        "traceId": "trace-generate-patch",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "build-implementation-plan-task-handler",
            "workflowExecutionId": WORKFLOW_ID,
            "taskExecutionId": PRODUCER_ID,
            "repositoryRevision": revision,
            "resourceRefs": [],
        },
        "taskExecutionId": PRODUCER_ID,
        "artifactType": "IMPLEMENTATION_PLAN",
        "repositoryRevision": revision,
        "mediaType": "application/json",
        "evaluationResultIds": [PLAN_EVALUATION_ID],
    }


def implementation_plan(
    intended_files: list[str],
    *,
    no_change_files: list[str] | None = None,
    required_insertions: list[dict[str, str]] | None = None,
) -> dict:
    return {
        "intendedFiles": intended_files,
        "noChangeFiles": no_change_files or [],
        "requiredInsertions": required_insertions or [],
        "tests": ["python -m pytest"],
        "assumptions": ["The checkout is revision-bound."],
        "risks": ["A change may exceed the plan scope."],
        "implementationSteps": ["Apply scoped changes.", "Evaluate the patch."],
    }


def repository_provider(revision: str) -> InMemoryRepositoryKnowledgeProvider:
    provenance = SourceProvenance(
        source_path="src/app.py",
        repository_revision=revision,
        scanned_at=TIMESTAMP,
        scanner_version="mvp-scanner/1.0.0",
    )
    return InMemoryRepositoryKnowledgeProvider(
        RepositoryKnowledgeSnapshot(
            api_version="aep.dev/repository-knowledge/v1",
            snapshot_version="snapshot-patch-v1",
            repository_revision=revision,
            created_at=TIMESTAMP,
            scanner_version="mvp-scanner/1.0.0",
            files=(
                RepositoryFile(
                    path="src/app.py",
                    language="Python",
                    is_documentation=False,
                    provenance=provenance,
                ),
            ),
            documentation=(),
            dependency_manifests=(),
            test_command_hints=(),
        )
    )


def resource(kind: str, name: str, spec: dict) -> Resource:
    resource_ref = ResourceRef(kind, name, "1.0.0")
    return Resource(
        ref=resource_ref,
        path=Path(f".ai/{kind.lower()}s/{name}.yaml"),
        data={
            "apiVersion": "aep.dev/v1alpha1",
            "kind": kind,
            "metadata": {"name": name, "version": "1.0.0"},
            "spec": spec,
        },
        references=(),
    )


def ref(kind: str, name: str) -> dict[str, str]:
    return {"kind": kind, "name": name, "version": "1.0.0"}


def validate_runtime(kind: str, value) -> None:
    paths = (
        ROOT / "schemas/resources/v1/resource-definitions.schema.json",
        ROOT / "schemas/runtime/v1/runtime-definitions.schema.json",
        ROOT / f"schemas/runtime/v1/{kind.lower()}.schema.json",
    )
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    registry = Registry().with_resources(
        (
            schema["$id"],
            SchemaResource.from_contents(schema, default_specification=DRAFT202012),
        )
        for schema in schemas
    )
    Draft202012Validator(schemas[-1], registry=registry).validate(dict(value))
