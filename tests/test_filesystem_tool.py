from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
from threading import Event, Lock

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.capability_policy import (
    ApplicablePolicy,
    PolicyScope,
    PreExecutionCapabilityPolicy,
)
from aep.filesystem_tool import (
    FILESYSTEM_INPUT_SCHEMA,
    FILESYSTEM_OUTPUT_SCHEMA,
    FilesystemInvocationIdentityConflictError,
    FilesystemTool,
    FilesystemToolAdapter,
)
from aep.runtime_store import InMemoryRuntimeObjectStore
from aep.tool_runtime import (
    ToolCaller,
    ToolFailureClass,
    ToolRequest,
    ToolResultStatus,
)


ROOT = Path(__file__).parents[1]
TASK_EXECUTION_ID = "taskexecution-123456789abc"


def request(
    operation: str,
    path: str,
    *,
    caller_kind: str | None = None,
    **values: object,
) -> ToolRequest:
    caller_kind = caller_kind or (
        "ContextBuilder" if operation == "read" else "AgentInvocation"
    )
    return ToolRequest(
        tool_ref={"kind": "Tool", "name": "filesystem", "version": "1.0.0"},
        input={"operation": operation, "path": path, **values},
        caller=ToolCaller(
            kind=caller_kind,
            id=(
                "taskexecution-123456789abc"
                if caller_kind == "TaskExecution"
                else "agentinvocation-123456789abc"
            ),
        ),
        capabilities=(f"filesystem.{operation}",),
        timeout_ms=1000,
        correlation={
            "traceId": "trace-filesystem-123",
            "workflowExecutionId": "workflowexecution-123456789abc",
            "taskExecutionId": "taskexecution-123456789abc",
        },
    )


def policy(effect: str, capability: str) -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "Policy",
        "metadata": {"name": "filesystem-access", "version": "1.0.0"},
        "spec": {
            "type": "pre-execution-capability",
            "rules": [{"effect": effect, "capabilities": [capability]}],
        },
    }


def authorization(
    store: InMemoryRuntimeObjectStore, effect: str, capability: str
):
    return PreExecutionCapabilityPolicy(store).tool_authorization_boundary(
        task_execution_id=TASK_EXECUTION_ID,
        resource_scope={
            "toolRef": {"kind": "Tool", "name": "filesystem", "version": "1.0.0"}
        },
        execution_context={"workspace": "test"},
        applicable_policies=[
            ApplicablePolicy(PolicyScope.TOOL, policy(effect, capability))
        ],
        timestamp="2026-07-30T12:00:00Z",
    )


def invoke(
    tool: FilesystemTool,
    store: InMemoryRuntimeObjectStore,
    tool_request: ToolRequest,
    *,
    authorize=None,
    suffix: str = "123456789abc",
):
    return tool.invoke(
        invocation_id=f"toolinvocation-{suffix}",
        task_execution_id=TASK_EXECUTION_ID,
        request=tool_request,
        authorize=authorize or (lambda _: True),
    )


def _can_create_symlink(directory: Path, target: Path) -> bool:
    probe = directory / "symlink-probe"
    try:
        probe.symlink_to(target)
    except OSError:
        return False
    else:
        probe.unlink()
        return True


def test_allowed_read_records_output_log_and_toolinvocation(tmp_path: Path) -> None:
    source = tmp_path / "src" / "message.txt"
    source.parent.mkdir()
    source.write_bytes(b"hello\n")
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)

    result, invocation = invoke(tool, store, request("read", "src/message.txt"))

    assert result.status is ToolResultStatus.SUCCEEDED
    assert result.output == {
        "operation": "read",
        "path": "src/message.txt",
        "content": "hello\n",
        "sizeBytes": 6,
        "sha256": "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03",
    }
    assert invocation["output"] == result.output
    assert invocation["logsAddress"] == result.logs_ref
    assert invocation["metrics"]["durationMs"] >= 0
    assert tool.adapter.get_log(result.logs_ref)["status"] == "SUCCEEDED"
    assert store.get(invocation["id"]) == invocation


def test_allowed_write_is_authorized_before_mutation_and_records_evidence(
    tmp_path: Path,
) -> None:
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)
    authorize = authorization(store, "allow", "filesystem.write")

    result, invocation = invoke(
        tool,
        store,
        request("write", "generated.txt", content="new content"),
        authorize=authorize,
    )

    assert (tmp_path / "generated.txt").read_text(encoding="utf-8") == "new content"
    assert result.status is ToolResultStatus.SUCCEEDED
    assert result.output["bytesWritten"] == 11
    evidence = store.list_by_task_execution(TASK_EXECUTION_ID)
    assert [item["kind"] for item in evidence] == ["ToolInvocation", "PolicyDecision"]
    assert evidence[1]["action"] == "filesystem.write"
    assert evidence[1]["decision"] == "ALLOW"
    assert invocation["capabilities"] == ["filesystem.write"]
    assert invocation["requestFingerprint"].startswith("sha256:")


def test_denied_write_does_not_mutate_workspace(tmp_path: Path) -> None:
    target = tmp_path / "protected.txt"
    target.write_text("original", encoding="utf-8")
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)

    result, invocation = invoke(
        tool,
        store,
        request("write", "protected.txt", content="changed"),
        authorize=authorization(store, "deny", "filesystem.write"),
    )

    assert result.status is ToolResultStatus.DENIED
    assert result.failure_class is ToolFailureClass.POLICY
    assert target.read_text(encoding="utf-8") == "original"
    assert invocation["failure"]["class"] == "POLICY"


def test_agent_cannot_read_repository_files_even_with_capability_and_policy_allow(
    tmp_path: Path,
) -> None:
    target = tmp_path / "repository-secret.txt"
    target.write_text("must enter a ContextPackage", encoding="utf-8")
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)

    result, invocation = invoke(
        tool,
        store,
        request(
            "read",
            "repository-secret.txt",
            caller_kind="AgentInvocation",
        ),
        authorize=authorization(store, "allow", "filesystem.read"),
    )

    assert result.failure_class is ToolFailureClass.POLICY
    assert result.output is None
    assert invocation["failure"]["class"] == "POLICY"


@pytest.mark.parametrize(
    "caller_kind", ["ContextBuilder", "TaskExecution", "WorkflowRuntime"]
)
def test_explicit_trusted_control_plane_callers_can_read(
    tmp_path: Path, caller_kind: str
) -> None:
    target = tmp_path / "context.txt"
    target.write_text("selected context", encoding="utf-8")
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)

    result, _ = invoke(
        tool,
        store,
        request("read", "context.txt", caller_kind=caller_kind),
    )

    assert result.output["content"] == "selected context"


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "nested/../../outside.txt",
        (r"C:\\outside.txt" if os.name == "nt" else "/outside.txt"),
    ],
)
def test_path_traversal_and_absolute_paths_are_denied(
    tmp_path: Path, path: str
) -> None:
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)

    result, invocation = invoke(
        tool, store, request("read", path), suffix=hex(abs(hash(path)))[2:] + "abcdef"
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.failure_class is ToolFailureClass.BOUNDARY
    assert invocation["failureClass"] == "BOUNDARY"
    assert invocation["failure"]["class"] == "POLICY"


def test_symlink_escape_is_denied(tmp_path: Path, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)

    result, _ = invoke(tool, store, request("read", "escape.txt"))

    assert result.failure_class is ToolFailureClass.BOUNDARY
    assert result.output is None


def test_read_replace_race_is_rejected_before_content_is_read(
    tmp_path: Path, tmp_path_factory
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("inside", encoding="utf-8")
    outside = tmp_path_factory.mktemp("outside-read") / "secret.txt"
    outside.write_text("outside secret", encoding="utf-8")
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)
    adapter: FilesystemToolAdapter
    use_real_symlink = _can_create_symlink(tmp_path, outside)

    def replace_after_validation(path: Path, _operation: str) -> None:
        if use_real_symlink:
            path.unlink()
            path.symlink_to(outside)
        else:
            adapter._handle_path_resolver = lambda _descriptor: outside

    adapter = FilesystemToolAdapter(tmp_path, before_open=replace_after_validation)
    tool.adapter = adapter

    result, _ = invoke(tool, store, request("read", "source.txt"))

    assert result.failure_class is ToolFailureClass.BOUNDARY
    assert result.output is None


def test_write_replace_race_is_rejected_before_truncate_or_write(
    tmp_path: Path, tmp_path_factory
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("inside original", encoding="utf-8")
    outside = tmp_path_factory.mktemp("outside-write") / "victim.txt"
    outside.write_text("outside original", encoding="utf-8")
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)
    adapter: FilesystemToolAdapter
    use_real_symlink = _can_create_symlink(tmp_path, outside)

    def replace_after_validation(path: Path, _operation: str) -> None:
        if use_real_symlink:
            path.unlink()
            path.symlink_to(outside)
        else:
            adapter._handle_path_resolver = lambda _descriptor: outside

    adapter = FilesystemToolAdapter(tmp_path, before_open=replace_after_validation)
    tool.adapter = adapter

    result, _ = invoke(
        tool, store, request("write", "target.txt", content="malicious write")
    )

    assert result.failure_class is ToolFailureClass.BOUNDARY
    # The race hook intentionally replaced this directory entry with a link;
    # assert the original inode was not opened for truncation and the external
    # target was not modified.
    if use_real_symlink:
        assert target.is_symlink()
    else:
        assert target.read_text(encoding="utf-8") == "inside original"
    assert outside.read_text(encoding="utf-8") == "outside original"


def test_raced_intermediate_link_cannot_create_nonexistent_outside_file(
    tmp_path: Path, tmp_path_factory
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    outside = tmp_path_factory.mktemp("outside-create")
    outside_target = outside / "must-not-exist.txt"
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)

    def replace_parent(_path: Path, _operation: str) -> None:
        nested.rmdir()
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(nested), str(outside)],
                check=False,
                capture_output=True,
                text=True,
            )
            assert completed.returncode == 0, completed.stderr
        else:
            nested.symlink_to(outside, target_is_directory=True)

    tool.adapter._before_open = replace_parent
    try:
        result, _ = invoke(
            tool,
            store,
            request("write", "nested/must-not-exist.txt", content="escape"),
        )
    finally:
        if nested.is_symlink():
            nested.unlink()
        elif nested.exists():
            nested.rmdir()

    assert result.failure_class is ToolFailureClass.BOUNDARY
    assert not outside_target.exists()


def test_invalid_input_is_schema_failure_and_is_persisted(tmp_path: Path) -> None:
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)
    invalid = request("write", "missing-content.txt")

    result, invocation = invoke(tool, store, invalid)

    assert result.failure_class is ToolFailureClass.VALIDATION
    assert invocation["failureClass"] == "VALIDATION"
    assert invocation["failure"]["class"] == "CONFIGURATION"
    assert not (tmp_path / "missing-content.txt").exists()


def test_missing_file_and_io_failures_are_distinct(tmp_path: Path) -> None:
    invalid_utf8 = tmp_path / "binary.dat"
    invalid_utf8.write_bytes(b"\xff")
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)

    missing, _ = invoke(
        tool, store, request("read", "missing.txt"), suffix="missing123456789abc"
    )
    io_failure, invocation = invoke(
        tool, store, request("read", "binary.dat"), suffix="ioerror123456789abc"
    )

    assert missing.failure_class is ToolFailureClass.NOT_FOUND
    assert io_failure.failure_class is ToolFailureClass.IO
    assert invocation["failure"]["retryable"] is True
    assert tool.adapter.get_log(io_failure.logs_ref)["failureClass"] == "IO"


def test_write_capability_cannot_be_omitted_even_with_permissive_hook(
    tmp_path: Path,
) -> None:
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)
    tool_request = ToolRequest(
        tool_ref={"kind": "Tool", "name": "filesystem", "version": "1.0.0"},
        input={"operation": "write", "path": "blocked.txt", "content": "blocked"},
        caller=ToolCaller(
            kind="AgentInvocation", id="agentinvocation-123456789abc"
        ),
        capabilities=("filesystem.read",),
        timeout_ms=1000,
        correlation={
            "traceId": "trace-filesystem-123",
            "workflowExecutionId": "workflowexecution-123456789abc",
            "taskExecutionId": "taskexecution-123456789abc",
        },
    )

    result, _ = invoke(tool, store, tool_request)

    assert result.failure_class is ToolFailureClass.POLICY
    assert not (tmp_path / "blocked.txt").exists()


def test_identical_retry_returns_terminal_evidence_without_repeating_effect(
    tmp_path: Path,
) -> None:
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)
    calls = 0

    def before_open(_path: Path, _operation: str) -> None:
        nonlocal calls
        calls += 1

    tool.adapter._before_open = before_open
    tool_request = request("write", "retry.txt", content="once")

    first_result, first_evidence = invoke(tool, store, tool_request)
    second_result, second_evidence = invoke(tool, store, tool_request)

    assert calls == 1
    assert second_result.output == first_result.output
    assert second_evidence == first_evidence
    assert len(store.list_by_task_execution(TASK_EXECUTION_ID)) == 1


def test_invocation_id_reuse_with_different_request_is_rejected_before_effect(
    tmp_path: Path,
) -> None:
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)
    invoke(tool, store, request("write", "first.txt", content="first"))

    with pytest.raises(
        FilesystemInvocationIdentityConflictError,
        match="different immutable request inputs",
    ):
        invoke(tool, store, request("write", "second.txt", content="second"))

    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "first"
    assert not (tmp_path / "second.txt").exists()


def test_concurrent_identical_invocations_execute_effect_once(
    tmp_path: Path,
) -> None:
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)
    entered = Event()
    release = Event()
    count_lock = Lock()
    calls = 0

    def before_open(_path: Path, _operation: str) -> None:
        nonlocal calls
        with count_lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=2)

    tool.adapter._before_open = before_open
    tool_request = request("write", "concurrent.txt", content="one effect")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(invoke, tool, store, tool_request)
        assert entered.wait(timeout=2)
        assert store.get("toolinvocation-123456789abc")["status"] == "PENDING"
        second = executor.submit(invoke, tool, store, tool_request)
        release.set()
        first_result = first.result(timeout=2)
        second_result = second.result(timeout=2)

    assert calls == 1
    assert first_result[0].output == second_result[0].output
    assert first_result[1] == second_result[1]
    assert (tmp_path / "concurrent.txt").read_text(encoding="utf-8") == "one effect"


def test_atomic_pending_create_failure_is_retryable_without_duplicate_effect(
    tmp_path: Path,
) -> None:
    class FailFirstPendingCreateStore(InMemoryRuntimeObjectStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail_pending_create = True

        def create(self, runtime_object, *, deterministic_key):
            if (
                self.fail_pending_create
                and runtime_object.get("kind") == "ToolInvocation"
            ):
                self.fail_pending_create = False
                raise RuntimeError("atomic persistence unavailable")
            return super().create(
                runtime_object,
                deterministic_key=deterministic_key,
            )

    store = FailFirstPendingCreateStore()
    tool = FilesystemTool(tmp_path, store)
    effects = 0

    def count_effect(_path: Path, _operation: str) -> None:
        nonlocal effects
        effects += 1

    tool.adapter._before_open = count_effect
    tool_request = request("write", "recovered.txt", content="once")

    with pytest.raises(RuntimeError, match="atomic persistence unavailable"):
        invoke(tool, store, tool_request)

    assert store.get("toolinvocation-123456789abc") is None
    assert effects == 0
    result, invocation = invoke(tool, store, tool_request)

    assert result.status is ToolResultStatus.SUCCEEDED
    assert invocation["status"] == "SUCCEEDED"
    assert effects == 1
    assert (tmp_path / "recovered.txt").read_text(encoding="utf-8") == "once"


def test_published_schemas_match_runtime_contract_constants() -> None:
    input_schema = json.loads(
        (ROOT / "schemas/tools/v1/filesystem-input.schema.json").read_text()
    )
    output_schema = json.loads(
        (ROOT / "schemas/tools/v1/filesystem-output.schema.json").read_text()
    )

    assert input_schema == {
        "$id": input_schema["$id"],
        "title": input_schema["title"],
        **FILESYSTEM_INPUT_SCHEMA,
    }
    assert output_schema == {
        "$id": output_schema["$id"],
        "title": output_schema["title"],
        **FILESYSTEM_OUTPUT_SCHEMA,
    }
    fixture = json.loads(
        (ROOT / "fixtures/tool-runtime/filesystem-read-success.json").read_text()
    )
    Draft202012Validator(input_schema).validate(fixture["request"]["input"])
    Draft202012Validator(output_schema).validate(fixture["output"])


def test_persisted_invocation_satisfies_runtime_schema(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("content", encoding="utf-8")
    store = InMemoryRuntimeObjectStore()
    tool = FilesystemTool(tmp_path, store)
    _, invocation = invoke(tool, store, request("read", "file.txt"))
    schema_paths = [
        ROOT / "schemas/resources/v1/resource-definitions.schema.json",
        ROOT / "schemas/runtime/v1/runtime-definitions.schema.json",
        ROOT / "schemas/runtime/v1/toolinvocation.schema.json",
    ]
    schemas = [json.loads(path.read_text()) for path in schema_paths]
    registry = Registry()
    for schema in schemas:
        registry = registry.with_resource(
            schema["$id"],
            SchemaResource.from_contents(
                schema, default_specification=DRAFT202012
            ),
        )

    assert list(
        Draft202012Validator(schemas[-1], registry=registry).iter_errors(
            dict(invocation)
        )
    ) == []
