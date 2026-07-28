---
name: implement-aep-feature
description: Implement code, schemas, fixtures, tests, knowledge sources, and synchronized documentation for an AI Agent Engineering Platform (AEP) task or feature. Use when Codex must change this repository to satisfy an AEP task file, acceptance criteria, bug fix, runtime contract, resource contract, controller, context, repository-knowledge, model, Tool, evaluation, policy, or workflow requirement.
---

# Implement an AEP Feature

Implement the smallest complete change that satisfies the selected contract while preserving AEP's separation of declarative behavior, deterministic execution, bounded AI reasoning, and governed side effects.

## Establish the Contract

1. Read `AGENTS.md` and `README.md`.
2. Inspect the worktree and preserve unrelated user changes.
3. Identify the governing `docs/tasks/AEP-*.md` file. Read its dependencies, deliverable, acceptance criteria, and required tests.
4. Read only the relevant material in:
   - `docs/adr/`
   - `docs/architecture/`
   - `schemas/resources/` or `schemas/runtime/`
   - related fixtures, source modules, and tests
5. If no task file covers the feature, derive an explicit contract from the request and current architecture. Do not silently expand the product scope.
6. Translate the requirements into a short implementation checklist covering:
   - inputs and outputs
   - identity and versioning
   - lifecycle and failure semantics
   - provenance and persistence
   - policy boundaries
   - acceptance tests
   - knowledge-base impact
   - documentation impact

Resolve contradictions in this order: explicit user requirement, `AGENTS.md`, accepted ADRs, applicable task acceptance criteria, architecture documents, then existing implementation. Surface any unresolved contradiction before making a materially different architectural decision.

## Design Within AEP Boundaries

Apply these invariants before writing code:

- Keep declarative Resources separate from runtime objects.
- Store desired behavior in versioned Resources and observed execution in runtime objects.
- Require explicit immutable resource versions; reject floating references such as `latest`.
- Never model `GeneratedArtifact` as a Resource.
- Represent model providers with Model resources and the model-invocation boundary, never as Tools.
- Keep scheduling, branching, dependencies, retries, and lifecycle transitions deterministic and outside Agents.
- Keep Agents stateless and bounded to structured cognitive work.
- Prevent Agents from retrieving repository knowledge directly. Supply immutable, provenance-rich `ContextPackage` inputs.
- Treat terminal execution evidence as immutable. Prefer deterministic IDs, idempotent creation, and concurrency-safe transitions.
- Validate Tool inputs and outputs, check capability policy before execution, and normalize denial, timeout, and adapter failures.
- Evaluate correctness separately from policy. Apply publication policy before an external publication action.
- Record enough resource versions, repository revision, context, trace, and evidence to explain and reproduce an execution.

Do not introduce infrastructure or generality that the task does not require. Preserve public contracts that are intended to support richer future implementations.

## Implement Contract First

1. Add or update schemas and deterministic fixtures when the contract changes.
2. Add focused tests from the acceptance criteria, including negative and boundary cases.
3. Implement the narrowest production behavior that satisfies the tests and repository conventions.
4. Use provider-neutral interfaces at boundaries for models, Tools, storage, repository knowledge, and external systems.
5. Classify failures explicitly as recoverable, configuration, evaluation, policy, or permanent where the runtime contract requires it.
6. Cover idempotency, immutability, ordering, retries, and concurrency when state can be replayed or updated by multiple workers.
7. Avoid unrelated refactors. Keep fixtures small and deterministic.

Do not weaken a schema or invariant merely to make an implementation pass. Fix the implementation or document an intentional contract change.

## Synchronize Knowledge and Related Documents

Treat knowledge and documentation updates as part of the implementation, not as
optional follow-up work.

1. Search for every affected name, contract, behavior, command, status, and
   example across `README.md`, `AGENTS.md`, `docs/`, `schemas/`, `fixtures/`,
   and repository-local `.ai/` configuration when present.
2. Update all affected authoritative knowledge sources:
   - curated `KnowledgeBase` Resources under `.ai/knowledge/` or the configured
     knowledge directory
   - product requirements, architecture documents, and accepted ADRs
   - task files, dependency descriptions, and `docs/execution-plan.md`
   - schemas, fixtures, examples, contributor instructions, and public API
     documentation
3. Keep the kind of knowledge explicit:
   - Update curated knowledge when a maintained fact, rule, convention, or
     relationship changes.
   - Do not hand-edit a derived Repository Knowledge snapshot or graph. Rebuild
     it from the new repository revision through the scanner or compiler.
   - Update repository-knowledge scanner/query tests and fixtures when the
     implementation changes what derived knowledge must detect or expose.
4. Remove or revise stale statements rather than adding a second contradictory
   description.
5. Re-run targeted searches after editing to verify that obsolete names,
   statuses, commands, and examples no longer remain.

When no curated KnowledgeBase content exists or a change cannot affect it,
record that determination in the handoff. This does not waive updates to
related project documents.

## Validate and Complete

Run focused tests while iterating, then run the full local suite when practical:

```powershell
python -m pytest
```

Also inspect:

```powershell
git diff --check
git status --short
```

Verify documentation and knowledge synchronization in the same change whenever
behavior, configuration, commands, public APIs, structure, knowledge retrieval,
or task status changes:

- Update `README.md` for contributor-facing behavior or commands.
- Update the governing task file and `docs/execution-plan.md` only when every acceptance criterion is satisfied.
- Update architecture or ADR material only when the design contract changes.
- Keep KnowledgeBase Resources, schemas, fixtures, examples, and documentation consistent with the implementation.
- Validate changed-document links and task/status counts when applicable.

Do not mark a task complete while its knowledge sources or related documents
describe stale behavior.

## Hand Off for Independent Review

Provide:

- the task or requirement implemented
- the files and contracts changed
- the acceptance criteria covered
- exact validation commands and results
- assumptions, known limitations, and residual risks
- knowledge sources updated or regenerated, or why they were unaffected
- related documents updated and stale-reference searches performed

Do not claim completion when required work remains or validation is failing. Do not commit, push, publish, or open a pull request unless the user requests it.
