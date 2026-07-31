from __future__ import annotations

import json
from pathlib import Path

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
    FilesystemTool,
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


def request(operation: str, path: str, **values: object) -> ToolRequest:
    return ToolRequest(
        tool_ref={"kind": "Tool", "name": "filesystem", "version": "1.0.0"},
        input={"operation": operation, "path": path, **values},
        caller=ToolCaller(
            kind="AgentInvocation", id="agentinvocation-123456789abc"
        ),
        capabilities=(f"filesystem.{operation}",),
        timeout_ms=1000,
        trace_id="trace-filesystem-123",
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


def test_allowed_read_records_output_log_and_toolinvocation(tmp_path: Path) -> None:
    source = tmp_path / "src" / "message.txt"
    source.parent.mkdir()
    source.write_text("hello\n", encoding="utf-8")
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
    assert [item["kind"] for item in evidence] == ["PolicyDecision", "ToolInvocation"]
    assert evidence[0]["action"] == "filesystem.write"
    assert evidence[0]["decision"] == "ALLOW"
    assert invocation["capabilities"] == ["filesystem.write"]


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


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "nested/../../outside.txt", str(Path("C:/outside.txt"))],
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
        trace_id="trace-filesystem-123",
    )

    result, _ = invoke(tool, store, tool_request)

    assert result.failure_class is ToolFailureClass.POLICY
    assert not (tmp_path / "blocked.txt").exists()


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
