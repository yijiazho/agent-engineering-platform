"""Deterministic build and test evaluation over Docker invocation evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from functools import cache
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.observability import CorrelationContext, bind_correlation
from aep.runtime_store import RuntimeObject, RuntimeObjectStore
from aep.tool_runtime import SEMVER_PATTERN


class BuildTestEvaluationContractError(ValueError):
    """Raised when evaluator inputs cannot form valid EvaluationResults."""


@dataclass(frozen=True)
class ValidationExpectation:
    """Bind one immutable Evaluation Resource to an ordered Docker command."""

    evaluation_ref: Mapping[str, Any]
    command_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.evaluation_ref, Mapping):
            raise BuildTestEvaluationContractError(
                "evaluation_ref must be a resource reference"
            )
        if not isinstance(self.command_index, int) or isinstance(self.command_index, bool):
            raise BuildTestEvaluationContractError(
                "command_index must be a non-negative integer"
            )
        if self.command_index < 0:
            raise BuildTestEvaluationContractError(
                "command_index must be a non-negative integer"
            )
        object.__setattr__(self, "evaluation_ref", deepcopy(dict(self.evaluation_ref)))


def evaluate_build_and_test(
    *,
    store: RuntimeObjectStore,
    build_result_id: str,
    test_result_id: str,
    task_execution_id: str,
    tool_invocation: Mapping[str, Any],
    docker_tool_ref: Mapping[str, Any],
    build_expectation: ValidationExpectation,
    test_expectation: ValidationExpectation,
    correlation: CorrelationContext | Mapping[str, Any],
    timestamp: str,
    provenance: Mapping[str, Any],
) -> tuple[RuntimeObject, RuntimeObject]:
    """Create and persist separate build and test results from Docker evidence."""

    if not isinstance(tool_invocation, Mapping):
        raise BuildTestEvaluationContractError("tool_invocation must be a mapping")
    if not isinstance(provenance, Mapping):
        raise BuildTestEvaluationContractError("provenance must be a mapping")
    if build_result_id == test_result_id:
        raise BuildTestEvaluationContractError(
            "build_result_id and test_result_id must be different"
        )
    if build_expectation.command_index == test_expectation.command_index:
        raise BuildTestEvaluationContractError(
            "build and test expectations must select different commands"
        )

    context = bind_correlation(
        correlation,
        task_execution_id=task_execution_id,
        provenance=provenance,
    )
    invocation = deepcopy(dict(tool_invocation))
    _validate_invocation_identity(
        invocation, docker_tool_ref, task_execution_id, context.trace_id
    )
    requested_commands, request_error = _requested_commands(invocation)
    command_records, output_error, sequence_error = _command_records(
        invocation, requested_commands
    )
    if sequence_error is None and output_error is None:
        sequence_error = _result_sequence_error(
            invocation, requested_commands, command_records
        )
    shared_configuration_error = request_error or sequence_error

    build = _make_result(
        result_id=build_result_id,
        task_execution_id=task_execution_id,
        evaluation_type="build",
        expectation=build_expectation,
        invocation=invocation,
        requested_commands=requested_commands,
        command_records=command_records,
        shared_configuration_error=shared_configuration_error,
        output_error=output_error,
        trace_id=context.trace_id,
        timestamp=timestamp,
        provenance=provenance,
    )
    test = _make_result(
        result_id=test_result_id,
        task_execution_id=task_execution_id,
        evaluation_type="test",
        expectation=test_expectation,
        invocation=invocation,
        requested_commands=requested_commands,
        command_records=command_records,
        shared_configuration_error=shared_configuration_error,
        output_error=output_error,
        trace_id=context.trace_id,
        timestamp=timestamp,
        provenance=provenance,
    )
    _validate_result(build)
    _validate_result(test)

    # RuntimeObjectStore has no atomic multi-create operation. Constructing and
    # validating both records first prevents all evaluator-detectable partial
    # writes; a backend failure between these two calls remains store-specific.
    build_saved = store.create(
        build,
        deterministic_key=(
            f"build-test-evaluation:{invocation['id']}:build:"
            f"{build_result_id}:{build['evidenceAddress']}"
        ),
    )
    test_saved = store.create(
        test,
        deterministic_key=(
            f"build-test-evaluation:{invocation['id']}:test:"
            f"{test_result_id}:{test['evidenceAddress']}"
        ),
    )
    return build_saved, test_saved


def _validate_invocation_identity(
    invocation: Mapping[str, Any],
    docker_tool_ref: Mapping[str, Any],
    task_execution_id: str,
    trace_id: str,
) -> None:
    if invocation.get("kind") != "ToolInvocation":
        raise BuildTestEvaluationContractError(
            "tool_invocation must be a ToolInvocation"
        )
    status = invocation.get("status")
    if status not in {"SUCCEEDED", "FAILED"}:
        raise BuildTestEvaluationContractError(
            "tool_invocation must have a terminal status"
        )
    result_status = invocation.get("resultStatus")
    if result_status not in {"SUCCEEDED", "FAILED", "TIMED_OUT", "DENIED"}:
        raise BuildTestEvaluationContractError(
            "tool_invocation must record a terminal resultStatus"
        )
    expected_status = "SUCCEEDED" if result_status == "SUCCEEDED" else "FAILED"
    if status != expected_status:
        raise BuildTestEvaluationContractError(
            "tool_invocation status is inconsistent with resultStatus"
        )
    if invocation.get("taskExecutionId") != task_execution_id:
        raise BuildTestEvaluationContractError(
            "tool_invocation must belong to task_execution_id"
        )
    if invocation.get("traceId") != trace_id:
        raise BuildTestEvaluationContractError(
            "tool_invocation traceId must match trace_id"
        )
    if not isinstance(docker_tool_ref, Mapping):
        raise BuildTestEvaluationContractError(
            "docker_tool_ref must be a versioned Tool reference"
        )
    configured_ref = deepcopy(dict(docker_tool_ref))
    if configured_ref.get("kind") != "Tool":
        raise BuildTestEvaluationContractError(
            "docker_tool_ref must be a versioned Tool reference"
        )
    version = configured_ref.get("version")
    name = configured_ref.get("name")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(version, str)
        or not version
        or not SEMVER_PATTERN.fullmatch(version)
    ):
        raise BuildTestEvaluationContractError(
            "docker_tool_ref must be a versioned Tool reference"
        )
    if set(configured_ref) != {"kind", "name", "version"}:
        raise BuildTestEvaluationContractError(
            "docker_tool_ref must be a canonical Tool reference"
        )
    tool_ref = invocation.get("toolRef")
    if not isinstance(tool_ref, Mapping) or dict(tool_ref) != configured_ref:
        raise BuildTestEvaluationContractError(
            "tool_invocation does not reference the configured Docker Tool"
        )
    invocation_id = invocation.get("id")
    if not isinstance(invocation_id, str) or not invocation_id:
        raise BuildTestEvaluationContractError(
            "tool_invocation id must be a non-empty string"
        )


def _requested_commands(
    invocation: Mapping[str, Any],
) -> tuple[tuple[tuple[str, ...], ...], str | None]:
    request = invocation.get("input")
    if not isinstance(request, Mapping):
        return (), "Docker ToolInvocation input is missing"
    commands = request.get("commands")
    if not isinstance(commands, Sequence) or isinstance(commands, (str, bytes)):
        return (), "Docker ToolInvocation input.commands is missing"
    normalized: list[tuple[str, ...]] = []
    for index, command in enumerate(commands):
        if not isinstance(command, Mapping):
            return (), f"Docker command {index} is not an object"
        argv = command.get("argv")
        if not _valid_argv(argv):
            return (), f"Docker command {index}.argv is incomplete"
        normalized.append(tuple(argv))
    if not normalized:
        return (), "Docker ToolInvocation contains no configured commands"
    return tuple(normalized), None


def _command_records(
    invocation: Mapping[str, Any], requested: Sequence[tuple[str, ...]]
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[int, str] | None,
    str | None,
]:
    output = invocation.get("output")
    if not isinstance(output, Mapping):
        return (), None, "Docker validation output is missing"
    commands = output.get("commands")
    if not isinstance(commands, Sequence) or isinstance(commands, (str, bytes)):
        return (), None, "Docker validation output.commands is missing"
    if len(commands) > len(requested):
        return (), None, "Docker validation output contains extra command evidence"

    for index, command in enumerate(commands):
        if not isinstance(command, Mapping):
            return (), None, f"Docker output command {index} is not an object"
        argv = command.get("argv")
        if not _valid_argv(argv):
            return (), None, f"Docker output command {index}.argv is incomplete"
        if tuple(argv) != requested[index]:
            return (), None, (
                "Docker output commands do not match the configured order"
            )

    normalized: list[dict[str, Any]] = []
    for index, command in enumerate(commands):
        argv = command.get("argv")
        duration = command.get("durationMs")
        exit_code = command.get("exitCode")
        logs_ref = command.get("logsRef")
        if (
            not _valid_argv(argv)
            or not isinstance(command.get("stdout"), str)
            or not isinstance(command.get("stderr"), str)
            or not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
            or not isinstance(duration, int)
            or isinstance(duration, bool)
            or duration < 0
            or not isinstance(logs_ref, str)
            or not logs_ref
        ):
            return (
                tuple(normalized),
                (
                    index,
                    f"Docker output command {index} is incomplete",
                ),
                None,
            )
        normalized.append(deepcopy(dict(command)))
    return tuple(normalized), None, None


def _result_sequence_error(
    invocation: Mapping[str, Any],
    requested: Sequence[tuple[str, ...]],
    records: Sequence[Mapping[str, Any]],
) -> str | None:
    result_status = invocation["resultStatus"]
    if result_status == "SUCCEEDED" and len(records) != len(requested):
        return "successful Docker validation output is incomplete"
    if result_status == "TIMED_OUT":
        if len(records) >= len(requested):
            return "timed-out Docker validation contains a complete command sequence"
        if any(record["exitCode"] != 0 for record in records):
            return "timed-out Docker validation contains nonzero command evidence"
    if result_status == "FAILED":
        first_failure = next(
            (index for index, record in enumerate(records) if record["exitCode"] != 0),
            None,
        )
        if first_failure is not None and first_failure != len(records) - 1:
            return "Docker validation contains command evidence after a nonzero exit"
    if result_status == "DENIED" and records:
        return "denied Docker validation contains command execution evidence"
    return None


def _valid_argv(value: Any) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and bool(value)
        and all(isinstance(item, str) and item for item in value)
    )


def _make_result(
    *,
    result_id: str,
    task_execution_id: str,
    evaluation_type: str,
    expectation: ValidationExpectation,
    invocation: Mapping[str, Any],
    requested_commands: Sequence[tuple[str, ...]],
    command_records: Sequence[Mapping[str, Any]],
    shared_configuration_error: str | None,
    output_error: tuple[int, str] | None,
    trace_id: str,
    timestamp: str,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    index = expectation.command_index
    errors: list[dict[str, str]] = []
    command_status = "INVALID_OUTPUT"
    duration_ms = 0
    logs_ref = invocation.get("logsAddress")
    exit_code: int | None = None

    configuration_error = shared_configuration_error
    if configuration_error is None and output_error is not None and index >= output_error[0]:
        configuration_error = output_error[1]
    if configuration_error is None and index >= len(requested_commands):
        configuration_error = (
            f"{evaluation_type} command index {index} is not configured in the "
            "Docker ToolInvocation"
        )

    if configuration_error is not None:
        errors.append({"code": "CONFIGURATION", "message": configuration_error})
    elif index < len(command_records):
        record = command_records[index]
        exit_code = record["exitCode"]
        duration_ms = record["durationMs"]
        logs_ref = record["logsRef"]
        command_status = "PASSED" if exit_code == 0 else "FAILED"
        if exit_code != 0:
            errors.append(
                {
                    "code": "NONZERO_EXIT",
                    "message": (
                        f"{evaluation_type} command exited with code {exit_code}"
                    ),
                }
            )
    else:
        result_status = invocation.get("resultStatus")
        first_missing = len(command_records)
        if result_status == "TIMED_OUT" and index == first_missing:
            command_status = "TIMED_OUT"
            errors.append(
                {
                    "code": "TIMEOUT",
                    "message": f"{evaluation_type} command timed out before completion",
                }
            )
        elif (
            result_status == "FAILED" and index >= first_missing
        ) or (
            result_status == "TIMED_OUT" and index > first_missing
        ):
            command_status = "NOT_RUN"
            errors.append(
                {
                    "code": "NOT_RUN",
                    "message": (
                        f"{evaluation_type} command was not run after an earlier "
                        "validation failure"
                    ),
                }
            )
        else:
            command_status = "INVALID_OUTPUT"
            configuration_error = (
                f"Docker validation output is missing {evaluation_type} command "
                f"evidence at index {index}"
            )
            errors.append({"code": "CONFIGURATION", "message": configuration_error})

    expected_argv = (
        list(requested_commands[index]) if index < len(requested_commands) else []
    )
    errors.sort(key=lambda item: (item["code"], item["message"]))
    evaluation_status = "FAILED" if configuration_error is not None else "SUCCEEDED"
    outcome = "PASS" if command_status == "PASSED" else "FAIL"
    evidence = {
        "type": "docker-command-evaluation",
        "evaluationType": evaluation_type,
        "toolInvocationId": invocation["id"],
        "toolResultStatus": invocation.get("resultStatus"),
        "commandIndex": index,
        "expectedArgv": expected_argv,
        "commandStatus": command_status,
        "exitCode": exit_code,
        "durationMs": duration_ms,
        "logsRef": logs_ref,
        "errors": errors,
    }
    evidence_json = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    summary = (
        f"{evaluation_type.capitalize()} validation passed"
        if outcome == "PASS"
        else f"{evaluation_type.capitalize()} validation {command_status.lower()}"
    )
    result: dict[str, Any] = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "EvaluationResult",
        "id": result_id,
        "traceId": trace_id,
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "provenance": deepcopy(dict(provenance)),
        "taskExecutionId": task_execution_id,
        "evaluationRef": deepcopy(dict(expectation.evaluation_ref)),
        "target": {"type": "ToolInvocation", "id": invocation["id"]},
        "status": evaluation_status,
        "outcome": outcome,
        "metrics": {"checks": 1, "passed": int(outcome == "PASS"), "durationMs": duration_ms},
        "logs": [summary],
        "evidence": evidence,
        "evidenceAddress": f"sha256:{sha256(evidence_json.encode()).hexdigest()}",
        "startedAt": timestamp,
        "completedAt": timestamp,
    }
    if isinstance(logs_ref, str) and logs_ref:
        result["logsAddress"] = logs_ref
    if configuration_error is not None:
        result["failure"] = {
            "class": "CONFIGURATION",
            "message": configuration_error,
            "retryable": False,
        }
    return result


def _validate_result(result: Mapping[str, Any]) -> None:
    errors = sorted(
        _evaluation_result_validator().iter_errors(dict(result)),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        error = errors[0]
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        raise BuildTestEvaluationContractError(
            f"invalid EvaluationResult at {path}: {error.message}"
        )


@cache
def _evaluation_result_validator() -> Draft202012Validator:
    schema_root = Path(__file__).parents[2] / "schemas"
    schema_paths = (
        schema_root / "resources" / "v1" / "resource-definitions.schema.json",
        schema_root / "runtime" / "v1" / "runtime-definitions.schema.json",
        schema_root / "runtime" / "v1" / "evaluationresult.schema.json",
    )
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in schema_paths]
    registry = Registry()
    for schema in schemas:
        registry = registry.with_resource(
            schema["$id"],
            SchemaResource.from_contents(schema, default_specification=DRAFT202012),
        )
    return Draft202012Validator(schemas[-1], registry=registry)
