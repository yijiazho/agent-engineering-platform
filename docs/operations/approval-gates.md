# Approval Gate Operations

**Audience:** Operators resolving `REQUIRE_APPROVAL` Policy decisions.

This runbook explains how to inspect a pending approval, verify the exact
evidence being approved, record an authorized decision, and understand when an
approval stops being valid.

## 1. When An Approval Is Required

An approval is required only when an evaluated capability or publication Policy
returns `REQUIRE_APPROVAL` for a governed action. The shipped self-hosting
`issue-to-pr` bundle does not require approval by default.

An `approval` Policy declares who may approve; it does not itself trigger the
gate. A deployment enables the gate with two additions to
`Task/create-pull-request` `spec.policies`:

```yaml
# 1. A capability rule that requires approval for the gated action.
apiVersion: aep.dev/v1alpha1
kind: Policy
metadata:
  name: require-publication-approval
  version: 1.0.0
spec:
  type: pre-execution-capability
  rules:
    - effect: require-approval
      capabilities:
        - github.create_pr
      reason: "Publication requires an artifact-bound human decision."
---
# 2. An approval Policy that declares the authorized reviewers.
apiVersion: aep.dev/v1alpha1
kind: Policy
metadata:
  name: publication-review
  version: 1.0.0
spec:
  type: approval
  approval:
    reviewers:
      - alice
    groups: []
  rules:
    - effect: allow
      reason: "Reviewer declaration for publication approval."
```

Reference both Policies from `Task/create-pull-request` `spec.policies`. The
capability rule decides that approval is required; the approval Policy supplies
the authorized identities. Group names are resolved by the injected,
provider-neutral reviewer resolver; the default runtime has no group resolver,
so prefer explicit reviewer identities unless the deployment supplies one.

The gate applies to publication and capability decisions produced by
CreatePullRequest. Approval never replaces policy evaluation: the handler
re-evaluates the capability and publication policies and rechecks the approval
digest before it performs the gated action.

## 2. Find And Inspect A Pending Request

Start from the WorkflowExecution and let the deterministic explanation identify
the decisive evidence:

```sh
aep explain workflowexecution-...
aep approvals list --status PENDING
aep approvals show approval-...
```

Use `--state-file <path>` when inspecting a backup instead of the live
`$AEP_STATE_ROOT/runtime/objects.json` checkpoint.

Before approving, verify all of the following from `approvals show`:

- `targetAction` is the action you expect, such as `github.create_pr`.
- `target` names the expected repository, branch, and other publication target.
- `repositoryRevision` is the revision whose artifacts were evaluated.
- `artifactRequestDigest` matches the digest reported by the workflow evidence.
- `governingPolicyRefs` contains the Policy versions you reviewed.
- `authorizedReviewers` includes you.
- `expiresAt`, when present, has not passed.

The inspection output is redacted by default. Do not use `--unsafe-debug` for
routine approvals.

## 3. Record A Decision

Approve or reject with the authenticated stable identity that appears in
`authorizedReviewers`:

```sh
aep approvals decide approval-... \
  --actor alice \
  --decision APPROVED \
  --reason "Reviewed target, revision, patch digest, and validation evidence."
```

```sh
aep approvals decide approval-... \
  --actor alice \
  --decision REJECTED \
  --reason "The patch changes an unrelated runtime path."
```

A non-empty reason is required. The command records `approver`, `decidedAt`,
`reason`, and terminal status on the Approval. Use `--output json` for
machine-readable output. Add `--digest sha256:...` to supply an independently
verified current digest; if omitted, the digest bound to the persisted request
is used, and the handler still checks it against the current action before
execution.

An approve decision wakes only the task's current approval binding. A
rejection or expiry makes that wait a non-retryable `POLICY` failure on the next
reconciliation. A later attempt that still requires approval creates a new
request and must be decided again.

The command writes to the durable runtime checkpoint. It is one of the explicit
`aep` write commands (`replay`, `eval`, `experiment`, and `approvals decide`);
the remaining inspection commands are read-only. Use `--state-file` only
against an offline copy; do not point it at a checkpoint another active writer
owns.

## 4. Invalidation Rules

An approval is bound to one canonical tuple:

```text
action + target + repository revision + artifact digests
```

Changing any bound input invalidates the approval:

- a different patch, artifact, or artifact digest;
- a different target repository, base branch, or working branch;
- a different repository revision or knowledge snapshot;
- a materially different requested capability or Policy version.

A stale decision is not silently applied. The scheduler wakes only the task's
current approval id, and the handler rechecks the current digest. If the
evidence changed, open a new gate and decide the new request. Historical
approval records remain immutable execution evidence.

Unauthorized attempts are recorded as `ApprovalAuthorizationDenied` events.
They do not change task eligibility or the request status.

## 5. Task And Workflow State

While a gate is pending:

- the TaskExecution status is `AWAITING_APPROVAL`;
- the request status is `PENDING`;
- the owning WorkflowExecution remains `RUNNING` by design so the same
  scheduler can resume the gated attempt;
- `aep explain <workflow-execution-id>` reports `APPROVAL_PENDING` and points to
  the Approval or governing PolicyDecision;
- `aep executions show` lists the waiting task and its approval evidence.

After an approval, the task returns to `PENDING` and is dispatched once. After
a rejection or expiry, the task becomes `FAILED` with failure class `POLICY`.
Publication, branch push, and pull-request creation remain blocked in both
cases.

The decision command changes durable state; it does not execute the workflow
itself. The self-hosting reconciliation consumer keeps the source outbox
request pending while a workflow is in flight and reconciles it again after the
decision. A replay or other manually dispatched execution has no outbox
consumer; after a decision it remains `PENDING` until the runtime reconciles it
through its normal path. AEP-058 tracks the explicit `aep resume` command for
that case.

## 6. Error Codes

`aep approvals decide` exits non-zero with a stable JSON error code:

| Code | Meaning |
| --- | --- |
| `APPROVAL_NOT_FOUND` | No Approval exists with that id. |
| `APPROVAL_UNAUTHORIZED` | The actor is not in `authorizedReviewers`. |
| `APPROVAL_STALE` | The supplied digest does not match the requested binding. |
| `APPROVAL_EXPIRED` | The request reached `expiresAt`; it is now `EXPIRED`. |
| `APPROVAL_ALREADY_DECIDED` | A different actor or conflicting decision already won. |
| `APPROVAL_DECISION_INVALID` | The decision is not `APPROVED` or `REJECTED`. |
| `APPROVAL_REASON_REQUIRED` | The reason is empty. |
| `APPROVAL_DIGEST_MISSING` | The request has no bound action digest. |
| `RUNTIME_STORE_ERROR` | The durable checkpoint could not be read or written. |

## 7. Security Notes

- Approvals are not a policy bypass. Capability and publication policy still
  run before the action.
- Approve only the exact target, revision, and digest you inspected. Do not
  approve from a ticket summary or a chat message.
- Use a stable operator identity; the actor string is recorded as evidence.
- Do not put credentials, tokens, or artifact bodies in the reason.
- Do not edit the runtime checkpoint by hand or reuse an approval id for a new
  action.
- Keep the operator's checkpoint access equivalent to the ability to publish.
  Treat approval authority as a privileged capability and review it like one.
- The runtime never exposes approval decisions through an unauthenticated
  network endpoint. The CLI requires local access to the durable checkpoint.

## 8. Verification

Run the focused tests before changing approval behavior:

```sh
python -m pytest tests/test_approval.py tests/test_approval_cli.py \
  tests/test_create_pull_request.py tests/test_workflow_scheduler.py
```

The tests cover approval, rejection, unauthorized identity, stale artifacts,
expiry, duplicate submissions, restart while pending, and post-approval single
execution.
