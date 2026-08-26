# AEP-050: Normalize Publication Repository Identity

**Status:** In Progress

## Context

The Git Tool and GitHub Tool use two intentional representations of the same
repository at different boundaries. Execution checkout and Git evidence use the
provider-qualified canonical identity `github:<owner>/<name>`. GitHub API
requests use GitHub's native `<owner>/<name>` repository name.

`CreatePullRequest` correctly carries both representations through the live
publication path:

* the checkout-bound `GitToolAdapter` receives
  `ExecutionCheckoutBinding.repository.canonical`; and
* the GitHub `createPullRequest` request receives the Workspace repository as
  `<owner>/<name>`.

The trusted GitHub publication verifier currently compares these values as raw
strings. It first requires the persisted `commit_changes` ToolInvocation output
repository to equal the GitHub request repository. It later repeats the same
assumption for the persisted `push_branch` output. A production-shaped
execution therefore fails closed even when the identities refer to the same
repository and every preceding publication check passed.

The controlled MTP-09/MTP-10 run for issue #67 exposed the defect at revision
`ad18c80cb71db75d7ac8dec3ebea0a161c9d52cf`:

```text
WorkflowExecution:  workflowexecution-17274f5c-6582-527f-85e3-7af9759c92aa
Trace:              trace-268e6e3e-f85b-5583-a26d-57976e93d6cf
CreatePullRequest:  taskexecution-9e78e92a-7f52-5620-872e-ef6020f66fe0
Execution branch:   aep/execution/9a595c55b1a732cf0f37
Committed head:     8781ce9fc6d33f21e804b7b9716f0d9a8e6b4268
Commit invocation:  toolinvocation-a0be85932b66e21ee99249a2
Push invocation:    toolinvocation-143df477ec0cbd04390b306d
GitHub invocation:  toolinvocation-15cfbd9ce24c4e32ac16ee10
Failure:            Git commit evidence mismatch
```

AnalyzeIssue, BuildImplementationPlan, GeneratePatch, RunValidation, and
EvaluateAcceptance succeeded. Publication Policy returned `ALLOW`; the
separate `git.push` capability decision returned `ALLOW`; `commit_changes`
succeeded; and the authenticated push recorded
`remoteMutationState: CONFIRMED` at the expected head. The GitHub Tool then
rejected publication before calling GitHub because it observed:

```text
commit output repository: github:yijiazho/agent-engineering-platform
GitHub request repository: yijiazho/agent-engineering-platform
```

All other commit-evidence predicates matched. After that first predicate is
corrected, the existing raw comparison in expected push output would reject
the same canonical value as `Git push target mismatch`, so both checks belong
to the same defect.

Provider reconciliation confirms that branch
`aep/execution/9a595c55b1a732cf0f37` exists on GitHub at exactly
`8781ce9fc6d33f21e804b7b9716f0d9a8e6b4268` and no pull request exists for the
branch. The terminal WorkflowExecution and ToolInvocations are immutable. The
confirmed branch is retained publication evidence and must not be silently
deleted, overwritten, or treated as an unattempted mutation.

Unit coverage missed the defect because `tests/test_github_tool.py` constructs
both Git Tool evidence and GitHub requests with the same synthetic bare
identity `acme/widgets`. The deterministic harness likewise configures its Git
adapter with an unqualified repository ID. Those fixtures do not reproduce the
checkout-bound production contract.

This is an identity-normalization defect, not grounds to weaken publication
verification, remove provider qualification from durable Git evidence, accept
arbitrary repository aliases, or invoke GitHub after an unresolved mismatch.

## Reproduction

Add a credential-free production-shaped regression around the trusted GitHub
publication verifier:

1. Construct a `RepositoryIdentity("github", "acme", "widgets")` and use its
   canonical value, `github:acme/widgets`, in successful persisted
   `commit_changes` and `push_branch` ToolInvocation outputs.
2. Construct the authorized GitHub `createPullRequest` request with repository
   `acme/widgets`, the same branch, base revision, committed head, correlated
   workflow/task/trace identifiers, passing evaluation and artifact evidence,
   an allowed Publication Policy decision, and an allowed Git push capability
   decision.
3. Invoke the real publication verifier through the GitHub Tool boundary with
   a fake GitHub provider.
4. Before the fix, require the request to fail before the provider call with
   `Git commit evidence mismatch` even though every non-repository commit
   predicate matches.
5. In a focused variant that advances beyond commit verification, demonstrate
   that the raw push-output comparison would fail with
   `Git push target mismatch` for the same two equivalent forms.

Add negative cases in which provider, owner, or repository name actually
differs. These must continue to fail before any GitHub provider call. Case
handling must follow the repository identity contract already enforced at the
checkout and GitHub App boundaries; tests must not create a second, conflicting
normalization rule only for publication.

Add an integration regression through checkout-bound orchestration and
`CreatePullRequest` using the repository's real Task and Policy Resources,
real Git Tool output shapes, and fake external providers. Do not replace the
canonical checkout identity with a bare test-only repository ID. The test must
exercise commit verification, push verification, and GitHub PR creation in one
path.

Use the live identifiers above only as safe reference metadata. Do not replay,
rewrite, or retry the terminal issue #67 execution. Reconcile the retained
branch by exact repository, branch, and head before any operator cleanup or
recovery action.

## Deliverable

Implement one provider-aware repository identity contract across checkout,
Git, GitHub publication verification, tests, and operator guidance that:

* preserves `github:<owner>/<name>` as the canonical checkout and durable Git
  evidence identity;
* preserves `<owner>/<name>` as the GitHub API request repository where that is
  the provider adapter's declared input contract;
* converts both forms to one structured `RepositoryIdentity` or otherwise
  compares them through one authoritative normalization function before
  checking commit and push evidence;
* rejects missing providers, unsupported providers, malformed names, provider
  mismatch, owner mismatch, and repository mismatch before any GitHub call;
* updates both the `commit_changes` evidence check and the `push_branch` target
  check so fixing the first comparison cannot merely expose the second;
* leaves branch, base revision, committed head, patch digest, mutation state,
  Tool version, provenance, PolicyDecision, artifact, EvaluationResult, and
  correlation checks unchanged and fail closed;
* does not strip an arbitrary prefix, compare only the trailing path, weaken
  case or syntax rules, or allow an event/request repository to override the
  checkout-bound repository identity;
* adds production-shaped unit and integration regressions using canonical Git
  evidence and native GitHub request identities, plus negative identity cases;
* updates existing synthetic harness fixtures so they cannot pass by using a
  representation that production never emits;
* documents safe diagnosis and reconciliation for the case where push is
  confirmed but the later GitHub Tool rejects local publication evidence; and
* publishes and verifies a corrected immutable service generation before a
  new controlled live issue is used for MTP-09/MTP-10.

The implementation should reuse the repository identity type and parsing rules
already owned by execution checkout. If those rules cannot be reused without a
dependency cycle, extract a shared identity boundary rather than duplicating
provider-specific string manipulation in the GitHub verifier.

## Dependencies

* AEP-022
* AEP-024
* AEP-034
* AEP-039
* AEP-049

## Acceptance Criteria

* One documented repository identity contract distinguishes canonical
  provider-qualified identity from provider-native API coordinates and names
  the authoritative conversion/comparison boundary.
* A successful persisted Git commit with repository `github:acme/widgets`
  verifies against a GitHub PR request for `acme/widgets` when provider, owner,
  repository, workflow, task, trace, branch, revisions, patch digest, Tool
  version, provenance, mutation state, evaluations, artifacts, and policy
  evidence all match.
* A successful persisted Git push with repository `github:acme/widgets`
  verifies against that same request and retains the requirement for
  `remoteMutationState: CONFIRMED` at the exact committed head.
* Parameterized tests prove that a different provider, owner, repository,
  branch, base revision, head revision, patch digest, mutation state, Tool
  version, workflow, task, trace, provenance, or required policy/evidence
  identity is rejected before the GitHub provider is called.
* Malformed or unsupported repository identities fail closed with stable safe
  diagnostics that do not expose credentials, webhook payloads, artifact
  bodies, provider response bodies, or unrestricted command output.
* The GitHub Tool does not call its provider when repository normalization or
  any publication-evidence check fails. Existing ambiguous GitHub mutation and
  idempotency behavior remains unchanged.
* A checkout-bound CreatePullRequest integration test uses
  `RepositoryIdentity.canonical` for real Git Tool evidence and `<owner>/<name>`
  for the fake GitHub provider request, passes Publication Policy,
  `git.push`, and `github.create_pr` capability gates in order, confirms the
  pushed head, and creates exactly one fake pull request.
* The deterministic MVP harness and GitHub Tool fixtures no longer rely on a
  bare Git repository ID that differs from production. A regression fails if
  raw equality between canonical Git evidence and GitHub API coordinates is
  reintroduced in either commit or push verification.
* Regression coverage proves the issue #67 state shape is classified as a
  confirmed push followed by a local, pre-provider policy failure—not as
  `NOT_ATTEMPTED`, `UNKNOWN`, or a failed remote push.
* Operator documentation requires exact branch/head reconciliation after a
  confirmed push and local pre-PR failure. It forbids automatic push replay,
  branch overwrite/deletion, or PR creation from mismatched evidence.
* `tests/test_github_tool.py`, `tests/test_create_pull_request.py`,
  `tests/test_execution_checkout.py`, and `tests/test_mvp_harness.py` cover the
  corrected boundary. The full local suite and
  `python deploy/validation/verify.py verify` exact-image gate pass.
* After publishing the corrected immutable generation, one new controlled
  MTP-09/MTP-10 issue executes all six Tasks, records successful commit and
  confirmed push evidence using the canonical repository identity, authorizes
  the GitHub mutation, and creates exactly one open, unmerged pull request.
  Redelivery creates no duplicate Event, WorkflowExecution, branch mutation,
  or PR.
* The retained issue #67 WorkflowExecution remains terminal and unchanged.
  Its remote branch/head and absence of a PR remain recorded in the operator
  evidence until an explicitly authorized cleanup or recovery decision.
* `README.md`, execution-checkout and Tool architecture, GitHub App operations,
  the self-hosting runbook, tests, this task, and `docs/execution-plan.md`
  describe the same repository identity representations, verification rules,
  fail-closed behavior, and live verification status.
