---
name: review-aep-pr
description: Review pull requests and local diffs for the AI Agent Engineering Platform (AEP). Use when evaluating AEP code, schemas, fixtures, tests, task completion, documentation, or architecture compliance and when producing severity-ranked findings plus a score out of 10.
---

# Review AEP Pull Requests

Review changes for correctness, regressions, security, architectural compliance, and acceptance-criteria coverage. Report actionable findings before the score. Do not modify the pull request unless the user separately asks for fixes.

## Establish the Review Scope

1. Read `AGENTS.md` and `README.md`.
2. Inspect the complete diff and changed-file list. Determine the base branch or comparison range instead of reviewing isolated files.
3. Identify related `docs/tasks/AEP-*.md` files and read their dependencies and acceptance criteria.
4. Read only the relevant architecture and ADR documents under `docs/architecture/` and `docs/adr/`.
5. Inspect tests, schemas, and fixtures affected by the change. Run focused tests or validation when practical.
6. Check whether behavior, configuration, commands, public APIs, task status, or project structure changed without corresponding documentation updates.

If the diff or task context is unavailable, state the limitation and do not imply a complete review.

## Apply AEP Guardrails

Reject changes that violate these project rules:

- Keep declarative Resources separate from observed runtime objects.
- Never model `GeneratedArtifact` as a declarative Resource.
- Require explicit, immutable resource versions; reject floating `latest` references.
- Keep model providers in Model resources, not Tool resources.
- Prevent Agents from retrieving repository knowledge directly; use deterministic `ContextPackage` construction.
- Keep orchestration deterministic. Agents perform bounded cognitive work and do not choose workflow execution paths.
- Require policy checks for privileged Tool capabilities and publication.
- Preserve provenance, audit evidence, and terminal runtime-object immutability.
- Keep fixtures small and deterministic and cover task acceptance criteria with tests.

Treat task documents and applicable ADRs as the source of truth. Flag stale documentation when the implementation changes behavior or task status.

## Classify Findings

Report only concrete issues introduced or exposed by the reviewed change. Cite the narrowest useful file and line. Explain the failure scenario, impact, and a practical correction.

- **Critical**: Causes security or secret exposure, destructive or unauthorized actions, unrecoverable data/evidence corruption, a systemic architecture violation, or makes the primary workflow fundamentally unsafe or unusable. Deduct 3 points each.
- **Major**: Produces incorrect behavior in a supported path, violates an acceptance criterion or important AEP invariant, creates a meaningful regression, or lacks validation for consequential behavior. Deduct 1 point each.
- **Minor**: A localized maintainability, clarity, edge-case, test-quality, or documentation issue with limited operational impact. Deduct 0 points, but report it separately.

Do not inflate severity based on file size or style preference. Do not count the same root cause more than once. Suggestions that are not defects belong under `Notes`, not findings.

## Calculate the Score

Start at 10 and calculate:

`score = max(0, 10 - (3 * critical_count) - major_count)`

Minor findings do not affect the numeric score. A score is not a substitute for explaining findings. If review scope is materially incomplete, label the score `Provisional`.

## Output Format

Use this exact section order. Omit a severity subsection only when it has no findings.

```markdown
# PR Review

## Findings

### Critical

- [C1] Short title - `path/to/file.py:42`
  - Impact: Concrete failure or risk.
  - Evidence: Why the changed code causes it.
  - Recommendation: Smallest practical correction.

### Major

- [M1] Short title - `path/to/file.py:87`
  - Impact: Concrete failure or regression.
  - Evidence: Relevant code path or unmet criterion.
  - Recommendation: Smallest practical correction.

### Minor

- [m1] Short title - `path/to/file.py:103`
  - Impact: Limited consequence.
  - Recommendation: Focused improvement.

## Score

**7/10**

- Starting score: 10
- Critical: 0 x -3 = 0
- Major: 3 x -1 = -3
- Minor: 2 x 0 = 0

## Validation

- `python -m pytest tests/test_example.py`: passed
- Not run: reason, if applicable

## Notes

- Optional non-defect observations or scope limitations.
```

When no issues are found, write `No findings.` under `Findings`, still provide the score and validation, and note any residual risks or untested areas. Never claim the change is defect-free solely because tests pass.
