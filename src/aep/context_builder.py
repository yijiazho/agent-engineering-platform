"""Deterministic, provenance-complete ContextPackage construction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from functools import cache
from hashlib import sha256
import json
from math import ceil
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Final, Protocol

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.generated_artifact_store import GeneratedArtifactStore
from aep.observability import StructuredLifecycleLogger, propagation_fields
from aep.repository_knowledge import (
    CandidateFileQuery,
    DependencyManifestQuery,
    DocumentationQuery,
    FileQuery,
    KnowledgeResult,
    RepositoryKnowledgeProvider,
    TestHintQuery,
)
from aep.resource_loader import Resource
from aep.runtime_store import RuntimeObjectStore


JsonMapping = Mapping[str, Any]


class RepositoryPlanningEvidenceReader(Protocol):
    def inspect(self, path: str, revision: str, *, max_bytes: int,
                strategy: str, status_scan_bytes: int) -> Any: ...

    def verify_absent(self, path: str, revision: str) -> bool: ...

SUPPORTED_CONTEXT: Final = frozenset(
    {
        "task",
        "event",
        "issue",
        "repository-inventory",
        "candidate-files",
        "planning-evidence",
        "editable-targets",
        "documentation",
        "dependency-manifests",
        "test-hints",
        "knowledge",
        "policies",
        "prior-artifacts",
    }
)
MAX_INPUT_CONTEXT_TOKENS: Final = 1_000_000
REPOSITORY_INVENTORY_LIMIT: Final = 20
CANDIDATE_FILE_LIMIT: Final = 20
DOCUMENTATION_LIMIT: Final = 8
KNOWLEDGE_SOURCE_LIMIT: Final = 8
DEFAULT_PLANNING_FILE_CEILING: Final = 256 * 1024
DEFAULT_PLANNING_TOTAL_CEILING: Final = 1024 * 1024
DEFAULT_STATUS_SCAN_CEILING: Final = 64 * 1024


class ContextBuilderError(Exception):
    """Base class for ContextPackage construction failures."""


class ContextInputValidationError(ContextBuilderError):
    """Raised when build inputs violate their declared relationships."""


class RequiredContextError(ContextBuilderError):
    """Raised when mandatory context cannot be resolved."""


class ContextBudgetExceededError(ContextBuilderError):
    """Raised when mandatory context alone exceeds the token budget."""


class ContextPackageValidationError(ContextBuilderError):
    """Raised when constructed output violates the runtime schema."""


class ImmutableContextPackageError(ContextBuilderError):
    """Raised when a TaskExecution's published package would be replaced."""


class ContextBuilder:
    """Build immutable ContextPackages without repository or model access."""

    def __init__(
        self,
        *,
        repository_knowledge: RepositoryKnowledgeProvider,
        artifact_store: GeneratedArtifactStore,
        runtime_store: RuntimeObjectStore | None = None,
        lifecycle_logger: StructuredLifecycleLogger | None = None,
        repository_file_reader: RepositoryPlanningEvidenceReader | None = None,
    ) -> None:
        if not isinstance(repository_knowledge, RepositoryKnowledgeProvider):
            raise TypeError("repository_knowledge must implement RepositoryKnowledgeProvider")
        if not isinstance(artifact_store, GeneratedArtifactStore):
            raise TypeError("artifact_store must implement GeneratedArtifactStore")
        self._repository_knowledge = repository_knowledge
        self._artifact_store = artifact_store
        self._runtime_store = runtime_store
        self._lifecycle_logger = lifecycle_logger
        if repository_file_reader is not None and not all(callable(
            getattr(repository_file_reader, method, None)
        ) for method in ("inspect", "verify_absent")):
            raise TypeError(
                "repository_file_reader must implement inspect and verify_absent"
            )
        self._repository_file_reader = repository_file_reader

    def build(
        self,
        *,
        task: Resource | JsonMapping,
        task_execution: JsonMapping,
        workflow_execution: JsonMapping,
        event: JsonMapping | None,
        knowledge_bases: Sequence[Resource | JsonMapping] = (),
        policies: Sequence[Resource | JsonMapping] = (),
        prior_task_execution_ids: Sequence[str] = (),
        editable_targets: Sequence[JsonMapping] = (),
        optional_context: Sequence[str] | None = None,
        token_budget: int | None = None,
        created_at: str,
    ) -> JsonMapping:
        """Construct and optionally persist one immutable ContextPackage."""
        task_data = _resource_data(task, expected_kind="Task")
        knowledge_data = tuple(
            _resource_data(item, expected_kind="KnowledgeBase")
            for item in knowledge_bases
        )
        policy_data = tuple(
            _resource_data(item, expected_kind="Policy") for item in policies
        )
        _validate_bound_resources(
            task_data, "knowledgeBases", knowledge_data, expected_kind="KnowledgeBase"
        )
        _validate_bound_resources(
            task_data, "policies", policy_data, expected_kind="Policy"
        )
        required = _context_names(
            task_data.get("spec", {}).get("requiredContext", ()),
            field="task.spec.requiredContext",
        )
        task_spec = task_data.get("spec", {})
        declared_optional = task_spec.get("optionalContext", ())
        optional = _context_names(
            declared_optional if optional_context is None else optional_context,
            field=(
                "task.spec.optionalContext"
                if optional_context is None
                else "optional_context"
            ),
        )
        if optional_context is not None and "optionalContext" in task_spec:
            configured = _context_names(
                declared_optional, field="task.spec.optionalContext"
            )
            if optional != configured:
                raise ContextInputValidationError(
                    "optional_context does not match Task.spec.optionalContext"
                )
        configured_budget = task_spec.get("inputContextTokenBudget")
        if token_budget is None:
            token_budget = configured_budget
        elif configured_budget is not None and token_budget != configured_budget:
            raise ContextInputValidationError(
                "token_budget does not match Task.spec.inputContextTokenBudget"
            )
        if required.intersection(optional):
            overlap = sorted(required.intersection(optional))
            raise ContextInputValidationError(
                f"context cannot be both required and optional: {overlap!r}"
            )
        _validate_inputs(
            task_data=task_data,
            task_execution=task_execution,
            workflow_execution=workflow_execution,
            event=event,
            required_context=required,
            prior_task_execution_ids=prior_task_execution_ids,
            token_budget=token_budget,
            created_at=created_at,
        )

        task_ref = _resource_ref(task_data)
        repository_revision = workflow_execution["repositoryRevision"]
        task_execution_id = task_execution["id"]
        correlation = propagation_fields(
            workflow_execution, task_execution_id=task_execution_id
        )
        trace_id = correlation["traceId"]
        workflow_execution_id = correlation["workflowExecutionId"]
        base_provenance = {
            "actor": "context-builder",
            "workflowExecutionId": workflow_execution_id,
            "taskExecutionId": task_execution_id,
            "repositoryRevision": repository_revision,
            "resourceRefs": [task_ref],
        }
        knowledge_graph_version = workflow_execution.get("knowledgeGraphVersion")
        if knowledge_graph_version is not None:
            base_provenance["knowledgeGraphVersion"] = knowledge_graph_version

        mandatory: list[tuple[str, dict[str, Any]]] = []
        optional_candidates: list[tuple[str, dict[str, Any]]] = []
        mandatory.append(("task", _resource_element("task", task_data, task_ref)))
        if "editable-targets" in required or "editable-targets" in optional:
            if not editable_targets:
                raise RequiredContextError("editable-targets requires every planned file preimage")
            target = mandatory if "editable-targets" in required else optional_candidates
            seen_targets: set[str] = set()
            for editable in sorted(
                editable_targets,
                key=lambda item: (str(item.get("path", "")).casefold(), str(item.get("path", ""))),
            ):
                path = editable.get("path")
                content = editable.get("content")
                digest = editable.get("preimageSha256")
                exists = editable.get("exists")
                if not isinstance(path, str) or not path or path in seen_targets:
                    raise RequiredContextError("editable targets must have unique non-empty paths")
                if not isinstance(content, str):
                    raise RequiredContextError(f"editable target {path!r} is not UTF-8 text")
                encoded = content.encode("utf-8")
                actual = sha256(encoded).hexdigest()
                if not isinstance(exists, bool):
                    raise RequiredContextError(f"editable target {path!r} must declare existence")
                if not exists and content:
                    raise RequiredContextError(f"absent editable target {path!r} must have an empty preimage")
                if digest != actual:
                    raise RequiredContextError(f"editable target {path!r} preimage digest is stale")
                if editable.get("repositoryRevision") != repository_revision:
                    raise RequiredContextError(f"editable target {path!r} revision is stale")
                seen_targets.add(path)
                target.append(("editable-targets", {
                    "type": "editable-target",
                    "content": {
                        "path": path,
                        "exists": exists,
                        "preimageState": "PRESENT" if exists else "ABSENT",
                        "content": content,
                        "contentAddress": f"sha256:{actual}",
                        "preimageSha256": actual,
                        "byteCount": len(encoded),
                        "tokenEstimate": max(1, ceil(len(encoded) / 4)),
                    },
                    "provenance": _json_copy(editable.get("provenance", {})),
                }))
        if event is not None:
            mandatory.append(
                (
                    "event",
                    {
                        "type": "event",
                        "content": _json_copy(event),
                        "provenance": {
                            "actor": "event-normalizer",
                            "repositoryRevision": repository_revision,
                            "resourceRefs": _event_refs(workflow_execution),
                        },
                    },
                )
            )

        terms = _search_terms(task_data, event)
        requested_repository = sorted(
            (required | optional)
            & {
                "repository-inventory",
                "candidate-files",
                "documentation",
                "dependency-manifests",
                "test-hints",
            }
        )
        repository_results: dict[str, tuple[KnowledgeResult, ...]] = {}
        for name in requested_repository:
            if name == "planning-evidence":
                continue
            results = self._query_repository(name, terms)
            repository_results[name] = results
            _require_result_binding(
                results, repository_revision, knowledge_graph_version
            )
            target = mandatory if name in required else optional_candidates
            for result in results:
                target.append((name, _knowledge_element("repository", result)))

        if "planning-evidence" in required or "planning-evidence" in optional:
            if self._repository_file_reader is None:
                raise RequiredContextError("planning-evidence requires a revision-bound repository reader")
            from aep.planning_evidence import evaluate_path_predicates, finalize_planning_evidence
            declarations = self._planning_predicate_declarations(
                task_spec, prior_task_execution_ids
            )
            if isinstance(declarations, (str, bytes)) or not isinstance(declarations, Sequence) or not declarations:
                raise RequiredContextError("planning-evidence requires Task.spec.planningPredicates")
            candidates_by_id: dict[str, KnowledgeResult] = {}
            absent_exact_paths: set[str] = set()
            for declaration in declarations:
                if not isinstance(declaration, Mapping):
                    raise RequiredContextError("planning predicate declarations must be objects")
                if "pathPrefix" in declaration and "maxPaths" not in declaration:
                    raise RequiredContextError(
                        "planning-evidence prefix declarations require maxPaths"
                    )
                max_paths = int(declaration.get("maxPaths", 1))
                if isinstance(declaration.get("path"), str):
                    scoped = self._repository_knowledge.lookup_file(
                        FileQuery(path=str(declaration["path"]))
                    )
                    if len(scoped) > 1:
                        raise RequiredContextError(
                            f"planning-evidence exact target {declaration['path']!r} was not unique"
                        )
                    if not scoped:
                        absent_exact_paths.add(str(declaration["path"]))
                else:
                    scoped = self._repository_knowledge.search_candidate_files(
                        CandidateFileQuery(terms=(),
                            path_prefix=str(declaration.get("pathPrefix", "")),
                            limit=max_paths + 1)
                    )
                    if len(scoped) > max_paths:
                        raise RequiredContextError(
                            f"planning-evidence prefix {declaration.get('pathPrefix')!r} exceeds maxPaths"
                        )
                _require_result_binding(scoped, repository_revision, knowledge_graph_version)
                for result in scoped:
                    candidates_by_id[result.id] = result
            candidates = tuple(sorted(candidates_by_id.values(),
                key=lambda item: item.provenance.source.path))
            candidate_sources = [
                (item.provenance.source.path, item.id) for item in candidates
            ] + [
                (path, f"absent:{repository_revision}:{path}")
                for path in absent_exact_paths
                if path not in {item.provenance.source.path for item in candidates}
            ]
            candidate_sources.sort(key=lambda item: (item[0].casefold(), item[0]))
            evidence_target = mandatory if "planning-evidence" in required else optional_candidates
            matched = 0
            seen_paths: set[str] = set()
            for path, source_id in candidate_sources:
                if path in seen_paths:
                    raise RequiredContextError(
                        f"planning-evidence candidate {path!r} is duplicated"
                    )
                predicates = []
                postconditions = []
                reasons = []
                declared_max_bytes: int | None = None
                for declaration in declarations:
                    if not isinstance(declaration, Mapping):
                        raise RequiredContextError("planning predicate declarations must be objects")
                    exact = declaration.get("path")
                    prefix = declaration.get("pathPrefix")
                    applies = exact == path or (isinstance(prefix, str) and (path == prefix or path.startswith(prefix.rstrip("/") + "/")))
                    if not applies:
                        continue
                    predicate = declaration.get("predicate")
                    postcondition = declaration.get("postcondition")
                    if not isinstance(predicate, Mapping) or not isinstance(postcondition, Mapping):
                        raise RequiredContextError("planning predicates require predicate and postcondition")
                    predicates.append(dict(predicate))
                    postconditions.append(dict(postcondition))
                    reasons.append(str(declaration.get("selectionReason", "TASK_DECLARED_PREDICATE")))
                    hint = declaration.get("maxBytes")
                    if hint is not None:
                        hint = int(hint)
                        declared_max_bytes = hint if declared_max_bytes is None else min(declared_max_bytes, hint)
                if not predicates:
                    continue
                inspection = task_spec.get("planningEvidenceInspection")
                if not isinstance(inspection, Mapping) or not all(
                    key in inspection for key in (
                        "maxFileBytes", "maxTotalBytes", "statusFieldScanBytes"
                    )
                ):
                    raise RequiredContextError(
                        "planning-evidence requires explicit Task inspection limits"
                    )
                trusted_ceiling = int(inspection["maxFileBytes"])
                total_ceiling = int(inspection["maxTotalBytes"])
                status_ceiling = int(inspection["statusFieldScanBytes"])
                kinds = {str(item.get("kind")) for item in (*predicates, *postconditions)}
                strategy = "STRUCTURED_STATUS_FIELD_SCAN" if kinds <= {"STATUS_EQUALS"} else "COMPLETE_BLOB_SCAN"
                applied_ceiling = trusted_ceiling
                inspected_so_far = sum(
                    int(item[1]["content"].get("inspection", {}).get("inspectedBytes", 0))
                    for item in evidence_target if item[0] == "planning-evidence"
                )
                applied_ceiling = min(applied_ceiling, total_ceiling - inspected_so_far)
                if applied_ceiling <= 0:
                    failure = RequiredContextError(
                        f"planning-evidence target {path!r} failed closed: AGGREGATE_SIZE_LIMIT_EXCEEDED"
                    )
                    failure.metadata = {
                        "reason": "AGGREGATE_SIZE_LIMIT_EXCEEDED", "path": path,
                        "declaredMaxBytesHint": declared_max_bytes,
                        "blobSize": None, "appliedTrustedCeiling": total_ceiling,
                        "predicateType": "+".join(sorted(kinds)),
                        "inspectionStrategy": strategy,
                        "evaluationComplete": False,
                    }
                    raise failure
                try:
                    if path in absent_exact_paths:
                        confirmed_absent = self._repository_file_reader.verify_absent(
                            path, repository_revision
                        )
                    else:
                        confirmed_absent = False
                    if confirmed_absent:
                        content = ""
                        blob_size, blob_digest, inspected_bytes, status_fields = (
                            0, sha256(b"").hexdigest(), 0, ()
                        )
                    else:
                        inspected = self._repository_file_reader.inspect(
                            path, repository_revision, max_bytes=applied_ceiling,
                            strategy=strategy, status_scan_bytes=status_ceiling,
                        )
                        content = inspected.content
                        blob_size, blob_digest = inspected.blob_size, inspected.blob_sha256
                        inspected_bytes, status_fields = inspected.inspected_bytes, inspected.status_fields
                    record = evaluate_path_predicates(path=path, content=content,
                        repository_revision=repository_revision, predicates=predicates,
                        source_id=source_id, max_bytes=applied_ceiling,
                        blob_size=blob_size, blob_sha256=blob_digest,
                        declared_max_bytes=declared_max_bytes,
                        inspection_strategy=strategy, status_fields=status_fields,
                        inspected_bytes=inspected_bytes, status_scan_bytes=status_ceiling)
                    postcondition_record = evaluate_path_predicates(
                        path=path, content=content,
                        repository_revision=repository_revision,
                        predicates=postconditions, source_id=source_id,
                        max_bytes=applied_ceiling, blob_size=blob_size,
                        blob_sha256=blob_digest, declared_max_bytes=declared_max_bytes,
                        inspection_strategy=strategy, status_fields=status_fields,
                        inspected_bytes=inspected_bytes, status_scan_bytes=status_ceiling,
                    )
                except (OSError, UnicodeError, ValueError) as error:
                    reason = getattr(error, "reason", None) or {
                        FileNotFoundError: "TARGET_MISSING", UnicodeDecodeError: "INVALID_UTF8",
                        IsADirectoryError: "NON_REGULAR_FILE",
                    }.get(type(error), type(error).__name__.upper())
                    failure = RequiredContextError(
                        f"planning-evidence target {path!r} failed closed: {reason}"
                    )
                    failure.metadata = {
                        "reason": reason, "path": path,
                        "declaredMaxBytesHint": declared_max_bytes,
                        "blobSize": getattr(error, "metadata", {}).get("blobSize"),
                        "appliedTrustedCeiling": applied_ceiling,
                        "predicateType": "+".join(sorted(kinds)),
                        "inspectionStrategy": strategy,
                        "evaluationComplete": False,
                    }
                    raise failure from error
                record = finalize_planning_evidence(
                    record, postconditions=postconditions, selection_reasons=reasons,
                    postcondition_results=postcondition_record["predicateResults"],
                )
                evidence_target.append(("planning-evidence", {
                    "type": "planning-evidence", "content": record,
                    "provenance": {"actor": "context-builder", "repositoryRevision": repository_revision,
                        "knowledgeGraphVersion": knowledge_graph_version,
                        "resourceRefs": [task_ref]},
                }))
                seen_paths.add(path)
                matched += 1
            if matched == 0:
                raise RequiredContextError("planning-evidence matched no bounded candidate paths")

        for knowledge_base in sorted(knowledge_data, key=_resource_sort_key):
            ref = _resource_ref(knowledge_base)
            results = self._query_knowledge_base(knowledge_base, terms)
            _require_result_binding(
                results, repository_revision, knowledge_graph_version
            )
            target = optional_candidates if "knowledge" in optional else mandatory
            for result in results:
                target.append(
                    ("knowledge", _knowledge_element("knowledge", result, ref=ref))
                )

        for policy in sorted(policy_data, key=_resource_sort_key):
            ref = _resource_ref(policy)
            target = optional_candidates if "policies" in optional else mandatory
            target.append(("policies", _resource_element("policy", policy, ref)))

        artifact_refs: list[dict[str, str]] = []
        for producer_id in _unique_nonempty(prior_task_execution_ids):
            producer = self._validate_prior_producer(
                producer_id,
                task_execution=task_execution,
                workflow_execution=workflow_execution,
            )
            for metadata in self._artifact_store.list_by_task_execution(producer_id):
                _validate_artifact_binding(
                    metadata,
                    producer=producer,
                    workflow_execution=workflow_execution,
                )
                content = self._artifact_store.get_content(metadata["id"])
                artifact_refs.append(
                    {
                        "generatedArtifactId": metadata["id"],
                        "contentAddress": metadata["contentAddress"],
                    }
                )
                target = (
                    optional_candidates
                    if "prior-artifacts" in optional
                    else mandatory
                )
                target.append(
                    (
                        "prior-artifacts",
                        {
                            "type": "artifact",
                            "content": {
                                "metadata": _json_copy(metadata),
                                "content": _decode_artifact(content, metadata.get("mediaType")),
                            },
                            "provenance": {
                                "actor": "generated-artifact-store",
                                "workflowExecutionId": workflow_execution_id,
                                "taskExecutionId": producer_id,
                                "repositoryRevision": repository_revision,
                                "resourceRefs": [],
                                "inputArtifactRefs": [artifact_refs[-1]],
                            },
                        },
                    )
                )

        resolved_names = {name for name, _ in mandatory + optional_candidates}
        if "issue" in required and event is not None and "issue" in event:
            resolved_names.add("issue")
        _validate_required_context(
            required,
            resolved_names,
        )

        mandatory, optional_candidates = _deduplicate_context_candidates(
            mandatory, optional_candidates
        )
        prepared_mandatory = [
            (name, _with_token_metadata(element)) for name, element in mandatory
        ]
        mandatory_tokens = sum(element["tokenCount"] for _, element in prepared_mandatory)
        if mandatory_tokens > token_budget:
            raise ContextBudgetExceededError(
                f"mandatory context requires approximately {mandatory_tokens} tokens, "
                f"exceeding budget {token_budget}"
            )

        elements = [element for _, element in prepared_mandatory]
        selected_names = [name for name, _ in mandatory]
        selected_categories = [name for name, _ in prepared_mandatory]
        mandatory_identity_indices = {
            identity: index
            for index, element in enumerate(elements)
            if (identity := _repository_identity(element)) is not None
        }
        discarded: list[dict[str, Any]] = []
        token_count = mandatory_tokens
        for name, element in optional_candidates:
            identity = _repository_identity(element)
            if identity is not None and identity in mandatory_identity_indices:
                index = mandatory_identity_indices[identity]
                merged = _with_token_metadata(
                    _merge_duplicate(_without_token_metadata(elements[index]), element)
                )
                incremental_tokens = max(
                    0, merged["tokenCount"] - elements[index]["tokenCount"]
                )
                if token_count + incremental_tokens <= token_budget:
                    elements[index] = merged
                    selected_names.extend(
                        reason
                        for reason in _selection_reasons(element, name)
                        if reason in optional
                    )
                    token_count += incremental_tokens
                else:
                    discarded.append(
                        {
                            "context": name,
                            "reason": "TOKEN_BUDGET",
                            "estimatedTokens": incremental_tokens,
                            "selectionReasons": _selection_reasons(element, name),
                        }
                    )
                continue
            prepared = _with_token_metadata(element)
            if token_count + prepared["tokenCount"] <= token_budget:
                elements.append(prepared)
                selected_names.append(name)
                selected_categories.append(name)
                token_count += prepared["tokenCount"]
            else:
                discarded.append(
                    {
                        "context": name,
                        "reason": "TOKEN_BUDGET",
                        "estimatedTokens": prepared["tokenCount"],
                        "selectionReasons": _selection_reasons(prepared, name),
                    }
                )

        selection = {
            "requiredContext": sorted(required),
            "optionalContext": sorted(optional),
            "selected": selected_names,
            "discarded": discarded,
        }
        breakdown = _token_breakdown(selected_categories, elements)
        package_seed = {
            "createdAt": created_at,
            "traceId": trace_id,
            "workflowExecutionId": workflow_execution_id,
            "taskExecutionId": task_execution_id,
            "taskRef": task_ref,
            "repositoryRevision": repository_revision,
            "elements": elements,
            "tokenBudget": token_budget,
            "selection": selection,
        }
        digest = sha256(_canonical_json(package_seed)).hexdigest()
        package = {
            "apiVersion": "aep.dev/v1alpha1",
            "kind": "ContextPackage",
            "id": f"contextpackage-{digest[:20]}",
            "traceId": trace_id,
            "createdAt": created_at,
            "updatedAt": created_at,
            "provenance": {
                **base_provenance,
                "inputArtifactRefs": artifact_refs,
            },
            "taskExecutionId": task_execution_id,
            "taskRef": task_ref,
            "repositoryRevision": repository_revision,
            "elements": elements,
            "tokenBudget": token_budget,
            "tokenCount": token_count,
            "tokenEstimate": {
                "algorithm": "utf8-bytes-ceiling-divided-by-4",
                "count": token_count,
                "breakdown": breakdown,
            },
            "truncation": "PRUNED" if discarded else "NONE",
            "selection": selection,
        }
        _validate_context_package(package)
        if self._runtime_store is not None:
            stored = self._runtime_store.create(
                package,
                deterministic_key=f"context-package:{task_execution_id}",
            )
            if dict(stored) != package:
                raise ImmutableContextPackageError(
                    f"TaskExecution {task_execution_id!r} already has a different "
                    "immutable ContextPackage"
                )
            package = dict(stored)
        if self._lifecycle_logger is not None:
            self._lifecycle_logger.emit(
                event_name="ContextPackageCreated",
                service="context-builder",
                runtime_object=package,
                emitted_at=created_at,
                status="CREATED",
            )
        return _freeze(package)

    def _validate_prior_producer(
        self,
        producer_id: str,
        *,
        task_execution: JsonMapping,
        workflow_execution: JsonMapping,
    ) -> JsonMapping:
        dependencies = task_execution.get("dependencyTaskExecutionIds", ())
        if producer_id not in dependencies:
            raise ContextInputValidationError(
                f"prior artifact producer {producer_id!r} is not a dependency of "
                f"TaskExecution {task_execution.get('id')!r}"
            )
        if self._runtime_store is None:
            raise ContextInputValidationError(
                "runtime_store is required to validate prior artifact producers"
            )
        producer = self._runtime_store.get(producer_id)
        if producer is None or producer.get("kind") != "TaskExecution":
            raise ContextInputValidationError(
                f"prior artifact producer TaskExecution {producer_id!r} was not found"
            )
        if producer.get("workflowExecutionId") != workflow_execution.get("id"):
            raise ContextInputValidationError(
                f"prior artifact producer {producer_id!r} belongs to another "
                "WorkflowExecution"
            )
        if producer.get("traceId") != workflow_execution.get("traceId"):
            raise ContextInputValidationError(
                f"prior artifact producer {producer_id!r} belongs to another trace"
            )
        if producer.get("status") != "SUCCEEDED":
            raise ContextInputValidationError(
                f"prior artifact producer {producer_id!r} has not succeeded"
            )
        producer_revision = producer.get("provenance", {}).get(
            "repositoryRevision"
        )
        if producer_revision != workflow_execution.get("repositoryRevision"):
            raise ContextInputValidationError(
                f"prior artifact producer {producer_id!r} belongs to repository "
                f"revision {producer_revision!r}, expected "
                f"{workflow_execution.get('repositoryRevision')!r}"
            )
        return producer

    def _planning_predicate_declarations(
        self, task_spec: JsonMapping, prior_task_execution_ids: Sequence[str]
    ) -> Sequence[JsonMapping]:
        configured = task_spec.get("planningPredicates", ())
        if configured:
            return configured
        if task_spec.get("planningPredicateSource") != "PRIOR_ISSUE_ANALYSIS":
            return ()
        for producer_id in _unique_nonempty(prior_task_execution_ids):
            for metadata in self._artifact_store.list_by_task_execution(producer_id):
                if metadata.get("artifactType") != "ISSUE_ANALYSIS":
                    continue
                content = _decode_artifact(
                    self._artifact_store.get_content(metadata["id"]),
                    metadata.get("mediaType"),
                )
                declarations = (
                    content.get("planningPredicates")
                    if isinstance(content, Mapping) else None
                )
                if isinstance(declarations, Sequence) and not isinstance(
                    declarations, (str, bytes)
                ):
                    normalized = []
                    for item in declarations:
                        if not isinstance(item, Mapping):
                            raise RequiredContextError(
                                "prior ISSUE_ANALYSIS planning predicates must be objects"
                            )
                        record = dict(item)
                        scope_type = record.pop("scopeType", None)
                        if scope_type == "PREFIX":
                            record["pathPrefix"] = record.pop("path", None)
                        elif scope_type != "EXACT":
                            raise RequiredContextError(
                                "prior ISSUE_ANALYSIS planning predicate scopeType is invalid"
                            )
                        normalized.append(record)
                    return normalized
        raise RequiredContextError(
            "planning-evidence requires predicate declarations from prior ISSUE_ANALYSIS"
        )

    def _query_repository(
        self, requirement: str, terms: tuple[str, ...]
    ) -> tuple[KnowledgeResult, ...]:
        if requirement == "repository-inventory":
            return self._repository_knowledge.search_candidate_files(
                CandidateFileQuery(limit=REPOSITORY_INVENTORY_LIMIT)
            )
        if requirement == "candidate-files":
            return self._repository_knowledge.search_candidate_files(
                CandidateFileQuery(terms=terms, limit=CANDIDATE_FILE_LIMIT)
            )
        if requirement == "planning-evidence":
            raise AssertionError("planning-evidence is materialized from candidate files")
        if requirement == "documentation":
            return self._repository_knowledge.lookup_documentation(
                DocumentationQuery(terms=terms, limit=DOCUMENTATION_LIMIT)
            )
        if requirement == "dependency-manifests":
            return self._repository_knowledge.lookup_dependency_manifests(
                DependencyManifestQuery()
            )
        if requirement == "test-hints":
            return self._repository_knowledge.lookup_test_hints(TestHintQuery())
        raise AssertionError(f"unsupported repository requirement {requirement!r}")

    def _query_knowledge_base(
        self, knowledge_base: dict[str, Any], terms: tuple[str, ...]
    ) -> tuple[KnowledgeResult, ...]:
        results: dict[str, KnowledgeResult] = {}
        for source in knowledge_base["spec"]["sources"]:
            source_type = source.get("type")
            path = str(source.get("path", "")).rstrip("/")
            limit = source.get("limit", KNOWLEDGE_SOURCE_LIMIT)
            if source_type in {"docs", "adr", "runbook"}:
                selected = self._repository_knowledge.lookup_documentation(
                    DocumentationQuery(terms=terms, path_prefix=path, limit=limit)
                )
            elif source_type == "repository":
                selected = self._repository_knowledge.search_candidate_files(
                    CandidateFileQuery(terms=terms, path_prefix=path, limit=limit)
                )
            else:
                raise ContextInputValidationError(
                    f"unsupported KnowledgeBase source type {source_type!r}"
                )
            for result in selected:
                results[result.id] = result
        return tuple(results[key] for key in sorted(results))


def _resource_data(resource: Resource | JsonMapping, *, expected_kind: str) -> dict[str, Any]:
    value = resource.data if isinstance(resource, Resource) else resource
    if not isinstance(value, Mapping):
        raise ContextInputValidationError(f"{expected_kind} must be a Resource or mapping")
    copied = _json_copy(value)
    if copied.get("kind") != expected_kind:
        raise ContextInputValidationError(
            f"expected {expected_kind} Resource, found {copied.get('kind')!r}"
        )
    _validate_resource_schema(copied, expected_kind)
    metadata = copied.get("metadata")
    if not isinstance(metadata, dict) or not all(
        isinstance(metadata.get(field), str) and metadata[field]
        for field in ("name", "version")
    ):
        raise ContextInputValidationError(
            f"{expected_kind} metadata must include name and immutable version"
        )
    if metadata["version"] == "latest":
        raise ContextInputValidationError("floating resource versions are not allowed")
    return copied


def _validate_bound_resources(
    task: JsonMapping,
    field: str,
    supplied: Sequence[JsonMapping],
    *,
    expected_kind: str,
) -> None:
    declared_refs = tuple(
        sorted(
            (_ref_key(reference) for reference in task["spec"].get(field, ())),
        )
    )
    supplied_refs = tuple(sorted(_ref_key(_resource_ref(item)) for item in supplied))
    if len(supplied_refs) != len(set(supplied_refs)):
        raise ContextInputValidationError(
            f"supplied {expected_kind} Resources must have unique references"
        )
    if declared_refs == supplied_refs:
        return
    missing = sorted(set(declared_refs) - set(supplied_refs))
    extra = sorted(set(supplied_refs) - set(declared_refs))
    raise ContextInputValidationError(
        f"supplied {expected_kind} Resources do not match Task.spec.{field}; "
        f"missing={[_format_ref_key(ref) for ref in missing]!r}, "
        f"extra={[_format_ref_key(ref) for ref in extra]!r}"
    )


def _ref_key(reference: JsonMapping) -> tuple[str, str, str]:
    return (
        str(reference.get("kind", "")),
        str(reference.get("name", "")),
        str(reference.get("version", "")),
    )


def _format_ref_key(reference: tuple[str, str, str]) -> str:
    return f"{reference[0]}/{reference[1]}:{reference[2]}"


def _validate_inputs(
    *,
    task_data: dict[str, Any],
    task_execution: JsonMapping,
    workflow_execution: JsonMapping,
    event: JsonMapping | None,
    required_context: frozenset[str],
    prior_task_execution_ids: Sequence[str],
    token_budget: int,
    created_at: str,
) -> None:
    if not isinstance(task_execution, Mapping) or task_execution.get("kind") != "TaskExecution":
        raise ContextInputValidationError("task_execution must be a TaskExecution")
    if (
        not isinstance(workflow_execution, Mapping)
        or workflow_execution.get("kind") != "WorkflowExecution"
    ):
        raise ContextInputValidationError("workflow_execution must be a WorkflowExecution")
    if task_execution.get("taskRef") != _resource_ref(task_data):
        raise ContextInputValidationError("TaskExecution taskRef does not match Task")
    if task_execution.get("workflowExecutionId") != workflow_execution.get("id"):
        raise ContextInputValidationError(
            "TaskExecution does not belong to the supplied WorkflowExecution"
        )
    task_revision = task_execution.get("provenance", {}).get("repositoryRevision")
    workflow_revision = workflow_execution.get("repositoryRevision")
    if task_revision != workflow_revision:
        raise ContextInputValidationError(
            "TaskExecution and WorkflowExecution repository revisions do not match"
        )
    if task_execution.get("traceId") != workflow_execution.get("traceId"):
        raise ContextInputValidationError("execution traceIds do not match")
    _validate_event_binding(
        workflow_execution,
        event,
        require_issue="issue" in required_context,
    )
    _unique_nonempty(prior_task_execution_ids)
    if (
        not isinstance(token_budget, int)
        or isinstance(token_budget, bool)
        or not 1 <= token_budget <= MAX_INPUT_CONTEXT_TOKENS
    ):
        raise ContextInputValidationError(
            f"token_budget must be an integer from 1 through {MAX_INPUT_CONTEXT_TOKENS}"
        )
    if not isinstance(created_at, str) or not created_at:
        raise ContextInputValidationError("created_at must be a non-empty timestamp")


def _validate_event_binding(
    workflow_execution: JsonMapping,
    event: JsonMapping | None,
    *,
    require_issue: bool,
) -> None:
    event_id = workflow_execution.get("eventId")
    event_ref = workflow_execution.get("eventRef")
    if event_id is None:
        if event is not None:
            raise ContextInputValidationError(
                "event input is not bound by WorkflowExecution.eventId"
            )
        if event_ref is not None or require_issue:
            raise ContextInputValidationError(
                "WorkflowExecution.eventId is required for Event context"
            )
        return
    if not isinstance(event_id, str) or not event_id:
        raise ContextInputValidationError(
            "WorkflowExecution.eventId must be a non-empty string"
        )
    if event is None:
        raise ContextInputValidationError(
            "event is required for a WorkflowExecution with an eventId"
        )
    if not isinstance(event, Mapping):
        raise ContextInputValidationError("event must be a mapping")
    if event.get("id") != event_id:
        raise ContextInputValidationError(
            f"event id {event.get('id')!r} does not match "
            f"WorkflowExecution.eventId {event_id!r}"
        )
    if not isinstance(event_ref, Mapping):
        raise ContextInputValidationError(
            "WorkflowExecution.eventRef is required for Event context"
        )
    expected_ref = {
        "kind": "Event",
        "name": "github-issue-created",
        "version": "1.0.0",
    }
    if dict(event_ref) != expected_ref:
        raise ContextInputValidationError(
            f"unsupported Event reference {_json_copy(event_ref)!r}"
        )
    _validate_github_issue_event(event, require_issue=require_issue)


def _validate_github_issue_event(event: JsonMapping, *, require_issue: bool) -> None:
    expected_strings = {
        "source": "github",
        "type": "github.issue.created",
    }
    for field, expected in expected_strings.items():
        if event.get(field) != expected:
            raise ContextInputValidationError(
                f"normalized Event.{field} must be {expected!r}"
            )
    for field in ("receivedAt", "deduplicationKey"):
        if not isinstance(event.get(field), str) or not event[field]:
            raise ContextInputValidationError(
                f"normalized Event.{field} must be a non-empty string"
            )
    _validate_event_object(
        event,
        "repository",
        (("id", int), ("full_name", str)),
    )
    _validate_event_object(
        event,
        "sender",
        (("id", int), ("login", str)),
    )
    # The only MVP normalized Event type is issue-created, so its issue payload
    # is part of the Event contract even when the Task requests generic Event
    # context. ``require_issue`` makes the Task-specific intent explicit.
    if require_issue or event.get("type") == "github.issue.created":
        _validate_event_object(
            event,
            "issue",
            (("id", int), ("number", int), ("title", str)),
        )


def _validate_event_object(
    event: JsonMapping,
    field: str,
    required_fields: Sequence[tuple[str, type[Any]]],
) -> None:
    value = event.get(field)
    if not isinstance(value, Mapping):
        raise ContextInputValidationError(
            f"normalized Event.{field} must be an object"
        )
    for child, expected_type in required_fields:
        candidate = value.get(child)
        valid = isinstance(candidate, expected_type)
        if expected_type is int and isinstance(candidate, bool):
            valid = False
        if expected_type is str and not candidate:
            valid = False
        if not valid:
            raise ContextInputValidationError(
                f"normalized Event.{field}.{child} must be a non-empty "
                f"{expected_type.__name__}"
            )


def _validate_required_context(
    required: frozenset[str],
    resolved: set[str],
) -> None:
    missing = sorted(required - resolved)
    if missing:
        raise RequiredContextError(f"required context could not be resolved: {missing!r}")


def _context_names(values: Sequence[str], *, field: str) -> frozenset[str]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ContextInputValidationError(f"{field} must be a sequence of strings")
    normalized = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ContextInputValidationError(f"{field} must contain non-empty strings")
        name = value.strip().casefold()
        if name not in SUPPORTED_CONTEXT:
            raise ContextInputValidationError(
                f"{field} contains unsupported context requirement {value!r}"
            )
        normalized.append(name)
    if len(normalized) != len(set(normalized)):
        raise ContextInputValidationError(f"{field} must contain unique values")
    return frozenset(normalized)


def _knowledge_element(
    element_type: str, result: KnowledgeResult, *, ref: dict[str, str] | None = None
) -> dict[str, Any]:
    source = result.provenance.source
    resource_refs = [ref] if ref is not None else []
    return {
        "type": element_type,
        "content": {
            "id": result.id,
            "kind": result.kind.value,
            "score": result.score,
            "attributes": _json_copy(result.attributes),
            "source": {
                "path": source.path,
                **({"startLine": source.start_line} if source.start_line is not None else {}),
                **({"endLine": source.end_line} if source.end_line is not None else {}),
                **({"symbol": source.symbol} if source.symbol is not None else {}),
            },
            "retrieval": {
                "snapshotVersion": result.provenance.snapshot_version,
                "snapshotCreatedAt": result.provenance.snapshot_created_at,
                "snapshotProducer": result.provenance.snapshot_producer,
                "traversalPath": list(result.provenance.traversal_path),
            },
        },
        "provenance": {
            "actor": "repository-knowledge-query",
            "repositoryRevision": result.provenance.repository_revision,
            "knowledgeGraphVersion": result.provenance.snapshot_version,
            "resourceRefs": resource_refs,
        },
    }


def _deduplicate_context_candidates(
    mandatory: list[tuple[str, dict[str, Any]]],
    optional: list[tuple[str, dict[str, Any]]],
) -> tuple[list[tuple[str, dict[str, Any]]], list[tuple[str, dict[str, Any]]]]:
    """Merge duplicate source slices within each priority class."""

    selected_mandatory: list[tuple[str, dict[str, Any]]] = []
    selected_optional: list[tuple[str, dict[str, Any]]] = []
    mandatory_index: dict[tuple[Any, ...], int] = {}
    optional_index: dict[tuple[Any, ...], int] = {}

    for name, element in mandatory:
        prepared = _with_selection_reason(element, name)
        identity = _repository_identity(prepared)
        if identity is not None and identity in mandatory_index:
            index = mandatory_index[identity]
            prior_name, prior = selected_mandatory[index]
            selected_mandatory[index] = (prior_name, _merge_duplicate(prior, prepared))
            continue
        if identity is not None:
            mandatory_index[identity] = len(selected_mandatory)
        selected_mandatory.append((name, prepared))

    for name, element in optional:
        prepared = _with_selection_reason(element, name)
        identity = _repository_identity(prepared)
        if identity is not None and identity in optional_index:
            index = optional_index[identity]
            prior_name, prior = selected_optional[index]
            selected_optional[index] = (prior_name, _merge_duplicate(prior, prepared))
            continue
        if identity is not None:
            optional_index[identity] = len(selected_optional)
        selected_optional.append((name, prepared))
    return selected_mandatory, selected_optional


def _repository_identity(element: JsonMapping) -> tuple[Any, ...] | None:
    if element.get("type") not in {"repository", "knowledge"}:
        return None
    content = element.get("content")
    provenance = element.get("provenance")
    if not isinstance(content, Mapping) or not isinstance(provenance, Mapping):
        return None
    source = content.get("source")
    retrieval = content.get("retrieval")
    if not isinstance(source, Mapping) or not isinstance(retrieval, Mapping):
        return None
    return (
        provenance.get("repositoryRevision"),
        retrieval.get("snapshotVersion"),
        source.get("path"),
        source.get("startLine"),
        source.get("endLine"),
        source.get("symbol"),
    )


def _with_selection_reason(element: dict[str, Any], name: str) -> dict[str, Any]:
    value = deepcopy(element)
    content = value.get("content")
    if value.get("type") in {"repository", "knowledge"} and isinstance(content, dict):
        content["selectionReasons"] = [name]
        retrieval = content.get("retrieval")
        if isinstance(retrieval, dict) and isinstance(retrieval.get("traversalPath"), list):
            retrieval["selectionTraversalPaths"] = [retrieval["traversalPath"]]
    return value


def _merge_duplicate(
    surviving: dict[str, Any], duplicate: dict[str, Any]
) -> dict[str, Any]:
    merged = deepcopy(surviving)
    merged_content = merged["content"]
    duplicate_content = duplicate["content"]
    merged_content["selectionReasons"] = sorted(
        set(merged_content.get("selectionReasons", ()))
        | set(duplicate_content.get("selectionReasons", ()))
    )
    merged_retrieval = merged_content.get("retrieval", {})
    duplicate_retrieval = duplicate_content.get("retrieval", {})
    traversal_paths = {
        tuple(path)
        for path in (
            *merged_retrieval.get("selectionTraversalPaths", ()),
            *duplicate_retrieval.get("selectionTraversalPaths", ()),
        )
    }
    merged_retrieval["selectionTraversalPaths"] = [
        list(path) for path in sorted(traversal_paths)
    ]
    refs = {
        _ref_key(ref): _json_copy(ref)
        for ref in (
            *merged.get("provenance", {}).get("resourceRefs", ()),
            *duplicate.get("provenance", {}).get("resourceRefs", ()),
        )
    }
    merged["provenance"]["resourceRefs"] = [refs[key] for key in sorted(refs)]
    return merged


def _without_token_metadata(element: JsonMapping) -> dict[str, Any]:
    value = _json_copy(element)
    value.pop("tokenCount", None)
    value.pop("truncated", None)
    return value


def _selection_reasons(element: JsonMapping, fallback: str) -> list[str]:
    content = element.get("content")
    if isinstance(content, Mapping):
        reasons = content.get("selectionReasons")
        if isinstance(reasons, Sequence) and not isinstance(reasons, (str, bytes)):
            return sorted(str(reason) for reason in reasons)
    return [fallback]


def _token_breakdown(
    categories: Sequence[str], elements: Sequence[JsonMapping]
) -> dict[str, dict[str, int]]:
    breakdown: dict[str, dict[str, int]] = {}
    for category, element in zip(categories, elements, strict=True):
        entry = breakdown.setdefault(category, {"elementCount": 0, "tokenCount": 0})
        entry["elementCount"] += 1
        entry["tokenCount"] += int(element["tokenCount"])
    return {category: breakdown[category] for category in sorted(breakdown)}


def _require_result_binding(
    results: Sequence[KnowledgeResult],
    expected_revision: str,
    expected_snapshot_version: Any,
) -> None:
    for result in results:
        if result.provenance.repository_revision != expected_revision:
            raise ContextInputValidationError(
                f"repository knowledge result {result.id!r} belongs to revision "
                f"{result.provenance.repository_revision!r}, expected {expected_revision!r}"
            )
        if (
            expected_snapshot_version is not None
            and result.provenance.snapshot_version != expected_snapshot_version
        ):
            raise ContextInputValidationError(
                f"repository knowledge result {result.id!r} belongs to snapshot "
                f"{result.provenance.snapshot_version!r}, expected "
                f"{expected_snapshot_version!r}"
            )


def _validate_artifact_binding(
    metadata: JsonMapping,
    *,
    producer: JsonMapping,
    workflow_execution: JsonMapping,
) -> None:
    artifact_id = metadata.get("id")
    producer_id = producer.get("id")
    provenance = metadata.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ContextInputValidationError(
            f"artifact {artifact_id!r} has no valid provenance"
        )
    expected = {
        "taskExecutionId": producer_id,
        "workflowExecutionId": workflow_execution.get("id"),
        "repositoryRevision": workflow_execution.get("repositoryRevision"),
        "traceId": workflow_execution.get("traceId"),
    }
    actual = {
        "taskExecutionId": metadata.get("taskExecutionId"),
        "workflowExecutionId": provenance.get("workflowExecutionId"),
        "repositoryRevision": metadata.get("repositoryRevision"),
        "traceId": metadata.get("traceId"),
    }
    for field, expected_value in expected.items():
        if actual[field] != expected_value:
            raise ContextInputValidationError(
                f"artifact {artifact_id!r} has {field}={actual[field]!r}, "
                f"expected {expected_value!r}"
            )
    if provenance.get("taskExecutionId") != producer_id:
        raise ContextInputValidationError(
            f"artifact {artifact_id!r} provenance does not identify producer "
            f"{producer_id!r}"
        )
    if provenance.get("repositoryRevision") != expected["repositoryRevision"]:
        raise ContextInputValidationError(
            f"artifact {artifact_id!r} provenance has the wrong repository revision"
        )


def _resource_element(
    element_type: str, resource: dict[str, Any], ref: dict[str, str]
) -> dict[str, Any]:
    return {
        "type": element_type,
        "content": _json_copy(resource),
        "provenance": {"actor": "resource-loader", "resourceRefs": [ref]},
    }


def _with_token_metadata(element: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(element)
    value["tokenCount"] = max(1, ceil(len(_canonical_json(value)) / 4))
    value["truncated"] = False
    return value


def _search_terms(task: dict[str, Any], event: JsonMapping | None) -> tuple[str, ...]:
    text = str(task.get("spec", {}).get("objective", ""))
    if event is not None:
        issue = event.get("issue")
        if isinstance(issue, Mapping):
            text += " " + str(issue.get("title", ""))
            text += " " + str(issue.get("body", ""))
    ignored = {"and", "for", "from", "into", "that", "the", "this", "with"}
    return tuple(
        sorted(
            {
                word.casefold()
                for word in re.findall(r"[A-Za-z0-9_-]{3,}", text)
                if word.casefold() not in ignored
            }
        )
    )[:20]


def _resource_ref(resource: JsonMapping) -> dict[str, str]:
    return {
        "kind": resource["kind"],
        "name": resource["metadata"]["name"],
        "version": resource["metadata"]["version"],
    }


def _event_refs(workflow_execution: JsonMapping) -> list[dict[str, str]]:
    event_ref = workflow_execution.get("eventRef")
    return [_json_copy(event_ref)] if isinstance(event_ref, Mapping) else []


def _resource_sort_key(resource: JsonMapping) -> tuple[str, str, str]:
    return (
        str(resource["kind"]),
        str(resource["metadata"]["name"]),
        str(resource["metadata"]["version"]),
    )


def _decode_artifact(content: bytes, media_type: Any) -> Any:
    text = content.decode("utf-8")
    if media_type == "application/json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise ContextInputValidationError(
                "artifact declares application/json but contains invalid JSON"
            ) from error
    return text


def _unique_nonempty(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ContextInputValidationError(
            "prior_task_execution_ids must be a sequence of strings"
        )
    result = tuple(values)
    if any(not isinstance(value, str) or not value for value in result):
        raise ContextInputValidationError(
            "prior_task_execution_ids must contain non-empty strings"
        )
    if len(result) != len(set(result)):
        raise ContextInputValidationError(
            "prior_task_execution_ids must contain unique values"
        )
    return result


def _json_copy(value: Any) -> Any:
    try:
        mutable = _mutable_json(value)
        return json.loads(json.dumps(mutable, allow_nan=False, sort_keys=True))
    except (TypeError, ValueError) as error:
        raise ContextInputValidationError("context input must be JSON-compatible") from error


def _mutable_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return {key: _mutable_json(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mutable_json(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported JSON value {type(value).__name__}")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _validate_context_package(package: dict[str, Any]) -> None:
    errors = sorted(
        _context_package_validator().iter_errors(package),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        error = errors[0]
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        raise ContextPackageValidationError(
            f"invalid ContextPackage at {path}: {error.message}"
        )


def _validate_resource_schema(resource: dict[str, Any], kind: str) -> None:
    errors = sorted(
        _resource_validator(kind).iter_errors(resource),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if not errors:
        return
    error = errors[0]
    path = "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}"
        for part in error.absolute_path
    )
    raise ContextInputValidationError(
        f"invalid {kind} Resource at {path}: {error.message}"
    )


@cache
def _resource_validator(kind: str) -> Draft202012Validator:
    schema_root = Path(__file__).parents[2] / "schemas" / "resources" / "v1"
    schemas = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in schema_root.glob("*.schema.json")
    ]
    registry = Registry()
    by_name: dict[str, dict[str, Any]] = {}
    for schema in schemas:
        registry = registry.with_resource(
            schema["$id"],
            SchemaResource.from_contents(schema, default_specification=DRAFT202012),
        )
        by_name[schema["$id"].rsplit("/", 1)[-1]] = schema
    schema_name = f"{kind.lower()}.schema.json"
    try:
        schema = by_name[schema_name]
    except KeyError as error:
        raise ContextInputValidationError(
            f"no Resource schema is available for {kind}"
        ) from error
    return Draft202012Validator(schema, registry=registry)


@cache
def _context_package_validator() -> Draft202012Validator:
    schema_root = Path(__file__).parents[2] / "schemas"
    schema_paths = (
        schema_root / "resources" / "v1" / "resource-definitions.schema.json",
        schema_root / "runtime" / "v1" / "runtime-definitions.schema.json",
        schema_root / "runtime" / "v1" / "contextpackage.schema.json",
    )
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in schema_paths]
    registry = Registry()
    for schema in schemas:
        registry = registry.with_resource(
            schema["$id"],
            SchemaResource.from_contents(schema, default_specification=DRAFT202012),
        )
    return Draft202012Validator(
        schemas[-1], registry=registry, format_checker=FormatChecker()
    )
