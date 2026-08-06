import json
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012
import pytest

from aep.observability import (
    OMITTED,
    REDACTED,
    CorrelationContext,
    ObservabilityContractError,
    StructuredLifecycleLogger,
    assert_trace_continuity,
    lifecycle_log,
    propagation_fields,
    redact,
)
from aep.resource_loader import ResourceLoader
from aep.runtime_store import InMemoryRuntimeObjectStore
from aep.task_execution import TaskExecutionLifecycle
from aep.workflow_execution import WorkflowExecutionCreator


ROOT = Path(__file__).parents[1]
TRACE_ID = "trace-fake-workflow-0001"
WORKFLOW_ID = "workflowexecution-000000000001"
TASK_ID = "taskexecution-000000000001"
TIMESTAMP = "2026-08-04T18:00:00Z"
REVISION = "abc1234"
WORKFLOW_REF = {"kind": "Workflow", "name": "issue-to-pr", "version": "1.0.0"}
TASK_REF = {"kind": "Task", "name": "analyze-issue", "version": "1.0.0"}


def runtime_record(kind: str, suffix: int, **values) -> dict[str, object]:
    record: dict[str, object] = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": kind,
        "id": f"{kind.lower()}-{suffix:012x}",
        "traceId": TRACE_ID,
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "fake-workflow",
            "workflowExecutionId": WORKFLOW_ID,
            "taskExecutionId": TASK_ID,
            "repositoryRevision": REVISION,
            "resourceRefs": [WORKFLOW_REF, TASK_REF],
        },
    }
    record.update(values)
    return record


def fake_workflow() -> list[dict[str, object]]:
    workflow = runtime_record(
        "WorkflowExecution",
        1,
        id=WORKFLOW_ID,
        provenance={
            "actor": "workflow-controller",
            "repositoryRevision": REVISION,
            "resourceRefs": [WORKFLOW_REF],
        },
        workflowRef=WORKFLOW_REF,
        repositoryRevision=REVISION,
        status="RUNNING",
    )
    task = runtime_record(
        "TaskExecution",
        1,
        id=TASK_ID,
        workflowExecutionId=WORKFLOW_ID,
        taskRef=TASK_REF,
        status="RUNNING",
    )
    return [
        workflow,
        task,
        runtime_record("ContextPackage", 1, taskExecutionId=TASK_ID, taskRef=TASK_REF),
        runtime_record("AgentInvocation", 1, taskExecutionId=TASK_ID, status="SUCCEEDED"),
        runtime_record(
            "ToolInvocation",
            1,
            taskExecutionId=TASK_ID,
            toolRef={"kind": "Tool", "name": "git", "version": "1.0.0"},
            status="SUCCEEDED",
        ),
        runtime_record(
            "EvaluationResult",
            1,
            taskExecutionId=TASK_ID,
            evaluationRef={
                "kind": "Evaluation",
                "name": "schema-check",
                "version": "1.0.0",
            },
            status="SUCCEEDED",
        ),
        runtime_record(
            "PolicyDecision",
            1,
            taskExecutionId=TASK_ID,
            policyRefs=[
                {"kind": "Policy", "name": "safe-publish", "version": "1.0.0"}
            ],
        ),
    ]


def test_trace_propagates_across_fake_workflow_and_service_boundaries() -> None:
    records = fake_workflow()

    assert assert_trace_continuity(records) == TRACE_ID
    root_fields = propagation_fields(records[0], task_execution_id=TASK_ID)
    assert root_fields == {
        "traceId": TRACE_ID,
        "workflowExecutionId": WORKFLOW_ID,
        "taskExecutionId": TASK_ID,
    }
    assert CorrelationContext.from_boundary_fields(root_fields) == (
        CorrelationContext(TRACE_ID, WORKFLOW_ID, TASK_ID)
    )
    for record in records[1:]:
        context = CorrelationContext.from_runtime_object(record)
        assert context.trace_id == TRACE_ID
        assert context.workflow_execution_id == WORKFLOW_ID
        assert context.task_execution_id == TASK_ID


def test_lifecycle_logs_include_required_correlation_and_provenance() -> None:
    captured: list[dict[str, object]] = []
    logger = StructuredLifecycleLogger(lambda record: captured.append(dict(record)))

    event_by_kind = {
        "TaskExecution": ("TaskExecutionStarted", "RUNNING"),
        "ContextPackage": ("ContextPackageCreated", "CREATED"),
        "AgentInvocation": ("AgentInvocationCompleted", "SUCCEEDED"),
        "ToolInvocation": ("ToolInvocationCompleted", "SUCCEEDED"),
        "EvaluationResult": ("EvaluationCompleted", "SUCCEEDED"),
        "PolicyDecision": ("PolicyDecisionRecorded", "RECORDED"),
    }
    for runtime_object in fake_workflow()[1:]:
        event_name, status = event_by_kind[str(runtime_object["kind"])]
        record = logger.emit(
            event_name=event_name,
            service="fake-workflow",
            runtime_object=runtime_object,
            emitted_at=TIMESTAMP,
            status=status,
            duration_ms=7,
        )
        assert record["executionId"] == WORKFLOW_ID
        assert record["taskId"] == TASK_ID
        assert record["traceId"] == TRACE_ID
        assert record["repositoryRevision"] == REVISION
        assert record["status"]
        assert record["durationMs"] == 7
        assert WORKFLOW_REF in record["resourceVersions"]
        assert TASK_REF in record["resourceVersions"]
    assert len(captured) == 6


def test_failure_log_carries_failure_class_and_error_level() -> None:
    failed = fake_workflow()[1]
    failed["status"] = "FAILED"
    failed["failure"] = {
        "class": "RECOVERABLE",
        "message": "provider unavailable",
        "retryable": True,
    }

    record = lifecycle_log(
        event_name="TaskExecutionFailed",
        service="workflow-runtime",
        runtime_object=failed,
        emitted_at=TIMESTAMP,
    )

    assert record["failureClass"] == "RECOVERABLE"
    assert record["level"] == "ERROR"


def test_redaction_removes_secrets_and_artifact_bodies_recursively() -> None:
    attributes = {
        "authorization": "Bearer top-secret",
        "request": {
            "api_key": "secret-key",
            "note": "Bearer another-secret",
            "safe": "visible",
        },
        "artifact": {"content": "large generated patch", "contentAddress": "sha256:abc"},
        "oversize": "x" * 4097,
        "headers": {
            "X-Api-Key": "header-key",
            "X-Auth-Token": "header-token",
            "Accept": "application/json",
        },
        "environment": {
            "DATABASE_URL": "postgresql://user:password@database/aep",
            "SERVICE_TOKEN": "env-token",
            "LOG_LEVEL": "INFO",
        },
        "connection": "postgresql://user:password@database/aep",
        "tokenUsage": {"input": 10, "output": 2},
        "secret_key": "x",
        "access_token_value": "y",
        "credential_blob": "z",
        "artifact_patch": "diff --git a/secret b/secret",
        "generated_artifact_payload": {"private": "material"},
        "artifacts": [
            {
                "id": "generatedartifact-000000000001",
                "payload": "first body",
                "patch": "second body",
                "metadata": {"contentAddress": "sha256:def"},
            }
        ],
        "nested": [{"map": {"refresh_token_value": "short"}}],
    }
    original = deepcopy(attributes)

    cleaned = redact(attributes)

    assert cleaned["authorization"] == REDACTED
    assert cleaned["request"]["api_key"] == REDACTED
    assert cleaned["request"]["note"] == REDACTED
    assert cleaned["request"]["safe"] == "visible"
    assert cleaned["artifact"]["content"] == OMITTED
    assert cleaned["artifact"]["contentAddress"] == "sha256:abc"
    assert cleaned["oversize"] == OMITTED
    assert cleaned["headers"]["X-Api-Key"] == REDACTED
    assert cleaned["headers"]["X-Auth-Token"] == REDACTED
    assert cleaned["headers"]["Accept"] == "application/json"
    assert cleaned["environment"]["DATABASE_URL"] == REDACTED
    assert cleaned["environment"]["SERVICE_TOKEN"] == REDACTED
    assert cleaned["environment"]["LOG_LEVEL"] == "INFO"
    assert cleaned["connection"] == REDACTED
    assert cleaned["tokenUsage"] == {"input": 10, "output": 2}
    assert cleaned["secret_key"] == REDACTED
    assert cleaned["access_token_value"] == REDACTED
    assert cleaned["credential_blob"] == REDACTED
    assert cleaned["artifact_patch"] == OMITTED
    assert cleaned["generated_artifact_payload"] == OMITTED
    assert cleaned["artifacts"][0]["payload"] == OMITTED
    assert cleaned["artifacts"][0]["patch"] == OMITTED
    assert cleaned["artifacts"][0]["metadata"]["contentAddress"] == "sha256:def"
    assert cleaned["nested"][0]["map"]["refresh_token_value"] == REDACTED
    assert "top-secret" not in json.dumps(cleaned)
    assert attributes == original


def test_trace_continuity_rejects_mismatch() -> None:
    records = fake_workflow()
    records[-1]["traceId"] = "trace-other-workflow"

    with pytest.raises(ObservabilityContractError, match="one traceId"):
        assert_trace_continuity(records)


@pytest.mark.parametrize(
    ("kind", "direct_field", "provenance_field"),
    [
        ("TaskExecution", "workflowExecutionId", "workflowExecutionId"),
        ("ToolInvocation", "taskExecutionId", "taskExecutionId"),
    ],
)
def test_runtime_correlation_rejects_direct_and_provenance_conflicts(
    kind, direct_field, provenance_field
) -> None:
    value = runtime_record(kind, 88)
    value[direct_field] = "workflowexecution-000000000099" if "workflow" in direct_field.lower() else "taskexecution-000000000099"
    value["provenance"][provenance_field] = (
        "workflowexecution-000000000001"
        if "workflow" in provenance_field.lower()
        else "taskexecution-000000000001"
    )

    with pytest.raises(ObservabilityContractError, match="conflicting"):
        CorrelationContext.from_runtime_object(value)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"event_name": "task-started"}, "PascalCase"),
        ({"emitted_at": "not-a-time"}, "RFC3339"),
    ],
)
def test_lifecycle_log_rejects_invalid_common_fields(change, message) -> None:
    arguments = {
        "event_name": "TaskExecutionStarted",
        "service": "workflow-runtime",
        "runtime_object": fake_workflow()[1],
        "emitted_at": TIMESTAMP,
    }
    arguments.update(change)

    with pytest.raises(ObservabilityContractError, match=message):
        lifecycle_log(**arguments)


@pytest.mark.parametrize(
    ("event_name", "changes", "message"),
    [
        ("TaskExecutionFailed", {"status": "SUCCEEDED"}, "incompatible"),
        ("ToolInvocationCompleted", {"kind": "TaskExecution"}, "runtimeKind"),
        ("TaskExecutionFailed", {"status": "FAILED"}, "failure class"),
        (
            "TaskExecutionSucceeded",
            {
                "status": "SUCCEEDED",
                "failure": {"class": "PERMANENT", "message": "impossible"},
            },
            "must not carry",
        ),
    ],
)
def test_lifecycle_log_rejects_semantically_impossible_events(
    event_name, changes, message
) -> None:
    runtime_object = deepcopy(fake_workflow()[1])
    runtime_object.update(changes)

    with pytest.raises(ObservabilityContractError, match=message):
        lifecycle_log(
            event_name=event_name,
            service="workflow-runtime",
            runtime_object=runtime_object,
            emitted_at=TIMESTAMP,
        )


def test_authoritative_runtime_fixtures_share_trace_and_are_schema_valid() -> None:
    fixture_events = {
        "successful-workflowexecution.json": ("workflowexecution", "WorkflowExecutionCompleted", None),
        "successful-taskexecution.json": ("taskexecution", "TaskExecutionSucceeded", None),
        "contextpackage.json": ("contextpackage", "ContextPackageCreated", "CREATED"),
        "resolvedagent.json": ("resolvedagent", "AgentResolved", "CREATED"),
        "successful-agentinvocation.json": ("agentinvocation", "AgentInvocationCompleted", None),
        "successful-modelinvocation.json": ("modelinvocation", "ModelInvocationCompleted", None),
        "successful-toolinvocation.json": ("toolinvocation", "ToolInvocationCompleted", None),
        "successful-evaluationresult.json": ("evaluationresult", "EvaluationCompleted", None),
        "allowed-policydecision.json": ("policydecision", "PolicyDecisionRecorded", "RECORDED"),
        "pending-approval.json": ("approval", "ApprovalRequested", None),
        "generatedartifact.json": ("generatedartifact", "GeneratedArtifactCreated", "CREATED"),
    }
    runtime_objects = []
    for filename, (schema_name, event_name, status) in fixture_events.items():
        value = json.loads(
            (ROOT / "fixtures/runtime/valid" / filename).read_text(encoding="utf-8")
        )
        value["provenance"].setdefault("repositoryRevision", REVISION)
        _runtime_validator(schema_name).validate(value)
        lifecycle_log(
            event_name=event_name,
            service="fixture-contract",
            runtime_object=value,
            emitted_at=value["updatedAt"],
            status=status,
        )
        runtime_objects.append(value)

    execution_event = json.loads(
        (ROOT / "fixtures/runtime/valid/executionevent.json").read_text(
            encoding="utf-8"
        )
    )
    _runtime_validator("executionevent").validate(execution_event)
    runtime_objects.append(execution_event)
    assert assert_trace_continuity(runtime_objects) == "trace-issue-to-pr-0001"


def test_real_workflow_and_task_producers_emit_one_correlated_trace() -> None:
    captured: list[dict[str, object]] = []
    logger = StructuredLifecycleLogger(lambda value: captured.append(dict(value)))
    store = InMemoryRuntimeObjectStore()
    resources = ResourceLoader(
        ROOT / "fixtures/resource-loader/workflow-resolution"
    ).load()
    workflow = resources.by_kind("Workflow")[0]
    event_resource = resources.by_kind("Event")[0]
    payload = json.loads(
        (ROOT / "fixtures/github/issue-created.json").read_text(encoding="utf-8")
    )
    execution = WorkflowExecutionCreator(
        store, lifecycle_logger=logger
    ).create(
        event={
            "id": "event-123456789abc",
            "source": "github",
            "type": "github.issue.created",
            "repository": payload["repository"],
            "issue": payload["issue"],
            "sender": payload["sender"],
            "receivedAt": TIMESTAMP,
            "deduplicationKey": "github:delivery:observability",
        },
        workflow=workflow,
        event_resource=event_resource,
        repository_revision=REVISION,
        knowledge_graph_version="kg-observability-1",
        timestamp=TIMESTAMP,
    )
    task_ref = {"kind": "Task", "name": "analyze-issue", "version": "1.0.0"}
    task_id = "taskexecution-000000000099"
    lifecycle = TaskExecutionLifecycle(store, lifecycle_logger=logger)
    pending = lifecycle.create(
        execution_id=task_id,
        workflow_execution_id=str(execution["id"]),
        task_ref=task_ref,
        attempt=1,
        correlation={
            "traceId": str(execution["traceId"]),
            "workflowExecutionId": str(execution["id"]),
            "taskExecutionId": task_id,
        },
        timestamp=TIMESTAMP,
        provenance={
            "actor": "workflow-runtime",
            "workflowExecutionId": execution["id"],
            "repositoryRevision": REVISION,
            "resourceRefs": [execution["workflowRef"], task_ref],
        },
    )
    running = lifecycle.start(task_id, timestamp="2026-08-04T18:00:01Z")
    succeeded = lifecycle.succeed(task_id, timestamp="2026-08-04T18:00:02Z")

    assert assert_trace_continuity((execution, pending, running, succeeded)) == execution["traceId"]
    assert [entry["eventName"] for entry in captured] == [
        "WorkflowExecutionStarted",
        "TaskExecutionQueued",
        "TaskExecutionStarted",
        "TaskExecutionSucceeded",
    ]
    assert {entry["traceId"] for entry in captured} == {execution["traceId"]}
    assert {entry["executionId"] for entry in captured} == {execution["id"]}


def test_lifecycle_fixture_matches_published_schema() -> None:
    schema = json.loads(
        (ROOT / "schemas/observability/v1/lifecycle-log.schema.json").read_text(
            encoding="utf-8"
        )
    )
    fixture = json.loads(
        (ROOT / "fixtures/observability/workflow-task-started.json").read_text(
            encoding="utf-8"
        )
    )

    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(fixture)

    impossible = deepcopy(fixture)
    impossible["status"] = "SUCCEEDED"
    with pytest.raises(ValidationError):
        validator.validate(impossible)


def _runtime_validator(schema_name: str) -> Draft202012Validator:
    paths = (
        ROOT / "schemas/resources/v1/resource-definitions.schema.json",
        ROOT / "schemas/runtime/v1/runtime-definitions.schema.json",
        ROOT / f"schemas/runtime/v1/{schema_name}.schema.json",
    )
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    registry = Registry().with_resources(
        (
            schema["$id"],
            SchemaResource.from_contents(schema, default_specification=DRAFT202012),
        )
        for schema in schemas
    )
    return Draft202012Validator(
        schemas[-1], registry=registry, format_checker=FormatChecker()
    )
