"""Resolve versioned Agent resources into immutable invocation inputs."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.observability import CorrelationContext, bind_correlation
from aep.resource_loader import Resource, ResourceCollection, ResourceRef, format_ref


SEMVER_PATTERN = re.compile(
    r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
RFC3339_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:(?P<second>\d{2})"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class AgentResolutionError(ValueError):
    """Base class for structured Agent resolution failures."""

    code = "agent_resolution_error"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = deepcopy(dict(details or {}))

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": str(self),
            "details": deepcopy(self.details),
        }


class InvalidAgentReferenceError(AgentResolutionError):
    """Raised when a supplied or declared reference is not explicit and typed."""

    code = "invalid_agent_reference"


class MissingAgentResourceError(AgentResolutionError):
    """Raised when an explicit Agent dependency cannot be loaded."""

    code = "missing_agent_resource"


class AgentToolDeniedError(AgentResolutionError):
    """Raised when an unconditional policy rule denies a configured Tool."""

    code = "agent_tool_denied"


@dataclass(frozen=True)
class ResolvedAgent(Mapping[str, object]):
    """Deeply immutable ResolvedAgent runtime object.

    ``as_dict`` returns a detached JSON-serializable representation for schema
    validation or persistence at a later runtime boundary.
    """

    _data: Mapping[str, object]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ResolvedAgent":
        return cls(_freeze(dict(value)))

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def as_dict(self) -> dict[str, Any]:
        return _thaw(self._data)


def resolve_agent(
    task_ref: ResourceRef,
    agent_ref: ResourceRef,
    resources: ResourceCollection,
    *,
    correlation: CorrelationContext | Mapping[str, Any],
    resolved_at: str,
) -> ResolvedAgent:
    """Bind one Task's Agent to explicit invocation-time Resource versions."""

    if not isinstance(resources, ResourceCollection):
        raise TypeError("resources must be a ResourceCollection")
    context = bind_correlation(correlation)
    if context.task_execution_id is None:
        raise InvalidAgentReferenceError("correlation requires taskExecutionId")
    task_execution_id = context.task_execution_id
    _require_text("resolved_at", resolved_at)

    task_ref = _validate_ref(task_ref, expected_kind="Task", field="task_ref")
    agent_ref = _validate_ref(agent_ref, expected_kind="Agent", field="agent_ref")
    task = _resolve(resources, task_ref, field="task_ref")
    agent = _resolve(resources, agent_ref, field="agent_ref")

    task_spec = _spec(task)
    assigned_agent_ref = _declared_ref(
        task_spec.get("agentRef"), expected_kind="Agent", field="Task.spec.agentRef"
    )
    if assigned_agent_ref != agent_ref:
        raise InvalidAgentReferenceError(
            f"{format_ref(task_ref)} assigns {format_ref(assigned_agent_ref)}, not "
            f"{format_ref(agent_ref)}",
            details={
                "taskRef": _ref_record(task_ref),
                "assignedAgentRef": _ref_record(assigned_agent_ref),
                "agentRef": _ref_record(agent_ref),
            },
        )

    agent_spec = _spec(agent)
    prompt_ref = _declared_ref(
        agent_spec.get("promptRef"), expected_kind="Prompt", field="Agent.spec.promptRef"
    )
    model_ref = _declared_ref(
        agent_spec.get("modelRef"), expected_kind="Model", field="Agent.spec.modelRef"
    )
    _resolve(resources, prompt_ref, field="Agent.spec.promptRef")
    model = _resolve(resources, model_ref, field="Agent.spec.modelRef")

    tool_refs = _declared_refs(
        agent_spec.get("toolRefs", ()), expected_kind="Tool", field="Agent.spec.toolRefs"
    )
    tools = tuple(
        _resolve(resources, ref, field=f"Agent.spec.toolRefs[{index}]")
        for index, ref in enumerate(tool_refs)
    )
    for tool in tools:
        tool_spec = _spec(tool)
        if "provider" in tool_spec:
            raise InvalidAgentReferenceError(
                f"{format_ref(tool.ref)} configures a model provider and cannot be listed as a Tool",
                details={"toolRef": _ref_record(tool.ref), "reason": "model_provider"},
            )

    task_policy_refs = _declared_refs(
        task_spec.get("policies", ()), expected_kind="Policy", field="Task.spec.policies"
    )
    agent_policy_refs = _declared_refs(
        agent_spec.get("policyRefs", ()),
        expected_kind="Policy",
        field="Agent.spec.policyRefs",
    )
    policy_refs = _deduplicate((*task_policy_refs, *agent_policy_refs))
    policies = tuple(
        _resolve(resources, ref, field=f"policyRefs[{index}]")
        for index, ref in enumerate(policy_refs)
    )
    _reject_unconditionally_denied_tools(tools, policies)

    model_parameters = _mapping_or_empty(_spec(model).get("parameters"), "Model.spec.parameters")
    output_schema_value = agent_spec.get("outputSchema", task_spec.get("outputs"))
    if not isinstance(output_schema_value, Mapping) or not output_schema_value:
        raise InvalidAgentReferenceError(
            "Agent or Task must declare a non-empty output schema",
            details={"field": "outputSchema", "reason": "required"},
        )

    resource_refs = (task_ref, agent_ref, prompt_ref, model_ref, *tool_refs, *policy_refs)
    resolved_agent_id = _resolved_agent_id(task_execution_id, resource_refs)
    record = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "ResolvedAgent",
        "id": resolved_agent_id,
        "traceId": context.trace_id,
        "createdAt": resolved_at,
        "updatedAt": resolved_at,
        "provenance": {
            "actor": "agent-resolver",
            "workflowExecutionId": context.workflow_execution_id,
            "taskExecutionId": task_execution_id,
            "resourceRefs": [_ref_record(ref) for ref in resource_refs],
        },
        "taskExecutionId": task_execution_id,
        "agentRef": _ref_record(agent_ref),
        "promptRef": _ref_record(prompt_ref),
        "modelRef": _ref_record(model_ref),
        "toolRefs": [_ref_record(ref) for ref in tool_refs],
        "policyRefs": [_ref_record(ref) for ref in policy_refs],
        "modelParameters": deepcopy(dict(model_parameters)),
        "outputSchema": deepcopy(dict(output_schema_value)),
        "resolvedAt": resolved_at,
    }
    _validate_runtime_record(record)
    return ResolvedAgent.from_mapping(record)


def _validate_ref(ref: ResourceRef, *, expected_kind: str, field: str) -> ResourceRef:
    if not isinstance(ref, ResourceRef):
        raise InvalidAgentReferenceError(
            f"{field} must be a ResourceRef",
            details={"field": field, "reason": "invalid_type"},
        )
    if ref.kind != expected_kind:
        raise InvalidAgentReferenceError(
            f"{field} must reference kind {expected_kind}, got {ref.kind}",
            details={"field": field, "expectedKind": expected_kind, "actualKind": ref.kind},
        )
    if not ref.name or not isinstance(ref.version, str) or not SEMVER_PATTERN.fullmatch(ref.version):
        reason = "floating_version" if ref.version == "latest" else "invalid_reference"
        raise InvalidAgentReferenceError(
            f"{field} must reference an explicit immutable {expected_kind} version",
            details={"field": field, "reason": reason, "resourceRef": _ref_record(ref)},
        )
    return ref


def _declared_ref(value: Any, *, expected_kind: str, field: str) -> ResourceRef:
    if not isinstance(value, Mapping):
        raise InvalidAgentReferenceError(
            f"{field} must be an explicit Resource reference",
            details={"field": field, "reason": "required"},
        )
    try:
        ref = ResourceRef.from_mapping(dict(value))
    except (KeyError, TypeError, ValueError):
        raise InvalidAgentReferenceError(
            f"{field} must be an explicit Resource reference",
            details={"field": field, "reason": "invalid_reference"},
        ) from None
    return _validate_ref(ref, expected_kind=expected_kind, field=field)


def _declared_refs(value: Any, *, expected_kind: str, field: str) -> tuple[ResourceRef, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise InvalidAgentReferenceError(
            f"{field} must be an array of Resource references",
            details={"field": field, "reason": "invalid_type"},
        )
    refs = tuple(
        _declared_ref(item, expected_kind=expected_kind, field=f"{field}[{index}]")
        for index, item in enumerate(value)
    )
    if len(set(refs)) != len(refs):
        raise InvalidAgentReferenceError(
            f"{field} must not contain duplicate references",
            details={"field": field, "reason": "duplicate_reference"},
        )
    return refs


def _resolve(resources: ResourceCollection, ref: ResourceRef, *, field: str) -> Resource:
    resource = resources.get(ref)
    if resource is None:
        raise MissingAgentResourceError(
            f"{field} cannot resolve {format_ref(ref)}",
            details={"field": field, "resourceRef": _ref_record(ref)},
        )
    if resource.kind != ref.kind or resource.data.get("kind") != ref.kind:
        raise InvalidAgentReferenceError(
            f"{field} resolved {format_ref(ref)} with the wrong Resource kind",
            details={"field": field, "expectedKind": ref.kind},
        )
    return resource


def _spec(resource: Resource) -> Mapping[str, Any]:
    spec = resource.data.get("spec")
    if not isinstance(spec, Mapping):
        raise InvalidAgentReferenceError(
            f"{format_ref(resource.ref)} must declare a spec object",
            details={"resourceRef": _ref_record(resource.ref), "field": "spec"},
        )
    return spec


def _reject_unconditionally_denied_tools(
    tools: Sequence[Resource], policies: Sequence[Resource]
) -> None:
    for tool in tools:
        capabilities = _spec(tool).get("capabilities")
        if not isinstance(capabilities, Sequence) or isinstance(capabilities, (str, bytes)):
            raise InvalidAgentReferenceError(
                f"{format_ref(tool.ref)} must declare a capability allowlist",
                details={"toolRef": _ref_record(tool.ref), "field": "spec.capabilities"},
            )
        for policy in policies:
            policy_spec = _spec(policy)
            if policy_spec.get("type") != "pre-execution-capability":
                continue
            rules = policy_spec.get("rules")
            if not isinstance(rules, Sequence):
                continue
            for index, rule in enumerate(rules):
                if not isinstance(rule, Mapping) or rule.get("effect") != "deny":
                    continue
                if "conditions" in rule:
                    continue
                denied = rule.get("capabilities")
                if not isinstance(denied, Sequence) or isinstance(denied, (str, bytes)):
                    continue
                overlap = tuple(capability for capability in capabilities if capability in denied)
                if overlap:
                    raise AgentToolDeniedError(
                        f"{format_ref(policy.ref)} denies {format_ref(tool.ref)} capabilities: "
                        + ", ".join(overlap),
                        details={
                            "toolRef": _ref_record(tool.ref),
                            "policyRef": _ref_record(policy.ref),
                            "ruleIndex": index,
                            "capabilities": list(overlap),
                        },
                    )


def _mapping_or_empty(value: Any, field: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidAgentReferenceError(
            f"{field} must be an object",
            details={"field": field, "reason": "invalid_type"},
        )
    return value


def _deduplicate(refs: Sequence[ResourceRef]) -> tuple[ResourceRef, ...]:
    return tuple(dict.fromkeys(refs))


def _resolved_agent_id(task_execution_id: str, refs: Sequence[ResourceRef]) -> str:
    identity = {
        "taskExecutionId": task_execution_id,
        "resourceRefs": [_ref_record(ref) for ref in refs],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return "resolvedagent-" + sha256(canonical.encode("utf-8")).hexdigest()[:20]


def _ref_record(ref: ResourceRef) -> dict[str, str]:
    return {"kind": ref.kind, "name": ref.name, "version": ref.version}


def _require_text(field: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise InvalidAgentReferenceError(
            f"{field} must be a non-empty string",
            details={"field": field, "reason": "required"},
        )


def _validate_runtime_record(record: Mapping[str, Any]) -> None:
    errors = sorted(
        _runtime_validator().iter_errors(dict(record)),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        error = errors[0]
        path = "$" + "".join(f".{part}" for part in error.absolute_path)
        raise InvalidAgentReferenceError(
            f"invalid ResolvedAgent at {path}: {error.message}",
            details={
                "field": path,
                "reason": "runtime_contract",
                "validationMessage": error.message,
            },
        )


@cache
def _runtime_validator() -> Draft202012Validator:
    schema_root = Path(__file__).parents[2] / "schemas"
    schema_paths = (
        schema_root / "resources" / "v1" / "resource-definitions.schema.json",
        schema_root / "runtime" / "v1" / "runtime-definitions.schema.json",
        schema_root / "runtime" / "v1" / "resolvedagent.schema.json",
    )
    schemas = [
        json.loads(schema_path.read_text(encoding="utf-8"))
        for schema_path in schema_paths
    ]
    registry = Registry().with_resources(
        (
            schema["$id"],
            SchemaResource.from_contents(
                schema, default_specification=DRAFT202012
            ),
        )
        for schema in schemas
    )
    format_checker = FormatChecker()
    format_checker.checkers = dict(format_checker.checkers)
    format_checker.checkers["date-time"] = (_is_rfc3339_timestamp, ())
    return Draft202012Validator(
        schemas[-1],
        registry=registry,
        format_checker=format_checker,
    )


def _is_rfc3339_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return True
    match = RFC3339_TIMESTAMP.fullmatch(value)
    if match is None:
        return False
    parseable = value
    if match.group("second") == "60":
        start, end = match.span("second")
        parseable = f"{value[:start]}59{value[end:]}"
    try:
        parsed = datetime.fromisoformat(parseable.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.utcoffset() is not None


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(child) for child in value)
    return deepcopy(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return deepcopy(value)
