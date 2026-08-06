"""Deterministic publication governance over immutable runtime evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from functools import cache
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator, SchemaError
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.capability_policy import ApplicablePolicy, PolicyDecision, PolicyScope
from aep.observability import CorrelationContext, bind_correlation
from aep.runtime_store import (
    RuntimeObject,
    RuntimeObjectAlreadyExistsError,
    RuntimeObjectStore,
)


SEMVER_PATTERN = re.compile(
    r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
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


class PublicationPolicyContractError(ValueError):
    """Raised when publication inputs cannot form trustworthy evidence."""


class PublicationPolicyIdentityConflictError(PublicationPolicyContractError):
    """Raised when a decision id is reused for different publication inputs."""


class PublicationPolicy:
    """Evaluate and persist the final governance gate before publication."""

    def __init__(self, store: RuntimeObjectStore) -> None:
        self._store = store

    def evaluate(
        self,
        *,
        decision_id: str,
        task_execution_id: str,
        candidate_action: Mapping[str, Any],
        required_artifact_ids: Sequence[str],
        artifacts: Sequence[Mapping[str, Any]],
        required_evaluation_ids: Sequence[str],
        evaluation_results: Sequence[Mapping[str, Any]],
        prior_policy_decisions: Sequence[Mapping[str, Any]],
        applicable_policies: Sequence[ApplicablePolicy],
        actor: str,
        resource_scope: Mapping[str, Any],
        correlation: CorrelationContext | Mapping[str, Any],
        timestamp: str,
    ) -> RuntimeObject:
        """Evaluate complete evidence and persist an explainable decision."""

        _require_text("decision_id", decision_id)
        _require_text("task_execution_id", task_execution_id)
        _require_text("actor", actor)
        _require_text("timestamp", timestamp)
        if not isinstance(candidate_action, Mapping):
            raise PublicationPolicyContractError("candidate_action must be a mapping")
        if not isinstance(resource_scope, Mapping):
            raise PublicationPolicyContractError("resource_scope must be a mapping")
        action = candidate_action.get("action")
        target = candidate_action.get("target")
        _require_text("candidate_action.action", action)
        if not isinstance(target, Mapping):
            raise PublicationPolicyContractError("candidate_action.target must be a mapping")
        revision = target.get("repositoryRevision")
        _require_text("candidate_action.target.repositoryRevision", revision)

        context = bind_correlation(correlation, task_execution_id=task_execution_id)
        artifact_ids = _validated_ids("required_artifact_ids", required_artifact_ids)
        evaluation_ids = _validated_ids(
            "required_evaluation_ids", required_evaluation_ids
        )
        artifact_records = _records_by_id("artifacts", artifacts)
        evaluation_records = _records_by_id("evaluation_results", evaluation_results)
        prior_records = _records_by_id(
            "prior_policy_decisions", prior_policy_decisions
        )
        policies = sorted(
            (_validated_publication_policy(binding) for binding in applicable_policies),
            key=lambda item: (
                _SCOPE_ORDER[item.scope],
                item.resource["metadata"]["name"],
                item.resource["metadata"]["version"],
            ),
        )
        policy_refs = _unique_refs(_policy_ref(item.resource) for item in policies)

        evidence_failures = _evidence_failures(
            store=self._store,
            required_artifact_ids=artifact_ids,
            artifacts=artifact_records,
            required_evaluation_ids=evaluation_ids,
            evaluations=evaluation_records,
            prior_decisions=prior_records,
            trace_id=context.trace_id,
            workflow_execution_id=context.workflow_execution_id,
            repository_revision=revision,
        )
        evidence_summary = {
            "patchGenerated": any(
                value.get("artifactType") == "PATCH"
                for value in artifact_records.values()
                if value.get("id") in artifact_ids
            ),
            "validationRan": bool(evaluation_ids),
            "requiredArtifactsPresent": all(
                item in artifact_records for item in artifact_ids
            ),
            "requiredEvaluationsPresent": all(
                item in evaluation_records for item in evaluation_ids
            ),
            "allRequiredEvaluationsPassed": bool(evaluation_ids)
            and all(
                evaluation_records.get(item, {}).get("status") == "SUCCEEDED"
                and evaluation_records.get(item, {}).get("outcome") == "PASS"
                for item in evaluation_ids
            ),
            "noPriorPolicyViolation": not any(
                value.get("decision") == "DENY" for value in prior_records.values()
            ),
        }
        policy_input = {
            "candidateAction": deepcopy(dict(candidate_action)),
            "evidence": evidence_summary,
            "priorPolicyState": [deepcopy(value) for value in prior_records.values()],
            "resourceScope": deepcopy(dict(resource_scope)),
        }

        matched_rules: list[dict[str, Any]] = []
        for binding in policies:
            policy_ref = _policy_ref(binding.resource)
            for rule_index, rule in enumerate(binding.resource["spec"]["rules"]):
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

        winning_rule: dict[str, Any] | None = None
        if evidence_failures:
            decision = PolicyDecision.DENY
            reason = "; ".join(evidence_failures)
        elif any(value.get("decision") == "REQUIRE_APPROVAL" for value in prior_records.values()):
            decision = PolicyDecision.REQUIRE_APPROVAL
            reason = "An earlier policy decision still requires approval."
        elif matched_rules:
            winning_rule = max(
                matched_rules,
                key=lambda rule: _DECISION_PRIORITY[_EFFECT_DECISION[rule["effect"]]],
            )
            decision = _EFFECT_DECISION[winning_rule["effect"]]
            source = next(
                item
                for item in policies
                if _policy_ref(item.resource) == winning_rule["policyRef"]
            ).resource["spec"]["rules"][winning_rule["ruleIndex"]]
            reason = source.get("reason") or _default_reason(decision, action, winning_rule)
        else:
            decision = PolicyDecision.DENY
            reason = f"No applicable publication rule authorizes action {action}."

        record = {
            "apiVersion": "aep.dev/v1alpha1",
            "kind": "PolicyDecision",
            "id": decision_id,
            "traceId": context.trace_id,
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "provenance": {
                "actor": "policy-engine",
                "caller": actor,
                "workflowExecutionId": context.workflow_execution_id,
                "taskExecutionId": task_execution_id,
                "repositoryRevision": revision,
                "resourceRefs": policy_refs,
            },
            "taskExecutionId": task_execution_id,
            "gate": "PUBLICATION",
            "policyRefs": policy_refs,
            "action": action,
            "decision": decision.value,
            "reason": reason,
            "approvalRequired": decision is PolicyDecision.REQUIRE_APPROVAL,
            "evaluatedAt": timestamp,
            "subject": actor,
            "resourceScope": deepcopy(dict(resource_scope)),
            "evaluatedRule": deepcopy(winning_rule),
            "matchedRules": matched_rules,
            "repositoryRevision": revision,
            "publicationTarget": deepcopy(dict(target)),
            "generatedArtifactIds": list(artifact_ids),
            "evaluationResultIds": list(evaluation_ids),
            "priorPolicyDecisionIds": list(prior_records),
            "evidence": evidence_summary | {"failures": evidence_failures},
        }
        _validate_decision(record)
        key = _decision_key(
            decision_id=decision_id,
            task_execution_id=task_execution_id,
            trace_id=context.trace_id,
            candidate_action=candidate_action,
            artifact_ids=artifact_ids,
            artifact_records=artifact_records,
            evaluation_ids=evaluation_ids,
            evaluation_records=evaluation_records,
            prior_records=prior_records,
            resource_scope=resource_scope,
            policies=policies,
            actor=actor,
        )
        try:
            return self._store.create(record, deterministic_key=key)
        except RuntimeObjectAlreadyExistsError as error:
            raise PublicationPolicyIdentityConflictError(
                f"decision id {decision_id!r} is already bound to different publication inputs"
            ) from error


def _evidence_failures(
    *,
    store: RuntimeObjectStore,
    required_artifact_ids: Sequence[str],
    artifacts: Mapping[str, Mapping[str, Any]],
    required_evaluation_ids: Sequence[str],
    evaluations: Mapping[str, Mapping[str, Any]],
    prior_decisions: Mapping[str, Mapping[str, Any]],
    trace_id: str,
    workflow_execution_id: str,
    repository_revision: str,
) -> list[str]:
    failures: list[str] = []
    missing_artifacts = [item for item in required_artifact_ids if item not in artifacts]
    missing_evaluations = [item for item in required_evaluation_ids if item not in evaluations]
    if missing_artifacts:
        failures.append("Required artifacts are missing: " + ", ".join(missing_artifacts) + ".")
    if not required_artifact_ids:
        failures.append("At least one required artifact must be declared.")
    if missing_evaluations:
        failures.append("Required EvaluationResults are missing: " + ", ".join(missing_evaluations) + ".")
    if not required_evaluation_ids:
        failures.append("Validation did not run: no required EvaluationResults were declared.")

    trustworthy_artifact_ids: set[str] = set()
    for artifact_id in required_artifact_ids:
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            continue
        contract_error = _runtime_evidence_error("GeneratedArtifact", artifact)
        if contract_error is not None:
            failures.append(f"GeneratedArtifact {artifact_id} is invalid: {contract_error}.")
            continue
        persisted = store.get(artifact_id)
        if persisted is None:
            failures.append(f"GeneratedArtifact {artifact_id} is not persisted.")
            continue
        if dict(persisted) != dict(artifact):
            failures.append(f"GeneratedArtifact {artifact_id} does not match persisted evidence.")
            continue
        trustworthy_artifact_ids.add(artifact_id)

    required_artifacts = [artifacts[item] for item in required_artifact_ids if item in trustworthy_artifact_ids]
    if not any(item.get("artifactType") == "PATCH" for item in required_artifacts):
        failures.append("Patch generation did not produce a required PATCH artifact.")
    for artifact in required_artifacts:
        if artifact.get("kind") != "GeneratedArtifact":
            failures.append(f"Artifact {artifact['id']} is not a GeneratedArtifact.")
        elif not _same_execution(artifact, trace_id, workflow_execution_id, repository_revision):
            failures.append(f"GeneratedArtifact {artifact['id']} has mismatched provenance.")

    for result_id in required_evaluation_ids:
        result = evaluations.get(result_id)
        if result is None:
            continue
        contract_error = _runtime_evidence_error("EvaluationResult", result)
        if contract_error is not None:
            failures.append(f"EvaluationResult {result_id} is invalid: {contract_error}.")
            continue
        persisted = store.get(result_id)
        if persisted is None:
            failures.append(f"EvaluationResult {result_id} is not persisted.")
            continue
        if dict(persisted) != dict(result):
            failures.append(f"EvaluationResult {result_id} does not match persisted evidence.")
            continue
        if result.get("kind") != "EvaluationResult":
            failures.append(f"Evidence {result_id} is not an EvaluationResult.")
        elif not _same_execution(result, trace_id, workflow_execution_id, repository_revision):
            failures.append(f"EvaluationResult {result_id} has mismatched provenance.")
        elif result.get("status") != "SUCCEEDED":
            failures.append(f"Required EvaluationResult {result_id} did not complete successfully.")
        elif result.get("outcome") != "PASS":
            failures.append(f"Required EvaluationResult {result_id} failed.")

    for value in prior_decisions.values():
        decision_id = value["id"]
        contract_error = _runtime_evidence_error("PolicyDecision", value)
        if contract_error is not None:
            failures.append(f"Prior PolicyDecision {decision_id} is invalid: {contract_error}.")
            continue
        persisted = store.get(decision_id)
        if persisted is None:
            failures.append(f"Prior PolicyDecision {decision_id} is not persisted.")
            continue
        if dict(persisted) != dict(value):
            failures.append(f"Prior PolicyDecision {decision_id} does not match persisted evidence.")
            continue
        if value.get("traceId") != trace_id:
            failures.append(f"Prior PolicyDecision {value['id']} has mismatched trace provenance.")
        if value.get("decision") == "DENY":
            failures.append(f"Prior PolicyDecision {value['id']} denied publication prerequisites.")
    return failures


def _same_execution(value: Mapping[str, Any], trace_id: str, workflow_id: str, revision: str) -> bool:
    provenance = value.get("provenance")
    return (
        value.get("traceId") == trace_id
        and isinstance(provenance, Mapping)
        and provenance.get("workflowExecutionId") == workflow_id
        and (value.get("repositoryRevision", provenance.get("repositoryRevision")) == revision)
    )


def _validated_publication_policy(binding: ApplicablePolicy) -> ApplicablePolicy:
    resource = binding.resource
    if resource.get("apiVersion") != "aep.dev/v1alpha1" or resource.get("kind") != "Policy":
        raise PublicationPolicyContractError("applicable policy must be an aep.dev/v1alpha1 Policy")
    metadata = resource.get("metadata")
    if not isinstance(metadata, Mapping) or not isinstance(metadata.get("name"), str):
        raise PublicationPolicyContractError("policy metadata.name must be a non-empty string")
    version = metadata.get("version")
    if not isinstance(version, str) or not SEMVER_PATTERN.fullmatch(version):
        raise PublicationPolicyContractError("policy metadata.version must be an immutable semantic version")
    spec = resource.get("spec")
    if not isinstance(spec, Mapping) or spec.get("type") != "publication":
        raise PublicationPolicyContractError("applicable policy must have type publication")
    rules = spec.get("rules")
    if not isinstance(rules, list) or not rules:
        raise PublicationPolicyContractError("policy rules must be a non-empty list")
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping) or rule.get("effect") not in _EFFECT_DECISION:
            raise PublicationPolicyContractError(f"policy rule {index} has an unsupported effect")
        if "capabilities" in rule:
            raise PublicationPolicyContractError(f"publication policy rule {index} must not declare capabilities")
        if (reason := rule.get("reason")) is not None and (not isinstance(reason, str) or not reason):
            raise PublicationPolicyContractError(f"policy rule {index} reason must be a non-empty string")
        if (conditions := rule.get("conditions")) is not None and not isinstance(conditions, Mapping):
            raise PublicationPolicyContractError(f"policy rule {index} conditions must be a JSON Schema mapping")
    return ApplicablePolicy(binding.scope, resource)


def _conditions_match(conditions: Mapping[str, Any], value: Mapping[str, Any], policy_ref: Mapping[str, str], rule_index: int) -> bool:
    try:
        Draft202012Validator.check_schema(conditions)
    except (SchemaError, TypeError) as error:
        message = error.message if isinstance(error, SchemaError) else str(error)
        raise PublicationPolicyContractError(
            f"invalid conditions in {policy_ref['name']} rule {rule_index}: {message}"
        ) from error
    return Draft202012Validator(conditions).is_valid(value)


def _records_by_id(name: str, records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    if isinstance(records, (str, bytes)):
        raise PublicationPolicyContractError(f"{name} must be a sequence of mappings")
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise PublicationPolicyContractError(f"{name} must contain mappings")
        value = deepcopy(dict(record))
        record_id = value.get("id")
        _require_text(f"{name} record id", record_id)
        if record_id in result:
            raise PublicationPolicyContractError(f"{name} contains duplicate id {record_id!r}")
        result[record_id] = value
    return result


def _validated_ids(name: str, values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise PublicationPolicyContractError(f"{name} must be a sequence of ids")
    result = tuple(values)
    if any(not isinstance(item, str) or not item for item in result):
        raise PublicationPolicyContractError(f"{name} must contain non-empty string ids")
    if len(set(result)) != len(result):
        raise PublicationPolicyContractError(f"{name} must contain unique ids")
    return result


def _policy_ref(resource: Mapping[str, Any]) -> dict[str, str]:
    return {"kind": "Policy", "name": resource["metadata"]["name"], "version": resource["metadata"]["version"]}


def _unique_refs(refs: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for ref in refs:
        if ref not in result:
            result.append(ref)
    return result


def _default_reason(decision: PolicyDecision, action: str, rule: Mapping[str, Any]) -> str:
    ref = rule["policyRef"]
    return f"{decision.value} for {action} by {rule['scope']} policy {ref['name']}@{ref['version']} rule {rule['ruleIndex']}."


def _decision_key(**inputs: Any) -> str:
    normalized = dict(inputs)
    normalized["policies"] = [
        {"scope": item.scope.value, "resource": item.resource}
        for item in normalized["policies"]
    ]
    try:
        canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise PublicationPolicyContractError(f"publication inputs must be JSON serializable: {error}") from error
    return "publication-policy:" + sha256(canonical.encode()).hexdigest()


def _validate_decision(record: Mapping[str, Any]) -> None:
    errors = sorted(_decision_validator().iter_errors(dict(record)), key=lambda error: (list(error.absolute_path), error.message))
    if errors:
        error = errors[0]
        path = "$" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path)
        raise PublicationPolicyContractError(f"invalid PolicyDecision at {path}: {error.message}")


def _runtime_evidence_error(kind: str, record: Mapping[str, Any]) -> str | None:
    errors = sorted(
        _runtime_evidence_validator(kind).iter_errors(dict(record)),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if not errors:
        return None
    error = errors[0]
    path = "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}"
        for part in error.absolute_path
    )
    return f"{path}: {error.message}"


@cache
def _decision_validator() -> Draft202012Validator:
    root = Path(__file__).parents[2] / "schemas"
    paths = (
        root / "resources" / "v1" / "resource-definitions.schema.json",
        root / "runtime" / "v1" / "runtime-definitions.schema.json",
        root / "runtime" / "v1" / "policydecision.schema.json",
    )
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    registry = Registry()
    for schema in schemas:
        registry = registry.with_resource(schema["$id"], SchemaResource.from_contents(schema, default_specification=DRAFT202012))
    return Draft202012Validator(schemas[-1], registry=registry)


@cache
def _runtime_evidence_validator(kind: str) -> Draft202012Validator:
    filenames = {
        "GeneratedArtifact": "generatedartifact.schema.json",
        "EvaluationResult": "evaluationresult.schema.json",
        "PolicyDecision": "policydecision.schema.json",
    }
    try:
        filename = filenames[kind]
    except KeyError as error:
        raise PublicationPolicyContractError(f"unsupported runtime evidence kind {kind!r}") from error
    root = Path(__file__).parents[2] / "schemas"
    paths = (
        root / "resources" / "v1" / "resource-definitions.schema.json",
        root / "runtime" / "v1" / "runtime-definitions.schema.json",
        root / "runtime" / "v1" / filename,
    )
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    registry = Registry()
    for schema in schemas:
        registry = registry.with_resource(
            schema["$id"],
            SchemaResource.from_contents(schema, default_specification=DRAFT202012),
        )
    return Draft202012Validator(schemas[-1], registry=registry)


def _require_text(field: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise PublicationPolicyContractError(f"{field} must be a non-empty string")
