# AEP-047: Build Hermetic Validation Image

**Status:** In Progress

## Context

The controlled self-hosting pilot now passes event admission, bounded context
construction, live model invocation, planning, patch generation, and patch
safety evaluation. MTP-10 remains blocked at `run-validation`. The observed
execution produced a valid patch that changed only `docs/execution-plan.md`,
then recorded this terminal failure:

```text
Docker validation failed: command exited with code 1:
python -m pytest /workspace/tests
```

The first configured validation command completed successfully. It installed
the hash-locked offline Python dependencies, installed AEP, and compiled the
source and tests. The second command collected 717 tests but finished with 618
passed, 34 failed, and 65 errors. The persisted Docker log repeatedly records:

```text
FileNotFoundError: [Errno 2] No such file or directory: 'git'
```

The self-hosting `run-validation:1.0.0` Resource pins a plain Python image:

```text
python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
```

That image supplies Python but not Git. The repository suite requires Git for
checkout provisioning, repository scanning, Git Tool behavior, patch
evaluation, and temporary repository fixtures. The AEP service image installs
Git, but the separately pinned validation image does not. Existing Resource
bundle tests prove offline Python installation with a small synthetic test;
they do not execute the complete repository suite inside the exact configured
validation image or verify its required operating-system executables.

The failed run also exposed other environment-sensitive results, including
filesystem symlink/race assertions, a malformed-provider recursion test, and
self-hosting deployment checks executed from a deliberately modified execution
worktree. Missing Git creates cascading noise, so these residual failures must
be reproduced after correcting the image and classified as genuine candidate
regressions, base-suite defects, or validation-environment contract failures.

RunValidation currently treats any nonzero test command as an `EVALUATION`
failure. That is correct for a candidate test regression but misleading when
the configured sandbox lacks a declared prerequisite. The production
validation boundary must establish its own readiness before evaluating a
generated change.

Resolve this without enabling runtime network access, weakening the full test
suite, mounting host executables into the sandbox, allowing floating image
tags, or treating the AEP service image as an implicit validation dependency.

## Reproduction

Use the exact image and commands declared by the self-hosting RunValidation
Task. A credential-free preflight demonstrates the immediate defect:

```powershell
docker run --rm --network none python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de git --version
```

The command fails because `git` is absent. Running the configured offline
bootstrap and complete test suite against an execution checkout reproduces the
terminal repository-test Evaluation. The regression harness must preserve the
same writable `/workspace` mount, disabled network, CPU/memory limits, command
order, and immutable base revision used by production.

Use the persisted MTP-10 evidence as the reference shape:

* WorkflowExecution `workflowexecution-8a85395e-e9a0-57f3-b969-8c97050ef69c`;
* Docker ToolInvocation `toolinvocation-da791b8919c0e9c9344a86ae`;
* successful build Evaluation and failed repository-tests Evaluation;
* test log address
  `sha256:b82ef93b82bdda7196ef5d41def4487945ea895b151be686e0b24b71e10e3d5a`;
* generated patch limited to `docs/execution-plan.md`; and
* repository revision `a65b27257b8c05b0a5d051ad746d2b946288b3e2`.

The implementation loop must not require another live GitHub issue until the
exact candidate validation image passes the credential-free regression gates.

The first corrected-image retry proved that image availability and executable
readiness are necessary but not sufficient. WorkflowExecution
`workflowexecution-3a9fa81a-5da9-5aae-9da1-c9070af40749`, bound to repository
revision `3435ec6fdc8c1028327b9f0cae7997c94700a635`, used validation image
`sha256:6e0214265e1c8bbdc0553413801dded85ede2c1c2e90be413d0c02fae17fbf5a`.
Image readiness passed and the offline build command exited `0`, but the full
test command exited `1` because
`test_dogfood_accepts_clean_windows_crlf_resource_checkout` relied on
`core.autocrlf=true` producing CRLF bytes on the Linux validation platform.
The host-side checks did not expose the failure before the image was consumed,
but it failed in the environment defined by `deploy/validation/Dockerfile`.
This escaped the pre-publication checks because no required, checked-in gate
built that Dockerfile and ran the complete production command sequence inside
its resulting image.

## Deliverable

Implement a dedicated, reproducible validation image and validation-readiness
contract that:

* builds from an immutable base and contains the pinned Python runtime plus
  every operating-system executable required by the configured repository
  build and tests, including Git and trusted CA certificates;
* publishes and consumes the validation image by immutable digest, separately
  from the AEP control-plane/service image;
* retains the current hash-locked offline Python wheelhouse and installs no
  dependencies from package indexes during RunValidation;
* keeps runtime networking disabled and confines writes, temporary files, Git
  repositories, and command logs to the invocation sandbox and authorized
  execution checkout;
* declares or otherwise centrally defines required validation executables and
  performs a bounded readiness check against the exact pinned image before the
  repository build and test commands begin;
* records missing images, missing executables, incompatible versions, mount or
  permission failures, and sandbox-startup defects as explicit safe
  configuration/infrastructure evidence rather than candidate test failures;
* continues to record a nonzero build or test outcome as an Evaluation failure
  after image readiness succeeds, preserving separate build and test
  EvaluationResults and content-addressed logs;
* runs the complete repository suite in the production-equivalent Linux
  sandbox from both a clean baseline checkout and a deliberately modified
  execution worktree, then fixes or isolates assumptions that incorrectly
  depend on the host platform, ambient environment, or a clean control-plane
  checkout;
* provides one checked-in, credential-free verification entrypoint under
  `deploy/validation/` that builds `deploy/validation/Dockerfile` and runs the
  exact RunValidation readiness probes, offline bootstrap, and complete test
  command with the production workspace mount, working directory, disabled
  network, resource bounds, and command order;
* makes that entrypoint the shared local and CI release gate, rather than
  maintaining a synthetic smoke test or a second command sequence that can
  drift from the RunValidation Resource;
* verifies both the image built from the reviewed Dockerfile and the published
  immutable digest selected for `image.lock.json`; promotion must prove that
  the digest being recorded is the same image that passed the gate and must
  rerun the credential-free probes by digest;
* constructs platform-specific fixtures, including Windows CRLF checkouts,
  explicitly so their semantics are reproducible inside the Linux validation
  container and do not depend on the Docker host's operating system or global
  Git configuration;
* preserves security tests for symlink races, Git hooks, ambient credentials,
  path confinement, revision binding, and network denial rather than skipping
  them to make validation pass;
* versions the changed RunValidation Task and every affected Workflow, Tool,
  Policy, Evaluation, Workspace, or fixture reference as one consistent
  immutable Resource graph; and
* updates image build/publish instructions, dependency provenance, operator
  preflight, validation architecture, and failure-triage documentation.

The image may contain tools required to build and test the repository, but it
must not contain provider credentials, source credentials, Docker socket
access, deployment authority, or undeclared network dependencies.

## Dependencies

* AEP-023
* AEP-027
* AEP-032
* AEP-039
* AEP-040

## Acceptance Criteria

* The dedicated validation image is built from reviewed source, published by
  immutable digest, and contains the expected pinned Python major/minor version
  and a working Git executable. Tests reject a floating tag or digest drift.
* A credential-free command against the exact configured image reports the
  expected Python and Git versions with `--network none` and without mounting
  host executables or credentials.
* The production RunValidation path performs readiness checks before build or
  test execution. A scripted image without Git fails with an explicit
  configuration/infrastructure classification and does not publish a failed
  repository-tests Evaluation that blames the candidate patch.
* The exact image completes `deploy/validation/offline_bootstrap.py` using only
  the committed hash-locked wheelhouse. Tests prove that package-index and
  external network access remain unavailable.
* A documented, checked-in verification entrypoint builds
  `deploy/validation/Dockerfile` and exits nonzero unless the image readiness
  probes, `deploy/validation/offline_bootstrap.py`, and
  `python -m pytest /workspace/tests` all succeed in the resulting Linux image.
  It uses the same commands and isolation inputs as the versioned
  RunValidation Resource; tests reject command, image, mount, network, working
  directory, or resource-limit drift between the entrypoint, Resource, lock,
  and deterministic fixture.
* The verification entrypoint runs from a clean baseline checkout and from a
  separate writable execution checkout containing a small allowed
  documentation change. It never modifies or mounts the immutable Resource
  checkout as the candidate workspace.
* Repository CI invokes the same entrypoint on a Docker-capable Linux worker
  whenever the validation Dockerfile, lock, bootstrap, dependency inputs,
  RunValidation Resource, or repository tests change. A failing readiness,
  build, or test command fails the required gate and prevents the image digest
  or AEP release from being treated as validated.
* The image publication procedure promotes the image that passed the
  Dockerfile gate, resolves its registry manifest digest, reruns the Python and
  Git probes using that digest with `--network none`, and only then updates
  `image.lock.json` and the versioned Resource graph. Tests reject a lock that
  names an unverified or differently built digest.
* The complete `python -m pytest /workspace/tests` suite passes inside the
  exact pinned image at the recorded clean base revision. A small permitted
  documentation-only change also passes from a dirty execution worktree,
  proving that deployment tests do not confuse candidate dirtiness with drift
  in the immutable Resource checkout.
* After Git is available, every residual failure from the recorded 34 failures
  and 65 errors is reproduced and resolved or assigned to a separately
  justified failing baseline. No failure is hidden through broad test skips,
  ignored exit codes, reduced test discovery, or relaxed assertions.
* Filesystem symlink/race, Git hook and credential isolation, patch safety,
  checkout fencing, and repository revision tests execute in the Linux
  validation sandbox and retain their security guarantees.
* The Windows CRLF deployment test passes in the Linux validation image because
  its fixture creates and verifies CRLF worktree content explicitly. The test
  does not infer Windows checkout behavior from the host platform, ambient Git
  configuration, or `core.autocrlf` alone, and still proves that a real content
  change makes the Resource checkout dirty.
* A successful Docker ToolInvocation records two successful command outcomes,
  build/test EvaluationResults both report `PASS`, and the immutable
  Evaluation report retains image digest, command identity, duration, exit
  status, and content-addressed log references without embedding log bodies.
* Image-readiness failures and candidate build/test failures remain separately
  diagnosable from TaskExecution, ToolInvocation, EvaluationResult, and
  structured lifecycle evidence. Logs contain no artifact bodies, credentials,
  ambient host environment, or unrestricted command output.
* `tests/test_docker_validation_tool.py`, `tests/test_build_test_evaluation.py`,
  `tests/test_run_validation.py`, `tests/test_self_hosting_resource_bundle.py`,
  and deployment tests cover the new image and readiness contract, followed by
  the complete local and exact-image test suites.
* The versioned self-hosting Resource graph loads without floating references,
  deterministic inventory fixtures name the new versions and image digest,
  and startup rejects an unavailable or mismatched validation image.
* The self-hosting runbook verifies validation-image availability and required
  executables before public ingress is enabled and provides bounded commands
  for inspecting failed command metadata and content-addressed logs.
* A controlled MTP-10 rerun reaches RunValidation with the same revision-bound
  patch, records passing build and repository-test Evaluations, and proceeds to
  EvaluateAcceptance instead of failing because Git or another declared image
  prerequisite is absent.
* `README.md`, validation and deployment architecture, the self-hosting
  runbook, image build files, schemas, Resources, fixtures, this task, and
  `docs/execution-plan.md` describe the same final image, readiness,
  classification, isolation, and operator behavior.

## Implementation State

The dedicated image, immutable digest wiring, offline dependency bootstrap,
production readiness evidence, Dockerfile-based verification entrypoint, and
Linux CI/release gate are implemented. The complete suite passes against both
the source-built image and the promoted published digest in separate clean and
documentation-only dirty Linux workspaces, and the CRLF fixture is
host-independent. Promotion records the exact tested artifact identity rather
than requiring later builds to reproduce build-created metadata. GitHub Actions
run `32553884007` passed the complete source-built and published-image gate from
clean Linux checkout `73f3d480aab67005975f369651cc44e734471e56`. The task
remains In Progress until the operator-controlled MTP-10 rerun records passing
build and repository-test Evaluations before proceeding to EvaluateAcceptance.
