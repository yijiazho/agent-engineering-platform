"""Deterministic pre-execution authorization for privileged capabilities."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

from aep.observability import CorrelationContext, bind_correlation
from aep.runtime_store import (
    RuntimeObject,
    RuntimeObjectAlreadyExistsError,
    RuntimeObjectStore,
)
from aep.tool_runtime import ToolRequest


SEMVER_PATTERN = re.compile(
    r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class CapabilityPolicyContractError(ValueError):
    """Raised when policy input cannot produce trustworthy authorization evidence."""


class CapabilityPolicyIdentityConflictError(CapabilityPolicyContractError):
    """Raised when a decision id is reused for different authorization inputs."""


class PolicyScope(str, Enum):
    PLATFORM = "Platform"
    WORKSPACE = "Workspace"
    WORKFLOW = "Workflow"
    TASK = "Task"
    AGENT = "Agent"
    TOOL = "Tool"


class PolicyDecision(str, Enum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


_SCOPE_ORDER = {scope: index for index, scope in enumerate(PolicyScope)}
_DECISION_PRIORITY = {
    PolicyDecision.ALLOW: 0,
    PolicyDecision.REQUIRE_APPROVAL: 1,
    PolicyDecision.DENY: 2,
}
_EFFECT_DECISION = {
    "allow": PolicyDecision.ALLOW,
    "require-approval": PolicyDecision.REQUIRE_APPROVAL,
    "deny": PolicyDecision.DENY,
}


@dataclass(frozen=True)
class ApplicablePolicy:
    """A versioned Policy resource attached at one authorization scope."""

    scope: PolicyScope
    resource: Mapping[str, Any]

    def __post_init__(self) -> None:
        try:
            normalized_scope = PolicyScope(self.scope)
        except (TypeError, ValueError) as error:
            raise CapabilityPolicyContractError(
                f"unsupported policy scope {self.scope!r}"
            ) from error
        if not isinstance(self.resource, Mapping):
            raise CapabilityPolicyContractError("policy resource must be a mapping")
        object.__setattr__(self, "scope", normalized_scope)
        object.__setattr__(self, "resource", deepcopy(dict(self.resource)))


class PreExecutionCapabilityPolicy:
    """Evaluate and persist fail-closed capability decisions."""

    def __init__(self, store: RuntimeObjectStore) -> None:
        self._store = store

    def evaluate(
        self,
        *,
        decision_id: str,
        task_execution_id: str,
        capability: str,
        actor: str,
        resource_scope: Mapping[str, Any],
        execution_context: Mapping[str, Any],
        applicable_policies: Sequence[ApplicablePolicy],
        correlation: CorrelationContext | Mapping[str, Any],
        timestamp: str,
    ) -> RuntimeObject:
        """Compose all matching rules and persist the most restrictive decision."""

        _require_text("decision_id", decision_id)
        _require_text("task_execution_id", task_execution_id)
        _require_text("capability", capability)
        _require_text("actor", actor)
        context = bind_correlation(
            correlation, task_execution_id=task_execution_id
        )
        _require_text("timestamp", timestamp)
        if not isinstance(resource_scope, Mapping):
            raise CapabilityPolicyContractError("resource_scope must be a mapping")
        if not isinstance(execution_context, Mapping):
            raise CapabilityPolicyContractError("execution_context must be a mapping")

        policy_input = {
            "capability": capability,
            "actor": actor,
            "resourceScope": deepcopy(dict(resource_scope)),
            "executionContext": deepcopy(dict(execution_context)),
        }
        policies = sorted(
            (_validated_policy(binding) for binding in applicable_policies),
            key=lambda item: (
                _SCOPE_ORDER[item.scope],
                item.resource["metadata"]["name"],
                item.resource["metadata"]["version"],
            ),
        )
        policy_refs = _unique_refs(_policy_ref(binding.resource) for binding in policies)
        matched_rules: list[dict[str, Any]] = []
        for binding in policies:
            policy_ref = _policy_ref(binding.resource)
            for rule_index, rule in enumerate(binding.resource["spec"]["rules"]):
                if capability not in rule["capabilities"]:
                    continue
                conditions = rule.get("conditions")
                if conditions is not None and not _conditions_match(
                    conditions, policy_input, policy_ref, rule_index
                ):
                    continue
                matched_rules.append(
                    {
                        "scope": binding.scope.value,
                        "policyRef": policy_ref,
                        "ruleIndex": rule_index,
                        "effect": rule["effect"],
                    }
                )

        winning_rule: dict[str, Any] | None
        if matched_rules:
            winning_rule = max(
                matched_rules,
                key=lambda rule: _DECISION_PRIORITY[_EFFECT_DECISION[rule["effect"]]],
            )
            decision = _EFFECT_DECISION[winning_rule["effect"]]
            winning_policy = next(
                binding
                for binding in policies
                if _policy_ref(binding.resource) == winning_rule["policyRef"]
            )
            source_rule = winning_policy.resource["spec"]["rules"][
                winning_rule["ruleIndex"]
            ]
            reason = source_rule.get("reason") or _default_reason(
                decision, capability, winning_rule
            )
        else:
            winning_rule = None
            decision = PolicyDecision.DENY
            reason = f"No applicable rule authorizes capability {capability}."

        provenance = {
            "actor": "policy-engine",
            "caller": actor,
            "workflowExecutionId": context.workflow_execution_id,
            "taskExecutionId": task_execution_id,
            "resourceRefs": policy_refs,
        }
        repository_revision = resource_scope.get("repositoryRevision")
        if isinstance(repository_revision, str) and repository_revision:
            provenance["repositoryRevision"] = repository_revision
        record = {
            "apiVersion": "aep.dev/v1alpha1",
            "kind": "PolicyDecision",
            "id": decision_id,
            "traceId": context.trace_id,
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "provenance": provenance,
            "taskExecutionId": task_execution_id,
            "gate": "PRE_EXECUTION_CAPABILITY",
            "policyRefs": policy_refs,
            "action": capability,
            "decision": decision.value,
            "reason": reason,
            "approvalRequired": decision is PolicyDecision.REQUIRE_APPROVAL,
            "evaluatedAt": timestamp,
            "subject": actor,
            "resourceScope": deepcopy(dict(resource_scope)),
            "evaluatedRule": deepcopy(winning_rule),
            "matchedRules": matched_rules,
        }
        deterministic_key = _decision_key(
            decision_id=decision_id,
            task_execution_id=task_execution_id,
            trace_id=context.trace_id,
            capability=capability,
            actor=actor,
            resource_scope=resource_scope,
            execution_context=execution_context,
            policies=policies,
        )
        try:
            return self._store.create(record, deterministic_key=deterministic_key)
        except RuntimeObjectAlreadyExistsError as error:
            raise CapabilityPolicyIdentityConflictError(
                f"decision id {decision_id!r} is already bound to different "
                "authorization inputs"
            ) from error

    def tool_authorization_boundary(
        self,
        *,
        task_execution_id: str,
        resource_scope: Mapping[str, Any],
        execution_context: Mapping[str, Any],
        applicable_policies: Sequence[ApplicablePolicy],
        timestamp: str,
    ) -> Callable[[ToolRequest], bool]:
        """Build the mandatory Tool Runtime hook; only ALLOW may execute."""

        scope_copy = deepcopy(dict(resource_scope))
        context_copy = deepcopy(dict(execution_context))
        policies_copy = tuple(
            ApplicablePolicy(binding.scope, binding.resource)
            for binding in applicable_policies
        )

        def authorize(request: ToolRequest) -> bool:
            actor = f"{request.caller.kind}:{request.caller.id}"
            allowed = True
            for capability in request.capabilities:
                digest_input = {
                    "taskExecutionId": task_execution_id,
                    "toolRef": dict(request.tool_ref),
                    "capability": capability,
                    "actor": actor,
                    "traceId": request.trace_id,
                    "resourceScope": scope_copy,
                    "executionContext": context_copy,
                    "policies": [
                        {
                            "scope": binding.scope.value,
                            "resource": binding.resource,
                        }
                        for binding in policies_copy
                    ],
                }
                canonical = json.dumps(
                    digest_input, sort_keys=True, separators=(",", ":")
                )
                decision_id = (
                    "policydecision-"
                    + sha256(canonical.encode("utf-8")).hexdigest()[:20]
                )
                result = self.evaluate(
                    decision_id=decision_id,
                    task_execution_id=task_execution_id,
                    capability=capability,
                    actor=actor,
                    resource_scope=scope_copy,
                    execution_context=context_copy,
                    applicable_policies=policies_copy,
                    correlation=request.correlation,
                    timestamp=timestamp,
                )
                if result["decision"] != PolicyDecision.ALLOW.value:
                    allowed = False
            return allowed

        return authorize


def _validated_policy(binding: ApplicablePolicy) -> ApplicablePolicy:
    resource = binding.resource
    if resource.get("apiVersion") != "aep.dev/v1alpha1":
        raise CapabilityPolicyContractError(
            "policy resource apiVersion must be aep.dev/v1alpha1"
        )
    if resource.get("kind") != "Policy":
        raise CapabilityPolicyContractError("policy resource kind must be Policy")
    metadata = resource.get("metadata")
    if not isinstance(metadata, Mapping):
        raise CapabilityPolicyContractError("policy metadata must be a mapping")
    name = metadata.get("name")
    version = metadata.get("version")
    _require_text("policy metadata.name", name)
    if not isinstance(version, str) or not SEMVER_PATTERN.fullmatch(version):
        raise CapabilityPolicyContractError(
            "policy metadata.version must be an immutable semantic version"
        )
    spec = resource.get("spec")
    if not isinstance(spec, Mapping):
        raise CapabilityPolicyContractError("policy spec must be a mapping")
    if spec.get("type") != "pre-execution-capability":
        raise CapabilityPolicyContractError(
            "applicable policy must have type pre-execution-capability"
        )
    rules = spec.get("rules")
    if not isinstance(rules, list) or not rules:
        raise CapabilityPolicyContractError("policy rules must be a non-empty list")
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping):
            raise CapabilityPolicyContractError(f"policy rule {index} must be a mapping")
        if rule.get("effect") not in _EFFECT_DECISION:
            raise CapabilityPolicyContractError(
                f"policy rule {index} has unsupported effect {rule.get('effect')!r}"
            )
        capabilities = rule.get("capabilities")
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or any(not isinstance(item, str) or not item for item in capabilities)
        ):
            raise CapabilityPolicyContractError(
                f"policy rule {index} capabilities must be a non-empty string list"
            )
        if len(set(capabilities)) != len(capabilities):
            raise CapabilityPolicyContractError(
                f"policy rule {index} capabilities must be unique"
            )
        reason = rule.get("reason")
        if reason is not None and (not isinstance(reason, str) or not reason):
            raise CapabilityPolicyContractError(
                f"policy rule {index} reason must be a non-empty string"
            )
        conditions = rule.get("conditions")
        if conditions is not None and not isinstance(conditions, Mapping):
            raise CapabilityPolicyContractError(
                f"policy rule {index} conditions must be a JSON Schema mapping"
            )
    return ApplicablePolicy(binding.scope, resource)


def _conditions_match(
    conditions: Mapping[str, Any],
    policy_input: Mapping[str, Any],
    policy_ref: Mapping[str, str],
    rule_index: int,
) -> bool:
    try:
        Draft202012Validator.check_schema(conditions)
    except (SchemaError, TypeError) as error:
        message = error.message if isinstance(error, SchemaError) else str(error)
        raise CapabilityPolicyContractError(
            f"invalid conditions in {policy_ref['name']} rule {rule_index}: {message}"
        ) from error
    return Draft202012Validator(conditions).is_valid(policy_input)


def _policy_ref(resource: Mapping[str, Any]) -> dict[str, str]:
    metadata = resource["metadata"]
    return {
        "kind": "Policy",
        "name": metadata["name"],
        "version": metadata["version"],
    }


def _unique_refs(refs: Any) -> list[dict[str, str]]:
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for ref in refs:
        key = (ref["kind"], ref["name"], ref["version"])
        if key not in seen:
            seen.add(key)
            unique.append(ref)
    return unique


def _default_reason(
    decision: PolicyDecision, capability: str, rule: Mapping[str, Any]
) -> str:
    policy_ref = rule["policyRef"]
    return (
        f"{decision.value} for {capability} by {rule['scope']} policy "
        f"{policy_ref['name']}@{policy_ref['version']} rule {rule['ruleIndex']}."
    )


def _decision_key(
    *,
    decision_id: str,
    task_execution_id: str,
    trace_id: str,
    capability: str,
    actor: str,
    resource_scope: Mapping[str, Any],
    execution_context: Mapping[str, Any],
    policies: Sequence[ApplicablePolicy],
) -> str:
    identity = {
        "decisionId": decision_id,
        "taskExecutionId": task_execution_id,
        "traceId": trace_id,
        "capability": capability,
        "actor": actor,
        "resourceScope": deepcopy(dict(resource_scope)),
        "executionContext": deepcopy(dict(execution_context)),
        "policies": [
            {"scope": binding.scope.value, "resource": binding.resource}
            for binding in policies
        ],
    }
    try:
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise CapabilityPolicyContractError(
            f"authorization inputs must be JSON serializable: {error}"
        ) from error
    digest = sha256(canonical.encode("utf-8")).hexdigest()
    return f"pre-execution-policy:{digest}"


def _require_text(field: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise CapabilityPolicyContractError(f"{field} must be a non-empty string")
