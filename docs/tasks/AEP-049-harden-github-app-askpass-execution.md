# AEP-049: Harden GitHub App Askpass Execution

**Status:** In Progress

Credential-free implementation and local contract validation are complete.
The corrected immutable service generation still requires publication and the
controlled MTP-09/MTP-10 live run before this task can be marked completed.

## Context

The self-hosting Git Tool performs authenticated fetch and push operations with
short-lived GitHub App installation tokens. `GitHubAppGitCredentialProvider`
keeps those tokens out of remotes and command arguments by creating a one-use
askpass program, exposing its path and credentials only through the scoped Git
subprocess environment, and deleting the helper when the lease closes.

The Git subprocess boundary intentionally receives exactly its supplied
environment rather than inheriting the Workflow Runtime environment. This
prevents ambient credentials, proxy settings, configuration, and other host or
container state from entering a Tool invocation. For an authenticated push,
the current environment contains the askpass path, askpass requirement,
ephemeral username/password, `GIT_CONFIG_NOSYSTEM=1`, and
`GIT_TERMINAL_PROMPT=0`; it does not contain `PATH`.

The controlled MTP-09/MTP-10 run for issue #65 reached the final publication
path after AEP-048 was corrected. The correlated execution was:

```text
Event:              event-da682d25-c235-502c-8895-ebb75cf20268
WorkflowExecution:  workflowexecution-a5d3a68f-7860-5422-b524-793da734cf9b
Trace:              trace-afd16862-c29d-52b1-a9b5-9e3948ebd503
Repository revision: 9c2d943dda6a7530b6e66fcd710330499d41965d
Execution branch:   aep/execution/b6089a425a895fa2e7e3
Git ToolInvocation: toolinvocation-a3b3c1a4af8b94adb2d2f2cc
```

All six Tasks were created. AnalyzeIssue, BuildImplementationPlan,
GeneratePatch, RunValidation, and EvaluateAcceptance succeeded. Publication
Policy `publication-evidence:1.1.0` matched its rule and returned `ALLOW`, and
the separate `git.push` capability decision also returned `ALLOW`.
CreatePullRequest committed the accepted patch locally, but the authenticated
push exited `128`:

```text
env: 'python3': No such file or directory
error: unable to read askpass response from the temporary askpass program
fatal: could not read Username for 'https://github.com': terminal prompts disabled
```

The askpass file is currently generated with this interpreter directive:

```text
#!/usr/bin/env python3
```

The deployed service image contains `python` and `python3` under
`/usr/local/bin`. Its ordinary process `PATH` includes `/usr/local/bin`, but
the isolated Git process correctly does not inherit that value. Git itself is
found at `/usr/bin/git` through the operating system's default executable
search. When Git invokes askpass, `/usr/bin/env python3` also falls back to the
default `/bin:/usr/bin` search and cannot find `/usr/local/bin/python3`.
Therefore the helper has an undeclared dependency on an ambient `PATH` that
the Git sandbox deliberately removes.

Because the push command had begun, the Git Tool conservatively persisted
`remoteMutationState: UNKNOWN`. Provider reconciliation subsequently found no
remote branch named `aep/execution/b6089a425a895fa2e7e3` and no pull request
for that head, proving that this attempt created no GitHub mutation. No
`PULL_REQUEST_DESCRIPTION` artifact was published. The Event outbox completed
after the terminal WorkflowExecution was durable.

Existing provider tests verify lease scope, file permissions, token redaction,
cleanup, and fake/local Git behavior, but they do not execute the generated
helper with the production `SubprocessGitSandbox` environment inside the
service image. A unit test can read the helper or run it under the developer's
ambient `PATH` and still miss this production failure.

This defect must not be fixed by inheriting the complete service environment,
placing credentials in the remote URL, enabling terminal prompts, logging
helper output, or treating a failed/unknown push as safe to repeat without
reconciliation.

## Reproduction

Use a credential-free test with a fake installation-token provider and a
temporary lease directory:

1. Create a `GitHubAppGitCredentialProvider` bound to the expected repository,
   `origin`, and `aep/execution/` branch prefix.
2. Acquire a lease with a synthetic token and inspect no token value.
3. Execute the generated askpass program for username and password prompts
   under the same minimal environment passed by `SubprocessGitSandbox`, with
   no inherited `PATH`, home-directory Git configuration, terminal prompt, or
   ambient credential variables.
4. Run the same proof inside the image built from `deploy/local/Dockerfile`,
   where the Python runtime resides under `/usr/local/bin`.

Before the fix, the helper exits nonzero with `/usr/bin/env` unable to locate
`python3`. A test that adds the host `PATH`, directly invokes the developer's
Python executable, or bypasses the generated helper does not reproduce the
defect.

Add an integration reproduction through the real `SubprocessGitSandbox` and
Git Tool push path using a local credential-challenging HTTP Git fixture or an
equivalent deterministic process boundary. It must prove that Git invokes the
actual leased helper, receives the synthetic credentials, and records a
successful push without network access to GitHub or use of live credentials.
The fixture must also reproduce helper startup failure and verify the
resulting safe failure and mutation-state classification.

Use the live evidence above only as the reference shape. Do not redeliver
issue #65 or retry its terminal ToolInvocation. Before any future live run,
reconcile the recorded unknown mutation by exact owner, repository, branch,
base, and head; the current reconciliation result is no branch and no PR.

## Deliverable

Implement a portable, fail-closed GitHub App askpass execution contract that:

* makes the generated helper executable using only dependencies explicitly
  guaranteed by the service image and scoped Git environment, without relying
  on inherited `PATH`, shell startup files, user profiles, global Git
  configuration, or host interpreters;
* uses a reviewed absolute interpreter/runtime contract or an equivalently
  minimal helper format whose executable is verified in the production image;
  if an interpreter path is derived at runtime, bind it to the running service
  executable and reject unsafe, missing, non-absolute, or non-executable paths;
* preserves the minimal subprocess environment instead of adding the full
  service `PATH` or copying arbitrary ambient variables into Git;
* validates deterministic helper startup before the remote push boundary when
  practical, without printing, persisting, or comparing the credential body in
  logs or runtime evidence, so an unavailable helper is classified before a
  remote mutation is marked started;
* returns username and password only for Git's expected prompts, emits no extra
  output, rejects unsupported invocations safely, and never places credentials
  in process arguments, repository configuration, remote URLs, exceptions,
  tracebacks, ToolInvocation records, or content-addressed logs;
* retains one-use lease ownership, restrictive directory/file permissions,
  token lifetime bounds, memory clearing, helper deletion, and cleanup on
  success, denial, timeout, cancellation, and every failure path;
* exercises the real provider, generated helper, `SubprocessGitSandbox`, and
  Git Tool integration with a deterministic local credential challenge rather
  than replacing the helper or sandbox with a fake at the critical boundary;
* adds a service-image smoke/integration gate that runs the helper with the
  exact minimal environment used by production and fails if the image's
  interpreter or executable layout drifts;
* distinguishes deterministic pre-mutation helper configuration/startup
  failures from provider authentication/authorization failures and genuinely
  ambiguous post-start push outcomes using stable safe classifications;
* preserves `UNKNOWN` once a remote mutation could have occurred and requires
  owner/head/base reconciliation before any retry, while allowing confirmed
  pre-mutation helper failure to remain `NOT_ATTEMPTED`;
* verifies successful authenticated push identity, expected base/head,
  authorized execution-branch prefix, and subsequent GitHub PR creation remain
  governed by the existing Publication Policy and separate capability gates;
  and
* updates Git Tool, GitHub App provider, deployment-image, security,
  observability, and operator documentation for the explicit helper/runtime
  contract and reconciliation procedure.

The implementation may use a minimal POSIX helper in the Linux self-hosting
profile or an absolute service interpreter such as the value derived from the
running Python executable. Whichever design is selected must be tested through
the exact subprocess and image boundary. Do not add a floating executable
lookup merely to make the current image pass.

## Dependencies

* AEP-022
* AEP-034
* AEP-039
* AEP-041
* AEP-048

## Acceptance Criteria

* A credential-free test acquires a real GitHub App credential lease with a
  synthetic token and successfully executes its generated askpass helper under
  an environment containing no `PATH`, inherited Git configuration, terminal
  prompt, proxy variables, home-directory variables, or ambient credentials.
* The same helper test runs inside the image built from
  `deploy/local/Dockerfile` and proves the exact executable/interpreter used by
  the helper exists and is executable. The test fails if the image layout or
  helper directive drifts back to an unresolved `env python3` dependency.
* A production-boundary integration test uses `SubprocessGitSandbox`, the real
  Git Tool push operation, the generated one-use helper, and a deterministic
  local credential-challenging Git endpoint. It confirms the authorized
  execution branch is pushed and no live GitHub credential or network call is
  required.
* The integration test proves the sandbox receives only allowlisted Git and
  credential variables. Adding the host/service environment, unrestricted
  `PATH`, credential helpers, proxy configuration, `HOME`, or repository-local
  hooks causes the test to fail.
* Username and password prompt tests return only their corresponding synthetic
  values. Unsupported prompts fail closed or return no credential according to
  the documented contract. No test output, exception, command log, runtime
  object, or persisted file contains the synthetic token after lease cleanup.
* Lease directories and helper files use restrictive permissions and are
  removed on success, acquisition failure, Git failure, timeout, cancellation,
  and repeated idempotent close. Tests cover cleanup when token acquisition or
  helper creation fails partway through.
* A missing or non-executable helper dependency detected before push becomes a
  stable configuration/startup failure with remote mutation
  `NOT_ATTEMPTED`. If Git has begun a push and the outcome cannot be confirmed,
  the ToolInvocation remains `UNKNOWN` and cannot be automatically retried.
* Reconciliation tests cover all three remote outcomes: no branch, the exact
  expected branch/head, and a conflicting branch/head. Only the exact expected
  identity may be confirmed or reused; absence and conflict never create a PR
  implicitly.
* Publication Policy and `git.push` remain required before helper acquisition
  or push. `github.create_pr` remains separately authorized after the pushed
  head is confirmed. Tests prove helper success cannot bypass any policy gate.
* Existing GitHub App token caching, installation binding, issue reads, PR
  idempotency, rate-limit handling, redaction, and ambiguous mutation behavior
  continue to pass unchanged.
* `tests/test_github_app_provider.py`, `tests/test_git_tool.py`,
  `tests/test_create_pull_request.py`, deployment-image tests, and the
  end-to-end harness cover the corrected boundary. The complete local suite
  and the relevant Docker/service-image integration gate pass.
* Git ToolInvocation and lifecycle evidence identify operation, repository,
  branch, safe failure class, mutation state, timing, command exit status, and
  content-addressed redacted logs without helper source, credential values,
  ambient environment, provider response bodies, or unrestricted stderr.
* The GitHub App operator guide and self-hosting runbook document the helper's
  executable contract, a credential-free readiness probe, safe inspection of
  helper startup failures, and mandatory branch/PR reconciliation for
  `UNKNOWN` push evidence.
* After credential-free validation and publication of a corrected immutable
  service generation, one controlled MTP-09/MTP-10 run executes all six Tasks,
  records passing validation and acceptance, confirms the authenticated push,
  authorizes `github.create_pr`, and creates exactly one open, unmerged PR.
  Redelivery creates no duplicate Event, WorkflowExecution, branch, or PR.
* `README.md`, Git Tool and deployment architecture, GitHub App operations,
  self-hosting operations, tests, this task, and `docs/execution-plan.md`
  describe the same minimal environment, helper executable, credential
  lifetime, failure classification, mutation reconciliation, and live status.
