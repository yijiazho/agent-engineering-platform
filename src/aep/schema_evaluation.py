"""Deterministic JSON Schema evaluation for artifact and invocation outputs."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError, validators
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.runtime_store import RuntimeObject, RuntimeObjectStore


class SchemaEvaluationContractError(ValueError):
    """Raised when an EvaluationResult would violate the runtime contract."""


def evaluate_schema(
    *,
    store: RuntimeObjectStore,
    result_id: str,
    task_execution_id: str,
    evaluation_ref: Mapping[str, Any],
    target: Mapping[str, Any],
    content: Any,
    schema: Mapping[str, Any],
    trace_id: str,
    timestamp: str,
    provenance: Mapping[str, Any],
) -> RuntimeObject:
    """Validate content, persist immutable evidence, and return the saved result."""

    schema_copy = deepcopy(dict(schema))
    errors = _validation_errors(content, schema_copy)
    outcome = "PASS" if not errors else "FAIL"
    schema_version = schema_copy.get("$schema", "https://json-schema.org/draft/2020-12/schema")
    evidence = {
        "type": "json-schema-validation",
        "schemaVersion": schema_version,
        "valid": not errors,
        "errors": errors,
    }
    logs = (
        ["JSON Schema validation passed"]
        if not errors
        else [f"{error['path']}: {error['message']}" for error in errors]
    )
    evidence_json = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    result = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "EvaluationResult",
        "id": result_id,
        "traceId": trace_id,
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "provenance": deepcopy(dict(provenance)),
        "taskExecutionId": task_execution_id,
        "evaluationRef": deepcopy(dict(evaluation_ref)),
        "target": deepcopy(dict(target)),
        "status": "SUCCEEDED",
        "outcome": outcome,
        "metrics": {"checks": 1, "passed": int(not errors), "errors": len(errors)},
        "logs": logs,
        "evidence": evidence,
        "evidenceAddress": f"sha256:{sha256(evidence_json.encode()).hexdigest()}",
        "startedAt": timestamp,
        "completedAt": timestamp,
    }
    _validate_result_contract(result)
    return store.create(result, deterministic_key=f"schema-evaluation:{result_id}")


def _validation_errors(content: Any, schema: dict[str, Any]) -> list[dict[str, str]]:
    try:
        validator_class = validators.validator_for(
            schema,
            default=validators.Draft202012Validator if "$schema" not in schema else None,
        )
        if validator_class is None:
            declared = schema.get("$schema")
            return [{"path": "$schema", "message": f"Unsupported JSON Schema version: {declared!r}"}]
        validator_class.check_schema(schema)
        errors = validator_class(schema).iter_errors(content)
        return sorted((_error_record(error) for error in errors), key=_error_sort_key)
    except (SchemaError, TypeError) as error:
        return [{"path": "$schema", "message": f"Invalid JSON Schema: {error.message if isinstance(error, SchemaError) else error}"}]


def _error_record(error: ValidationError) -> dict[str, str]:
    path_parts = [str(part) for part in error.absolute_path]
    if error.validator == "required":
        missing = _missing_property(error.message)
        if missing is not None:
            path_parts.append(missing)
    path = "$" + "".join(
        f"[{part}]" if part.isdigit() else f".{part}" for part in path_parts
    )
    return {"path": path, "message": error.message}


def _missing_property(message: str) -> str | None:
    match = re.fullmatch(r"'(.+)' is a required property", message)
    return match.group(1) if match else None


def _error_sort_key(error: dict[str, str]) -> tuple[str, str]:
    return error["path"], error["message"]


def _validate_result_contract(result: dict[str, Any]) -> None:
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
    validator = Draft202012Validator(schemas[-1], registry=registry)
    errors = sorted(validator.iter_errors(result), key=lambda error: list(error.absolute_path))
    if errors:
        error = errors[0]
        path = "$" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path)
        raise SchemaEvaluationContractError(f"invalid EvaluationResult at {path}: {error.message}")
