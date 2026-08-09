"""AgentInvocation coordination over immutable, pre-built runtime inputs.

The coordinator deliberately has no repository-knowledge dependency. All repository
and curated knowledge available to a model must already be present in the supplied
ContextPackage.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from functools import cache
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, SchemaError
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.model_invocation import (
    ModelAdapter,
    ModelConfiguration,
    ModelErrorClass,
    ModelInvocationError,
    ModelRequest,
)
from aep.observability import (
    CorrelationContext,
    ObservabilityContractError,
    StructuredLifecycleLogger,
)
from aep.runtime_store import (
    RuntimeObject,
    RuntimeObjectAlreadyExistsError,
    RuntimeObjectStore,
)


class AgentInvocationContractError(ValueError):
    """Raised before invocation when immutable runtime inputs conflict."""


def invoke_agent(
    *,
    store: RuntimeObjectStore,
    invocation_id: str,
    model_invocation_id: str,
    resolved_agent: Mapping[str, Any],
    context_package: Mapping[str, Any],
    prompt: Mapping[str, Any],
    model_configuration: ModelConfiguration,
    adapter: ModelAdapter,
    started_at: str,
    completed_at: str,
    lifecycle_logger: StructuredLifecycleLogger | None = None,
) -> RuntimeObject:
    """Run one bounded model-backed reasoning unit and persist all evidence.

    Inputs are copied and validated before persistence. The model sees only the
    resolved Prompt, output contract, and supplied ContextPackage; it cannot call
    the repository-knowledge query API through this boundary.
    """

    agent = _plain_mapping(resolved_agent, "resolved_agent")
    package = _plain_mapping(context_package, "context_package")
    prompt_resource = _plain_mapping(prompt, "prompt")
    _validate_inputs(agent, package, prompt_resource, model_configuration)
    assembled_input = _assemble_input(prompt_resource, package, agent["outputSchema"])

    correlation = CorrelationContext.from_runtime_object(agent)
    task_execution_id = agent["taskExecutionId"]
    repository_revision = package["repositoryRevision"]
    resource_refs = _resource_refs(agent, package)
    agent_provenance = {
        "actor": "agent-invocation-coordinator",
        "workflowExecutionId": correlation.workflow_execution_id,
        "taskExecutionId": task_execution_id,
        "repositoryRevision": repository_revision,
        "resourceRefs": resource_refs,
    }
    agent_record = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "AgentInvocation",
        "id": invocation_id,
        "traceId": correlation.trace_id,
        "createdAt": started_at,
        "updatedAt": started_at,
        "provenance": agent_provenance,
        "taskExecutionId": task_execution_id,
        "resolvedAgentId": agent["id"],
        "contextPackageId": package["id"],
        "status": "RUNNING",
        "modelInvocationIds": [model_invocation_id],
        "toolInvocationIds": [],
        "outputSchemaValidation": "NOT_RUN",
        "startedAt": started_at,
    }
    _validate_runtime(agent_record, "AgentInvocation")
    request = ModelRequest(
        configuration=model_configuration,
        input=assembled_input,
        correlation=correlation,
    )
    input_address = _content_address(assembled_input)
    model_provenance = {
        "actor": f"model-adapter:{model_configuration.provider}",
        "parentId": invocation_id,
        "workflowExecutionId": correlation.workflow_execution_id,
        "taskExecutionId": task_execution_id,
        "repositoryRevision": repository_revision,
        "resourceRefs": [deepcopy(dict(model_configuration.model_ref))],
    }
    model_record = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "ModelInvocation",
        "id": model_invocation_id,
        "traceId": correlation.trace_id,
        "createdAt": started_at,
        "updatedAt": started_at,
        "provenance": model_provenance,
        "agentInvocationId": invocation_id,
        "modelRef": deepcopy(dict(model_configuration.model_ref)),
        "modelConfiguration": model_configuration.as_record(),
        "status": "RUNNING",
        "inputAddress": input_address,
        "schemaValidation": "NOT_RUN",
        "startedAt": started_at,
    }
    _validate_runtime(model_record, "ModelInvocation")

    existing_agent = store.get(invocation_id)
    if existing_agent is not None:
        _validate_existing_agent(existing_agent, agent_record)
        if existing_agent.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return existing_agent
    existing_model = store.get(model_invocation_id)
    if existing_model is not None:
        if existing_agent is None or existing_model.get("agentInvocationId") != invocation_id:
            raise AgentInvocationContractError(
                f"ModelInvocation {model_invocation_id!r} already belongs to another invocation"
            )
        if existing_model.get("status") != "RUNNING":
            raise AgentInvocationContractError(
                f"ModelInvocation {model_invocation_id!r} is already terminal while its "
                "AgentInvocation is running"
            )

    pair = {
        "agentInvocationId": invocation_id,
        "modelInvocationId": model_invocation_id,
    }
    claimed, prior_pair = store.claim(
        f"agent-invocation-pair:{invocation_id}", pair
    )
    if not claimed:
        if dict(prior_pair) != pair:
            raise AgentInvocationContractError(
                f"AgentInvocation {invocation_id!r} is claimed for a different "
                "ModelInvocation"
            )
        current = store.get(invocation_id)
        return current if current is not None else agent_record
    if existing_model is not None:
        # A RUNNING child without an earlier pair claim has uncertain provider
        # state. Do not risk repeating that external side effect.
        return existing_agent

    saved_agent = store.create(
        agent_record, deterministic_key=f"agent-invocation:{invocation_id}"
    )
    _validate_existing_agent(saved_agent, agent_record)
    if saved_agent.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        return saved_agent
    if saved_agent.get("status") != "RUNNING" or dict(saved_agent) != agent_record:
        raise AgentInvocationContractError(
            f"AgentInvocation {invocation_id!r} already exists with conflicting state"
        )
    _emit(lifecycle_logger, "AgentInvocationStarted", saved_agent, started_at)

    try:
        saved_model = store.create(
            model_record, deterministic_key=f"model-invocation:{model_invocation_id}"
        )
    except RuntimeObjectAlreadyExistsError as error:
        failure = {
            "class": "CONFIGURATION",
            "message": f"ModelInvocation identity conflict: {model_invocation_id}",
            "retryable": False,
        }
        failed_agent = store.update_status(
            invocation_id,
            "FAILED",
            expected_status="RUNNING",
            updated_at=completed_at,
            changes={"failure": failure},
        )
        _validate_runtime(failed_agent, "AgentInvocation")
        _emit(lifecycle_logger, "AgentInvocationFailed", failed_agent, completed_at)
        raise AgentInvocationContractError(failure["message"]) from error
    if dict(saved_model) != model_record:
        failure = {
            "class": "CONFIGURATION",
            "message": f"ModelInvocation identity conflict: {model_invocation_id}",
            "retryable": False,
        }
        failed_agent = store.update_status(
            invocation_id,
            "FAILED",
            expected_status="RUNNING",
            updated_at=completed_at,
            changes={"failure": failure},
        )
        _validate_runtime(failed_agent, "AgentInvocation")
        _emit(lifecycle_logger, "AgentInvocationFailed", failed_agent, completed_at)
        raise AgentInvocationContractError(
            failure["message"]
        )
    _emit(lifecycle_logger, "ModelInvocationStarted", saved_model, started_at)

    try:
        response = adapter.invoke(request)
    except ModelInvocationError as error:
        failure = {
            "class": error.classification.value,
            "message": str(error) or error.code,
            "retryable": error.recoverable,
        }
        failed_model = store.update_status(
            model_invocation_id,
            "FAILED",
            expected_status="RUNNING",
            updated_at=completed_at,
            changes={
                "providerMetadata": {
                    **deepcopy(dict(error.provider_metadata)),
                    "errorCode": error.code,
                },
                "failure": failure,
            },
        )
        _validate_runtime(failed_model, "ModelInvocation")
        _emit(lifecycle_logger, "ModelInvocationFailed", failed_model, completed_at)
        failed_agent = store.update_status(
            invocation_id,
            "FAILED",
            expected_status="RUNNING",
            updated_at=completed_at,
            changes={
                "modelInvocationIds": [model_invocation_id],
                "failure": failure,
            },
        )
        _validate_runtime(failed_agent, "AgentInvocation")
        _emit(lifecycle_logger, "AgentInvocationFailed", failed_agent, completed_at)
        return failed_agent

    output, serialization_error = _json_output(response.output)
    validation_errors = (
        [serialization_error]
        if serialization_error is not None
        else _output_errors(output, agent["outputSchema"])
    )
    validation = "FAILED" if validation_errors else "PASSED"
    model_changes: dict[str, Any] = {
        "tokenUsage": response.usage.as_record(),
        "latencyMs": response.latency_ms,
        "providerMetadata": deepcopy(dict(response.provider_metadata)),
        "schemaValidation": validation,
    }
    if serialization_error is None:
        model_changes["outputAddress"] = _content_address(output)
    if response.cost is not None:
        model_changes["cost"] = response.cost
    completed_model = store.update_status(
        model_invocation_id,
        "SUCCEEDED",
        expected_status="RUNNING",
        updated_at=completed_at,
        changes=model_changes,
    )
    _validate_runtime(completed_model, "ModelInvocation")
    _emit(lifecycle_logger, "ModelInvocationCompleted", completed_model, completed_at)

    agent_changes: dict[str, Any] = {
        "modelInvocationIds": [model_invocation_id],
        "outputSchemaValidation": validation,
        "tokenUsage": response.usage.as_record(),
    }
    if serialization_error is None:
        agent_changes["output"] = output
    if response.cost is not None:
        agent_changes["cost"] = response.cost
    if validation_errors:
        agent_changes["failure"] = {
            "class": "EVALUATION",
            "message": "structured output does not match outputSchema: "
            + "; ".join(validation_errors),
            "retryable": False,
        }
    status = "FAILED" if validation_errors else "SUCCEEDED"
    completed_agent = store.update_status(
        invocation_id,
        status,
        expected_status="RUNNING",
        updated_at=completed_at,
        changes=agent_changes,
    )
    _validate_runtime(completed_agent, "AgentInvocation")
    _emit(
        lifecycle_logger,
        "AgentInvocationFailed" if validation_errors else "AgentInvocationCompleted",
        completed_agent,
        completed_at,
    )
    return completed_agent


def _validate_inputs(
    agent: dict[str, Any],
    package: dict[str, Any],
    prompt: dict[str, Any],
    configuration: ModelConfiguration,
) -> None:
    _validate_runtime(agent, "ResolvedAgent")
    _validate_runtime(package, "ContextPackage")
    try:
        agent_context = CorrelationContext.from_runtime_object(agent)
        package_context = CorrelationContext.from_runtime_object(package)
    except ObservabilityContractError as error:
        raise AgentInvocationContractError(
            f"invalid invocation correlation: {error}"
        ) from error
    if agent_context != package_context:
        raise AgentInvocationContractError(
            "ResolvedAgent and ContextPackage must share trace, WorkflowExecution, "
            "and TaskExecution identities"
        )
    if _resource_ref(prompt) != agent["promptRef"]:
        raise AgentInvocationContractError(
            "prompt must match the immutable ResolvedAgent.promptRef"
        )
    if dict(configuration.model_ref) != agent["modelRef"]:
        raise AgentInvocationContractError(
            "model_configuration must match the immutable ResolvedAgent.modelRef"
        )
    if dict(configuration.parameters) != agent.get("modelParameters", {}):
        raise AgentInvocationContractError(
            "model_configuration parameters must match ResolvedAgent.modelParameters"
        )
    if "modelConfiguration" in agent and configuration.as_record() != agent["modelConfiguration"]:
        raise AgentInvocationContractError(
            "model_configuration must match ResolvedAgent.modelConfiguration"
        )
    try:
        Draft202012Validator.check_schema(agent["outputSchema"])
    except (SchemaError, TypeError) as error:
        raise AgentInvocationContractError(
            f"ResolvedAgent.outputSchema is invalid: {error}"
        ) from error


def _validate_existing_agent(
    existing: Mapping[str, Any], expected: Mapping[str, Any]
) -> None:
    fields = (
        "id",
        "traceId",
        "taskExecutionId",
        "resolvedAgentId",
        "contextPackageId",
        "modelInvocationIds",
    )
    if any(existing.get(field) != expected[field] for field in fields):
        raise AgentInvocationContractError(
            f"AgentInvocation {expected['id']!r} already exists for different inputs"
        )


def _assemble_input(
    prompt: Mapping[str, Any], package: Mapping[str, Any], output_schema: Any
) -> dict[str, Any]:
    spec = prompt.get("spec")
    if not isinstance(spec, Mapping):
        raise AgentInvocationContractError("Prompt.spec must be an object")
    return {
        "prompt": _plain(spec),
        "contextPackage": {
            "id": package["id"],
            "repositoryRevision": package["repositoryRevision"],
            "elements": _plain(package["elements"]),
        },
        "outputSchema": _plain(output_schema),
    }


def _output_errors(output: Any, schema: Mapping[str, Any]) -> list[str]:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(output),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    messages = []
    for error in errors:
        parts = list(error.absolute_path)
        if error.validator == "required":
            missing = error.message.split("'", 2)
            if len(missing) >= 2:
                parts.append(missing[1])
        messages.append(f"{_json_path(parts)}: {error.message}")
    return messages


def _resource_ref(resource: Mapping[str, Any]) -> dict[str, str]:
    if resource.get("kind") != "Prompt" or not isinstance(resource.get("metadata"), Mapping):
        raise AgentInvocationContractError("prompt must be a versioned Prompt Resource")
    metadata = resource["metadata"]
    ref = {"kind": "Prompt", "name": metadata.get("name"), "version": metadata.get("version")}
    if not all(isinstance(value, str) and value for value in ref.values()):
        raise AgentInvocationContractError("prompt must be a versioned Prompt Resource")
    return ref


def _resource_refs(agent: Mapping[str, Any], package: Mapping[str, Any]) -> list[dict[str, str]]:
    refs: dict[tuple[str, str, str], dict[str, str]] = {}
    for runtime_object in (agent, package):
        provenance = runtime_object.get("provenance")
        candidates = provenance.get("resourceRefs", ()) if isinstance(provenance, Mapping) else ()
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            key = tuple(candidate.get(field) for field in ("kind", "name", "version"))
            if all(isinstance(part, str) and part for part in key):
                refs[key] = {"kind": key[0], "name": key[1], "version": key[2]}
    return [refs[key] for key in sorted(refs)]


def _content_address(value: Any) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return "sha256:" + sha256(canonical.encode("utf-8")).hexdigest()


def _json_output(value: Any) -> tuple[Any, str | None]:
    normalized = _plain(value)
    try:
        json.dumps(normalized, allow_nan=False)
    except (TypeError, ValueError) as error:
        return None, f"$: provider output is not JSON-compatible: {error}"
    return normalized, None


def _plain_mapping(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentInvocationContractError(f"{field} must be a mapping")
    normalized = _plain(value)
    if not isinstance(normalized, dict):
        raise AgentInvocationContractError(f"{field} must be a mapping")
    return normalized


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return deepcopy(value)


def _json_path(parts: Any) -> str:
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def _emit(
    logger: StructuredLifecycleLogger | None,
    event_name: str,
    runtime_object: Mapping[str, Any],
    timestamp: str,
) -> None:
    if logger is not None:
        logger.emit(
            event_name=event_name,
            service="workflow-runtime",
            runtime_object=runtime_object,
            emitted_at=timestamp,
        )


def _validate_runtime(value: Mapping[str, Any], kind: str) -> None:
    errors = sorted(
        _runtime_validator(kind).iter_errors(dict(value)),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        error = errors[0]
        raise AgentInvocationContractError(
            f"invalid {kind} at {_json_path(error.absolute_path)}: {error.message}"
        )


@cache
def _runtime_validator(kind: str) -> Draft202012Validator:
    schema_root = Path(__file__).parents[2] / "schemas"
    paths = (
        schema_root / "resources" / "v1" / "resource-definitions.schema.json",
        schema_root / "runtime" / "v1" / "runtime-definitions.schema.json",
        schema_root / "runtime" / "v1" / f"{kind.lower()}.schema.json",
    )
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    registry = Registry().with_resources(
        (
            schema["$id"],
            SchemaResource.from_contents(schema, default_specification=DRAFT202012),
        )
        for schema in schemas
    )
    return Draft202012Validator(schemas[-1], registry=registry)
