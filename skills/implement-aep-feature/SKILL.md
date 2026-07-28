---
name: implement-aep-feature
description: Implement code, schemas, fixtures, tests, and synchronized documentation for an AI Agent Engineering Platform (AEP) task or feature. Use when Codex must change this repository to satisfy an AEP task file, acceptance criteria, bug fix, runtime contract, resource contract, controller, context, repository-knowledge, model, Tool, evaluation, policy, or workflow requirement.
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

## Validate and Synchronize

Run focused tests while iterating, then run the full local suite when practical:

```powershell
python -m pytest
```

Also inspect:

```powershell
git diff --check
git status --short
```

Update documentation in the same change when behavior, configuration, commands, public APIs, structure, or task status changes:

- Update `README.md` for contributor-facing behavior or commands.
- Update the governing task file and `docs/execution-plan.md` only when every acceptance criterion is satisfied.
- Update architecture or ADR material only when the design contract changes.
- Keep schemas, fixtures, examples, and documentation consistent with the implementation.

If no documentation update is necessary, state why in the handoff.

## Hand Off for Independent Review

Provide:

- the task or requirement implemented
- the files and contracts changed
- the acceptance criteria covered
- exact validation commands and results
- assumptions, known limitations, and residual risks
- documentation updated, or why none was needed

Do not claim completion when required work remains or validation is failing. Do not commit, push, publish, or open a pull request unless the user requests it.
