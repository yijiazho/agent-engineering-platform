from copy import deepcopy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.capability_policy import (
    ApplicablePolicy,
    CapabilityPolicyContractError,
    CapabilityPolicyIdentityConflictError,
    PolicyDecision,
    PolicyScope,
    PreExecutionCapabilityPolicy,
)
from aep.runtime_store import InMemoryRuntimeObjectStore
from aep.tool_runtime import ToolCaller, ToolRequest


TASK_EXECUTION_ID = "taskexecution-123456789abc"
ROOT = Path(__file__).parents[1]


def policy(
    name: str,
    effect: str,
    capabilities: list[str],
    *,
    conditions: dict | None = None,
    reason: str | None = None,
    version: str = "1.0.0",
) -> dict:
    rule: dict[str, object] = {"effect": effect, "capabilities": capabilities}
    if conditions is not None:
        rule["conditions"] = conditions
    if reason is not None:
        rule["reason"] = reason
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "Policy",
        "metadata": {"name": name, "version": version},
        "spec": {"type": "pre-execution-capability", "rules": [rule]},
    }


def evaluate(
    policies: list[ApplicablePolicy],
    capability: str,
    *,
    store: InMemoryRuntimeObjectStore | None = None,
    actor: str = "agent:patch-writer",
    resource_scope: dict | None = None,
    execution_context: dict | None = None,
    decision_id: str | None = None,
):
    evaluator = PreExecutionCapabilityPolicy(store or InMemoryRuntimeObjectStore())
    return evaluator.evaluate(
        decision_id=decision_id
        or f"policydecision-{capability.replace('.', '')}123456789abc",
        task_execution_id=TASK_EXECUTION_ID,
        capability=capability,
        actor=actor,
        resource_scope=resource_scope or {
            "workspaceRef": {"kind": "Workspace", "name": "main", "version": "1.0.0"}
        },
        execution_context=execution_context or {"repository": "acme/widgets"},
        applicable_policies=policies,
        trace_id="trace-policy-123",
        timestamp="2026-07-30T12:00:00Z",
    )


@pytest.mark.parametrize(
    "capability",
    ["filesystem.write", "docker.run", "git.push", "github.create_pr"],
)
def test_mvp_capabilities_are_evaluated_and_persisted(capability: str) -> None:
    store = InMemoryRuntimeObjectStore()
    result = evaluate(
        [
            ApplicablePolicy(
                PolicyScope.PLATFORM,
                policy("mvp-capabilities", "allow", [capability], reason="MVP grant"),
            )
        ],
        capability,
        store=store,
    )

    assert result["decision"] == "ALLOW"
    assert result["reason"] == "MVP grant"
    assert result["evaluatedRule"]["scope"] == "Platform"
    assert result["evaluatedRule"]["ruleIndex"] == 0
    assert store.get(result["id"]) == result


def test_all_policy_scopes_compose_with_most_restrictive_rule_winning() -> None:
    effects = {
        PolicyScope.PLATFORM: "allow",
        PolicyScope.WORKSPACE: "allow",
        PolicyScope.WORKFLOW: "require-approval",
        PolicyScope.TASK: "allow",
        PolicyScope.AGENT: "deny",
        PolicyScope.TOOL: "allow",
    }
    policies = [
        ApplicablePolicy(scope, policy(scope.value.lower(), effect, ["git.push"]))
        for scope, effect in reversed(tuple(effects.items()))
    ]

    result = evaluate(policies, "git.push")

    assert result["decision"] == "DENY"
    assert result["approvalRequired"] is False
    assert result["evaluatedRule"]["scope"] == "Agent"
    assert [rule["scope"] for rule in result["matchedRules"]] == [
        "Platform",
        "Workspace",
        "Workflow",
        "Task",
        "Agent",
        "Tool",
    ]


def test_require_approval_wins_over_allow() -> None:
    result = evaluate(
        [
            ApplicablePolicy(
                PolicyScope.PLATFORM, policy("default", "allow", ["docker.run"])
            ),
            ApplicablePolicy(
                PolicyScope.TASK,
                policy(
                    "sensitive-validation",
                    "require-approval",
                    ["docker.run"],
                    reason="Human approval is required for this task.",
                ),
            ),
        ],
        "docker.run",
    )

    assert result["decision"] == "REQUIRE_APPROVAL"
    assert result["approvalRequired"] is True
    assert result["reason"] == "Human approval is required for this task."


def test_conditions_match_the_policy_input_and_non_matching_rules_are_ignored() -> None:
    result = evaluate(
        [
            ApplicablePolicy(
                PolicyScope.WORKSPACE,
                policy(
                    "repository-write",
                    "allow",
                    ["filesystem.write"],
                    conditions={
                        "properties": {
                            "executionContext": {
                                "properties": {"repository": {"const": "acme/widgets"}},
                                "required": ["repository"],
                            }
                        },
                        "required": ["executionContext"],
                    },
                ),
            ),
            ApplicablePolicy(
                PolicyScope.TOOL,
                policy(
                    "other-repository",
                    "deny",
                    ["filesystem.write"],
                    conditions={
                        "properties": {
                            "executionContext": {
                                "properties": {"repository": {"const": "other/repo"}}
                            }
                        }
                    },
                ),
            ),
        ],
        "filesystem.write",
    )

    assert result["decision"] == "ALLOW"
    assert len(result["matchedRules"]) == 1


def test_no_matching_rule_denies_by_default_and_records_all_evaluated_policies() -> None:
    result = evaluate(
        [
            ApplicablePolicy(
                PolicyScope.PLATFORM,
                policy("read-only", "allow", ["filesystem.read"]),
            )
        ],
        "filesystem.write",
    )

    assert result["decision"] == "DENY"
    assert result["evaluatedRule"] is None
    assert result["matchedRules"] == []
    assert result["reason"] == "No applicable rule authorizes capability filesystem.write."
    assert result["policyRefs"] == [
        {"kind": "Policy", "name": "read-only", "version": "1.0.0"}
    ]


def test_tool_authorization_boundary_persists_each_decision_and_blocks_approval() -> None:
    store = InMemoryRuntimeObjectStore()
    evaluator = PreExecutionCapabilityPolicy(store)
    boundary = evaluator.tool_authorization_boundary(
        task_execution_id=TASK_EXECUTION_ID,
        resource_scope={"toolRef": {"kind": "Tool", "name": "git", "version": "1.0.0"}},
        execution_context={"repository": "acme/widgets"},
        applicable_policies=[
            ApplicablePolicy(
                PolicyScope.PLATFORM,
                policy("git", "allow", ["filesystem.write"]),
            ),
            ApplicablePolicy(
                PolicyScope.TOOL,
                policy("push-approval", "require-approval", ["git.push"]),
            ),
        ],
        timestamp="2026-07-30T12:00:00Z",
    )
    request = ToolRequest(
        tool_ref={"kind": "Tool", "name": "git", "version": "1.0.0"},
        input={"path": "."},
        caller=ToolCaller(
            kind="AgentInvocation",
            id="agentinvocation-123456789abc",
        ),
        capabilities=("filesystem.write", "git.push"),
        timeout_ms=1000,
        trace_id="trace-policy-123",
    )

    assert boundary(request) is False
    decisions = store.list_by_task_execution(TASK_EXECUTION_ID)
    assert [value["decision"] for value in decisions] == [
        "ALLOW",
        "REQUIRE_APPROVAL",
    ]


def test_decision_id_reuse_cannot_return_a_stale_grant() -> None:
    store = InMemoryRuntimeObjectStore()
    decision_id = "policydecision-reuse123456789abc"
    allowed = evaluate(
        [
            ApplicablePolicy(
                PolicyScope.PLATFORM,
                policy("write-policy", "allow", ["filesystem.write"]),
            )
        ],
        "filesystem.write",
        store=store,
        decision_id=decision_id,
    )

    with pytest.raises(
        CapabilityPolicyIdentityConflictError, match="different authorization inputs"
    ):
        evaluate(
            [
                ApplicablePolicy(
                    PolicyScope.PLATFORM,
                    policy("write-policy", "deny", ["filesystem.write"]),
                )
            ],
            "filesystem.write",
            store=store,
            decision_id=decision_id,
        )

    assert allowed["decision"] == "ALLOW"
    assert store.get(decision_id)["decision"] == "ALLOW"


def test_identical_decision_retry_is_idempotent() -> None:
    store = InMemoryRuntimeObjectStore()
    binding = ApplicablePolicy(
        PolicyScope.PLATFORM,
        policy("write-policy", "allow", ["filesystem.write"]),
    )

    first = evaluate([binding], "filesystem.write", store=store)
    second = evaluate([binding], "filesystem.write", store=store)

    assert second == first
    assert len(store.list_by_task_execution(TASK_EXECUTION_ID)) == 1


@pytest.mark.parametrize(
    "mutation, message",
    [
        (
            lambda value: value.update(apiVersion="aep.dev/v2"),
            "apiVersion",
        ),
        (lambda value: value["metadata"].update(version="latest"), "immutable semantic version"),
        (lambda value: value.update(kind="Tool"), "kind must be Policy"),
        (
            lambda value: value["spec"].update(type="publication"),
            "pre-execution-capability",
        ),
        (
            lambda value: value["spec"]["rules"][0].pop("capabilities"),
            "capabilities",
        ),
    ],
)
def test_malformed_policy_contracts_are_rejected(mutation, message: str) -> None:
    value = policy("invalid", "allow", ["filesystem.write"])
    mutation(value)

    with pytest.raises(CapabilityPolicyContractError, match=message):
        evaluate([ApplicablePolicy(PolicyScope.PLATFORM, value)], "filesystem.write")


def test_policy_inputs_and_persisted_evidence_do_not_alias_callers() -> None:
    value = policy("immutable-input", "allow", ["github.create_pr"])
    binding = ApplicablePolicy(PolicyScope.TOOL, value)
    store = InMemoryRuntimeObjectStore()
    result = evaluate([binding], "github.create_pr", store=store)

    value["spec"]["rules"][0]["effect"] = "deny"
    persisted = store.get(result["id"])

    assert persisted["decision"] == "ALLOW"
    result["matchedRules"][0]["effect"] = "deny"
    assert store.get(result["id"])["matchedRules"][0]["effect"] == "allow"


def test_pre_execution_policy_schema_accepts_complete_fixture() -> None:
    fixture = _load_json(
        ROOT / "fixtures/resources/valid/pre-execution-capability-policy.json"
    )

    assert list(_policy_validator().iter_errors(fixture)) == []


@pytest.mark.parametrize(
    "fixture_name",
    [
        "pre-execution-policy-missing-capabilities.json",
        "pre-execution-policy-empty-capabilities.json",
    ],
)
def test_pre_execution_policy_schema_rejects_missing_or_empty_capabilities(
    fixture_name: str,
) -> None:
    fixture = _load_json(ROOT / "fixtures/resources/invalid" / fixture_name)

    assert list(_policy_validator().iter_errors(fixture))


def test_publication_policy_schema_does_not_require_capabilities() -> None:
    fixture = policy("publication", "allow", ["placeholder"])
    fixture["spec"]["type"] = "publication"
    fixture["spec"]["rules"][0].pop("capabilities")

    assert list(_policy_validator().iter_errors(fixture)) == []


@pytest.mark.parametrize(
    "field",
    [
        "approvalRequired",
        "evaluatedAt",
        "subject",
        "resourceScope",
        "evaluatedRule",
        "matchedRules",
    ],
)
def test_pre_execution_decision_schema_requires_complete_evidence(field: str) -> None:
    fixture = _load_json(
        ROOT / "fixtures/runtime/valid/pre-execution-policydecision.json"
    )
    fixture.pop(field)

    assert list(_policy_decision_validator().iter_errors(fixture))


def test_pre_execution_decision_schema_accepts_complete_fixture() -> None:
    fixture = _load_json(
        ROOT / "fixtures/runtime/valid/pre-execution-policydecision.json"
    )

    assert list(_policy_decision_validator().iter_errors(fixture)) == []


def test_pre_execution_decision_schema_rejects_incomplete_fixture() -> None:
    fixture = _load_json(
        ROOT
        / "fixtures/runtime/invalid/pre-execution-policydecision-incomplete.json"
    )

    assert list(_policy_decision_validator().iter_errors(fixture))


def test_publication_decision_schema_does_not_require_pre_execution_evidence() -> None:
    fixture = _load_json(
        ROOT / "fixtures/runtime/valid/require-approval-policydecision.json"
    )

    assert list(_policy_decision_validator().iter_errors(fixture)) == []


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _policy_validator() -> Draft202012Validator:
    schema_root = ROOT / "schemas/resources/v1"
    schemas = [_load_json(path) for path in schema_root.glob("*.schema.json")]
    registry = _registry(schemas)
    schema = next(
        item for item in schemas if item["$id"].endswith("/policy.schema.json")
    )
    return Draft202012Validator(schema, registry=registry)


def _policy_decision_validator() -> Draft202012Validator:
    paths = (
        ROOT / "schemas/resources/v1/resource-definitions.schema.json",
        ROOT / "schemas/runtime/v1/runtime-definitions.schema.json",
        ROOT / "schemas/runtime/v1/policydecision.schema.json",
    )
    schemas = [_load_json(path) for path in paths]
    return Draft202012Validator(schemas[-1], registry=_registry(schemas))


def _registry(schemas: list[dict]) -> Registry:
    registry = Registry()
    for schema in schemas:
        registry = registry.with_resource(
            schema["$id"],
            SchemaResource.from_contents(
                schema, default_specification=DRAFT202012
            ),
        )
    return registry
