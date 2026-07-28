---
name: review-aep-pr
description: Independently review pull requests, patches, and local diffs for the AI Agent Engineering Platform (AEP). Use when evaluating another agent's implementation, AEP code, schemas, fixtures, tests, task completion, documentation, security, concurrency, or architecture compliance and when producing severity-ranked findings plus a score out of 10. This is a read-only review workflow unless the user separately requests fixes.
---

# Review AEP Code Changes

Review the implementation as independent evidence. Do not assume the author handoff, passing tests, or apparent intent proves correctness. Report actionable findings before the score and do not modify the change unless the user separately asks for fixes.

## Establish the Review Scope

1. Read `AGENTS.md` and `README.md`.
2. Inspect the complete changed-file list and diff against the correct base. For local work, include staged, unstaged, and relevant untracked files.
3. Identify and read the governing `docs/tasks/AEP-*.md` file, its dependencies, deliverable, acceptance criteria, and required tests.
4. Read only the relevant ADRs, architecture documents, schemas, fixtures, implementation modules, and tests.
5. Compare the diff with the implementer's handoff, if provided, but verify every claim from repository evidence.
6. Check whether behavior, configuration, commands, public APIs, schemas, fixtures, task status, project structure, or execution-plan status changed without matching documentation updates.
7. State any missing diff, task context, dependency, or environment limitation that prevents a complete review.

Do not modify files, resolve threads, commit, push, publish, or open a pull request during review unless the user separately authorizes it.

## Review in Risk Order

### Architecture and contracts

Verify that the change:

- preserves the separation between declarative Resources and observed runtime objects
- uses explicit immutable resource versions and rejects floating references such as `latest`
- never models `GeneratedArtifact` as a Resource
- represents model providers with Model resources rather than Tools
- prevents Agents from retrieving repository knowledge directly
- supplies deterministic, immutable, provenance-rich `ContextPackage` inputs
- keeps scheduling, branching, dependencies, retries, and lifecycle decisions outside model reasoning
- keeps Agents stateless and bounded to structured cognitive work
- preserves immutable terminal evidence, traceability, and required provenance
- uses the applicable JSON Schemas and provider-neutral subsystem interfaces

Treat applicable task documents and accepted ADRs as the source of truth. Flag an implementation that weakens a schema or invariant merely to make tests pass.

### Behavior and lifecycle

Trace success and failure paths from inputs to persisted evidence. Check:

- validation before mutation or external effects
- legal status transitions and explicit failure classification
- retries only for recoverable failures
- idempotency and deterministic identity
- concurrency races, atomic claims, and optimistic status checks
- stable ordering and deterministic outputs
- timeout, cancellation, approval, denial, malformed input, and adapter failure behavior
- partial failure without corrupting prior or terminal evidence
- exact resource versions, repository revision, context, and trace data needed for reproducibility

### Tools, policy, and security

Verify:

- least privilege and capability policy before privileged Tool execution
- Tool input and output validation
- normalized denial, timeout, validation, and adapter failures
- secret handling and isolation assumptions
- separation of technical evaluation from governance
- publication policy before pull request creation or another external publication action

Treat a governance bypass, unauthorized side effect, secret exposure, or irreversible evidence corruption as high risk.

### Tests and documentation

Map every acceptance criterion to implementation evidence and at least one meaningful test where appropriate. Look for tests that:

- mirror implementation details without proving behavior
- omit negative, boundary, retry, immutability, or concurrency paths
- rely on nondeterministic ordering, time, or external state
- fail to exercise schema validation or persistence boundaries
- pass while leaving an acceptance criterion unimplemented

Keep fixtures small and deterministic. A task may be marked `Completed` only when all acceptance criteria are satisfied. Require `README.md`, task files, architecture, ADRs, schemas, fixtures, and `docs/execution-plan.md` to remain synchronized with changed behavior.

## Validate Independently

Run the narrowest relevant checks first, then the full local suite when practical:

```powershell
python -m pytest
git diff --check
```

Run additional schema, fixture, or task-specific validation when the change requires it. Record commands exactly and distinguish passed, failed, and not run. Never treat passing tests as proof that no defect exists.

## Classify Findings

Report only concrete issues introduced or exposed by the reviewed change. Cite the narrowest useful file and line. Explain the failure scenario, impact, and smallest contract-preserving correction.

- **Critical:** Causes unauthorized or destructive action, secret exposure, unrecoverable evidence corruption, a systemic architecture violation, or makes the primary workflow fundamentally unsafe or unusable. Deduct 3 points each.
- **Major:** Breaks a supported path, violates an acceptance criterion or important AEP invariant, produces incorrect persisted state, creates a meaningful race or regression, or omits validation for consequential behavior. Deduct 1 point each.
- **Minor:** Creates a localized edge case, maintainability problem, weak test, misleading documentation, or limited contract inconsistency. Deduct 0 points.

Do not inflate severity for file size or style preferences. Do not count the same root cause more than once. Put optional improvements under `Notes`.

## Determine Verdict and Score

Use:

- **Changes required:** Any Critical or Major finding.
- **Accept with minor follow-up:** Only Minor findings remain.
- **Accept:** No findings.

Calculate:

`score = max(0, 10 - (3 * critical_count) - major_count)`

Minor findings do not affect the numeric score. If review scope is materially incomplete, label the score and verdict `Provisional`.

## Output Format

Use this exact section order and omit empty severity subsections:

```markdown
# AEP Code Review

## Verdict

Changes required

## Findings

### Critical

- [C1] Short title - `path/to/file.py:42`
  - Impact: Concrete failure or risk.
  - Evidence: Why the changed behavior causes it.
  - Recommendation: Smallest contract-preserving correction.

### Major

- [M1] Short title - `path/to/file.py:87`
  - Impact: Concrete failure or unmet criterion.
  - Evidence: Relevant execution path.
  - Recommendation: Focused correction.

### Minor

- [m1] Short title - `path/to/file.py:103`
  - Impact: Limited consequence.
  - Recommendation: Focused improvement.

## Acceptance Criteria

- Criterion: Met | Not met | Not verified - evidence

## Score

**7/10**

- Starting score: 10
- Critical: 0 x -3 = 0
- Major: 3 x -1 = -3
- Minor: 2 x 0 = 0

## Validation

- `python -m pytest tests/test_example.py`: passed
- Not run: reason, if applicable

## Documentation

- Synchronized, or list required updates.

## Notes

- Scope limitations, residual risks, and non-defect suggestions.
```

If there are no findings, write `No findings.` under `Findings` and still report acceptance-criteria coverage, score, validation, documentation status, and residual risks.
