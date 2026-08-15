"""AnalyzeIssue Task handler composed from existing AEP runtime boundaries."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from hashlib import sha256
from typing import Any

from aep.agent_invocation import AgentInvocationContractError, invoke_agent
from aep.agent_resolver import AgentResolutionError, AgentToolDeniedError, resolve_agent
from aep.context_builder import ContextBuilder, ContextBuilderError
from aep.generated_artifact_store import GeneratedArtifactStore
from aep.model_invocation import ModelAdapter, ModelConfiguration
from aep.resource_loader import Resource, ResourceCollection, ResourceRef
from aep.runtime_store import RuntimeObject, RuntimeObjectStore
from aep.runtime_validation import is_rfc3339_timestamp
from aep.schema_evaluation import SchemaEvaluationContractError, evaluate_schema
from aep.task_execution import FailureClass
from aep.workflow_scheduler import TaskExecutionResult


JsonMapping = Mapping[str, Any]
EventResolver = Callable[[str], JsonMapping | None]
Clock = Callable[[], str]


class AnalyzeIssueContractError(ValueError):
    """Raised when AnalyzeIssue is wired to conflicting immutable inputs."""


class AnalyzeIssueTaskHandler:
    """Execute one already-running AnalyzeIssue TaskExecution.

    The scheduler remains the owner of terminal lifecycle transitions. This
    handler attaches produced runtime evidence while the attempt is RUNNING and
    returns a classified result for the scheduler to apply.
    """

    task_name = "analyze-issue"
    task_label = "AnalyzeIssue"
    invocation_label = "Issue Analyzer"
    artifact_type = "ISSUE_ANALYSIS"
    artifact_actor = "analyze-issue-task-handler"
    runtime_id_namespace = "analyze-issue"

    def __init__(
        self,
        *,
        resources: ResourceCollection,
        runtime_store: RuntimeObjectStore,
        context_builder: ContextBuilder,
        artifact_store: GeneratedArtifactStore,
        model_adapter: ModelAdapter,
        event_resolver: EventResolver,
        clock: Clock,
    ) -> None:
        if not isinstance(resources, ResourceCollection):
            raise TypeError("resources must be a ResourceCollection")
        if not isinstance(context_builder, ContextBuilder):
            raise TypeError("context_builder must be a ContextBuilder")
        if not isinstance(artifact_store, GeneratedArtifactStore):
            raise TypeError("artifact_store must implement GeneratedArtifactStore")
        if not isinstance(model_adapter, ModelAdapter):
            raise TypeError("model_adapter must implement ModelAdapter")
        if not callable(event_resolver) or not callable(clock):
            raise TypeError("event_resolver and clock must be callable")
        self._resources = resources
        self._runtime_store = runtime_store
        self._context_builder = context_builder
        self._artifact_store = artifact_store
        self._model_adapter = model_adapter
        self._event_resolver = event_resolver
        self._clock = clock

    def execute(
        self, task: Resource, task_execution: RuntimeObject
    ) -> TaskExecutionResult:
        """Coordinate context, reasoning, evaluation, and artifact publication."""

        try:
            workflow, event = self._validate_inputs(task, task_execution)
            task_spec = _spec(task)
            evaluation = self._schema_evaluation(task_spec)
            task_output_schema = self._validate_task_evaluation_contract(
                task_spec, evaluation
            )
            context_package = self._context_builder.build(
                task=task,
                task_execution=task_execution,
                workflow_execution=workflow,
                event=event,
                knowledge_bases=self._resolve_declared(
                    task_spec.get("knowledgeBases", ()), "KnowledgeBase"
                ),
                policies=self._resolve_declared(task_spec.get("policies", ()), "Policy"),
                **self._context_arguments(task_execution, workflow),
                created_at=self._timestamp(),
            )
            self._attach(task_execution["id"], {"contextPackageId": context_package["id"]})

            task_ref = task.ref
            agent_ref = _required_ref(task_spec.get("agentRef"), "Agent", "Task.spec.agentRef")
            resolved_agent = resolve_agent(
                task_ref,
                agent_ref,
                self._resources,
                correlation=_correlation(task_execution),
                resolved_at=self._timestamp(),
            )
            resolved_record = resolved_agent.as_dict()
            saved_agent = self._runtime_store.create(
                resolved_record,
                deterministic_key=f"resolved-agent:{task_execution['id']}",
            )
            if dict(saved_agent) != resolved_record:
                raise AnalyzeIssueContractError(
                    "TaskExecution already has a different ResolvedAgent"
                )
            self._attach(task_execution["id"], {"resolvedAgentId": saved_agent["id"]})
            agent_output_schema = saved_agent.get("outputSchema")
            if (
                not isinstance(agent_output_schema, Mapping)
                or dict(agent_output_schema) != dict(task_output_schema)
            ):
                raise AnalyzeIssueContractError(
                    f"{self.task_label} Agent outputSchema must match Task.spec.outputs"
                )

            prompt = self._require_resource(
                ResourceRef.from_mapping(dict(saved_agent["promptRef"])), "Prompt"
            )
            model = self._require_resource(
                ResourceRef.from_mapping(dict(saved_agent["modelRef"])), "Model"
            )
            model_configuration = _model_configuration(model)
            invocation_id = self._runtime_id("agentinvocation", str(task_execution["id"]))
            model_invocation_id = self._runtime_id(
                "modelinvocation", str(task_execution["id"])
            )
            started_at = self._timestamp()
            invocation = invoke_agent(
                store=self._runtime_store,
                invocation_id=invocation_id,
                model_invocation_id=model_invocation_id,
                resolved_agent=saved_agent,
                context_package=context_package,
                prompt=prompt.data,
                model_configuration=model_configuration,
                adapter=self._model_adapter,
                started_at=started_at,
                completed_at=self._timestamp(),
            )
            self._attach(
                task_execution["id"],
                {"agentInvocationIds": [invocation_id]},
            )
            if invocation["status"] != "SUCCEEDED":
                failure = invocation.get("failure", {})
                if failure.get("class") == FailureClass.EVALUATION.value:
                    self._run_schema_evaluation(
                        task_execution=task_execution,
                        workflow=workflow,
                        evaluation=evaluation,
                        invocation_id=invocation_id,
                        content=invocation.get("output"),
                    )
                return TaskExecutionResult.failure(
                    _failure_class(failure.get("class")),
                    str(failure.get("message") or f"{self.invocation_label} invocation failed"),
                    retry_not_before=failure.get("retryNotBefore"),
                )

            artifact_id = self._runtime_id("generatedartifact", str(task_execution["id"]))
            evaluation_result = self._run_schema_evaluation(
                task_execution=task_execution,
                workflow=workflow,
                evaluation=evaluation,
                invocation_id=invocation_id,
                content=invocation["output"],
            )
            evaluation_id = str(evaluation_result["id"])
            if evaluation_result["outcome"] != "PASS":
                details = "; ".join(evaluation_result.get("logs", ()))
                return TaskExecutionResult.failure(
                    FailureClass.EVALUATION,
                    f"{self.task_label} output failed schema Evaluation: {details}",
                )

            artifact = self._artifact_store.publish(
                {
                    "apiVersion": "aep.dev/v1alpha1",
                    "kind": "GeneratedArtifact",
                    "id": artifact_id,
                    "traceId": task_execution["traceId"],
                    "createdAt": self._timestamp(),
                    "updatedAt": self._timestamp(),
                    "provenance": {
                        "actor": self.artifact_actor,
                        "workflowExecutionId": workflow["id"],
                        "taskExecutionId": task_execution["id"],
                        "repositoryRevision": workflow["repositoryRevision"],
                        "resourceRefs": _artifact_resource_refs(
                            saved_agent, _ref_record(evaluation.ref)
                        ),
                    },
                    "taskExecutionId": task_execution["id"],
                    "artifactType": self.artifact_type,
                    "repositoryRevision": workflow["repositoryRevision"],
                    "mediaType": "application/json",
                    "evaluationResultIds": [evaluation_id],
                },
                invocation["output"],
            )
            self._attach(
                task_execution["id"],
                {"generatedArtifactIds": [artifact["id"]]},
            )
            return TaskExecutionResult.success()
        except AgentToolDeniedError as error:
            return TaskExecutionResult.failure(FailureClass.POLICY, str(error))
        except (
            ContextBuilderError,
            AgentResolutionError,
            AgentInvocationContractError,
            SchemaEvaluationContractError,
            AnalyzeIssueContractError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            return TaskExecutionResult.failure(FailureClass.CONFIGURATION, str(error))

    def _validate_inputs(
        self, task: Resource, task_execution: RuntimeObject
    ) -> tuple[RuntimeObject, JsonMapping | None]:
        if not isinstance(task, Resource) or task.kind != "Task":
            raise AnalyzeIssueContractError("task must be a loaded Task Resource")
        if task.name != self.task_name:
            raise AnalyzeIssueContractError(
                f"{self.task_label} handler requires Task {self.task_name}"
            )
        if dict(task_execution.get("taskRef", {})) != _ref_record(task.ref):
            raise AnalyzeIssueContractError("TaskExecution.taskRef does not match Task")
        if (
            task_execution.get("kind") != "TaskExecution"
            or task_execution.get("status") != "RUNNING"
        ):
            raise AnalyzeIssueContractError("TaskExecution must be RUNNING")
        workflow_id = task_execution.get("workflowExecutionId")
        workflow = self._runtime_store.get(str(workflow_id))
        if workflow is None or workflow.get("kind") != "WorkflowExecution":
            raise AnalyzeIssueContractError(
                f"WorkflowExecution {workflow_id!r} was not found"
            )
        event_id = workflow.get("eventId")
        event = self._event_resolver(str(event_id)) if isinstance(event_id, str) else None
        return workflow, event

    def _context_arguments(
        self, task_execution: JsonMapping, workflow: JsonMapping
    ) -> dict[str, object]:
        """Return handler-specific Context Builder inputs."""

        return {}

    def _resolve_declared(
        self, values: Sequence[JsonMapping], expected_kind: str
    ) -> tuple[Resource, ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise AnalyzeIssueContractError(
                f"Task.spec {expected_kind} references must be an array"
            )
        return tuple(
            self._require_resource(
                _required_ref(value, expected_kind, f"Task.spec.{expected_kind}[{index}]"),
                expected_kind,
            )
            for index, value in enumerate(values)
        )

    def _require_resource(self, ref: ResourceRef, expected_kind: str) -> Resource:
        if ref.kind != expected_kind or ref.version == "latest":
            raise AnalyzeIssueContractError(
                f"expected explicit {expected_kind} reference, found {_ref_record(ref)!r}"
            )
        resource = self._resources.get(ref)
        if resource is None:
            raise AnalyzeIssueContractError(f"missing Resource {_ref_record(ref)!r}")
        return resource

    def _schema_evaluation(self, task_spec: JsonMapping) -> Resource:
        evaluations = self._resolve_declared(
            task_spec.get("evaluations", ()), "Evaluation"
        )
        schema_evaluations = tuple(
            item for item in evaluations if _spec(item).get("type") == "schema"
        )
        if len(schema_evaluations) != 1:
            raise AnalyzeIssueContractError(
                f"{self.task_label} Task must declare exactly one schema Evaluation"
            )
        return schema_evaluations[0]

    def _validate_task_evaluation_contract(
        self, task_spec: JsonMapping, evaluation: Resource
    ) -> JsonMapping:
        task_schema = task_spec.get("outputs")
        if not isinstance(task_schema, Mapping) or not task_schema:
            raise AnalyzeIssueContractError(
                f"{self.task_label} Task requires spec.outputs"
            )
        evaluation_schema = _evaluation_schema(evaluation)
        if dict(task_schema) != dict(evaluation_schema):
            raise AnalyzeIssueContractError(
                f"{self.task_label} Evaluation inputSchema must match Task.spec.outputs"
            )
        return task_schema

    def _run_schema_evaluation(
        self,
        *,
        task_execution: JsonMapping,
        workflow: JsonMapping,
        evaluation: Resource,
        invocation_id: str,
        content: Any,
    ) -> RuntimeObject:
        evaluation_ref = _ref_record(evaluation.ref)
        evaluation_id = self._runtime_id(
            "evaluationresult", str(task_execution["id"])
        )
        result = evaluate_schema(
            store=self._runtime_store,
            result_id=evaluation_id,
            task_execution_id=str(task_execution["id"]),
            evaluation_ref=evaluation_ref,
            target={"type": "AgentInvocation", "id": invocation_id},
            content=content,
            schema=_evaluation_schema(evaluation),
            correlation=_correlation(task_execution),
            timestamp=self._timestamp(),
            provenance={
                "actor": "schema-evaluator",
                "workflowExecutionId": workflow["id"],
                "taskExecutionId": task_execution["id"],
                "repositoryRevision": workflow["repositoryRevision"],
                "resourceRefs": [evaluation_ref],
            },
        )
        self._attach(
            task_execution["id"],
            {"evaluationResultIds": [evaluation_id]},
        )
        return result

    def _attach(self, task_execution_id: object, changes: JsonMapping) -> None:
        execution_id = str(task_execution_id)
        current = self._runtime_store.get(execution_id)
        if current is None or current.get("status") != "RUNNING":
            raise AnalyzeIssueContractError("TaskExecution is no longer RUNNING")
        merged: dict[str, Any] = {}
        for field, value in changes.items():
            prior = current.get(field)
            if field in {"contextPackageId", "resolvedAgentId"} and prior is not None:
                if prior != value:
                    raise AnalyzeIssueContractError(f"{field} is already bound")
                continue
            if isinstance(value, list):
                merged[field] = list(dict.fromkeys([*(prior or ()), *value]))
            else:
                merged[field] = deepcopy(value)
        if merged:
            self._runtime_store.update_status(
                execution_id,
                "RUNNING",
                expected_status="RUNNING",
                updated_at=self._timestamp(),
                changes=merged,
            )

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, str) or not is_rfc3339_timestamp(value):
            raise AnalyzeIssueContractError("clock must return an RFC3339 timestamp")
        return value

    def _runtime_id(self, prefix: str, task_execution_id: str) -> str:
        digest = sha256(
            f"{self.runtime_id_namespace}:{task_execution_id}:{prefix}".encode()
        ).hexdigest()[:24]
        return f"{prefix}-{digest}"


def _required_ref(value: Any, expected_kind: str, field: str) -> ResourceRef:
    if not isinstance(value, Mapping):
        raise AnalyzeIssueContractError(f"{field} must be an explicit Resource reference")
    try:
        ref = ResourceRef.from_mapping(dict(value))
    except KeyError as error:
        raise AnalyzeIssueContractError(
            f"{field} must include kind, name, and version"
        ) from error
    if ref.kind != expected_kind or not ref.name or not ref.version or ref.version == "latest":
        raise AnalyzeIssueContractError(f"{field} must reference versioned {expected_kind}")
    return ref


def _spec(resource: Resource) -> JsonMapping:
    value = resource.data.get("spec")
    if not isinstance(value, Mapping):
        raise AnalyzeIssueContractError(f"{resource.kind}.spec must be an object")
    return value


def _model_configuration(model: Resource) -> ModelConfiguration:
    spec = _spec(model)
    return ModelConfiguration(
        model_ref=_ref_record(model.ref),
        provider=str(spec.get("provider", "")),
        model=str(spec.get("model", "")),
        parameters=dict(spec.get("parameters", {})),
        token_limit=spec.get("tokenLimit"),
        timeout_ms=spec.get("timeoutMs"),
        retry_policy=dict(spec.get("retryPolicy", {})),
        rate_limit_policy=dict(spec.get("rateLimitPolicy", {})),
    )


def _evaluation_schema(evaluation: Resource) -> JsonMapping:
    schema = _spec(evaluation).get("inputSchema")
    if not isinstance(schema, Mapping) or not schema:
        raise AnalyzeIssueContractError("schema Evaluation requires spec.inputSchema")
    return schema


def _failure_class(value: object) -> FailureClass:
    try:
        return FailureClass(str(value))
    except ValueError:
        return FailureClass.PERMANENT


def _correlation(task_execution: JsonMapping) -> dict[str, str]:
    return {
        "traceId": str(task_execution["traceId"]),
        "workflowExecutionId": str(task_execution["workflowExecutionId"]),
        "taskExecutionId": str(task_execution["id"]),
    }


def _ref_record(ref: ResourceRef) -> dict[str, str]:
    return {"kind": ref.kind, "name": ref.name, "version": ref.version}


def _artifact_resource_refs(
    resolved_agent: JsonMapping, evaluation_ref: JsonMapping
) -> list[dict[str, str]]:
    values: dict[tuple[str, str, str], dict[str, str]] = {}
    provenance = resolved_agent.get("provenance", {})
    refs = provenance.get("resourceRefs", ()) if isinstance(provenance, Mapping) else ()
    for candidate in (*refs, evaluation_ref):
        if not isinstance(candidate, Mapping):
            continue
        key = tuple(str(candidate.get(field, "")) for field in ("kind", "name", "version"))
        if all(key):
            values[key] = {"kind": key[0], "name": key[1], "version": key[2]}
    return [values[key] for key in sorted(values)]
