# Offline Validation Bootstrap

This directory contains the pinned artifacts required to turn the
digest-pinned Python runtime into AEP's repository validation environment
without network access.

`Dockerfile` builds the dedicated validation image from a digest-pinned Python
base and adds only Git and CA certificates. The published digest in
`image.lock.json` must match `.ai/tasks/run-validation.yaml` and the
deterministic bundle fixture. The lock also records the source image
configuration digest and every isolation input consumed by `verify.py`.
RunValidation repeats the bounded readiness checks in its sandbox before
build/test commands. A missing executable or version mismatch is configuration
evidence, not a failed candidate test.

`offline-requirements.txt` pins every runtime, test, and Python build dependency
and records the accepted SHA-256 wheel hashes. `wheelhouse/` contains only those
artifacts for CPython 3.12 on the Linux validation platform and Windows local
proof platform. `offline_bootstrap.py` installs from that directory using
`--no-index --no-deps --require-hashes`, installs the mounted project with
dependency resolution and build isolation disabled, and compiles the source and
tests.

To refresh the wheelhouse, select exact versions from `requirements-dev.lock`,
pin compatible `setuptools` and `wheel` versions, download only binary wheels
for each supported CPython 3.12 platform, and replace the lock-file hashes in
the same change. Then run:

```powershell
python -m pytest tests/test_self_hosting_resource_bundle.py
python -m pytest
```

The focused test rejects a missing, additional, or hash-mismatched wheel and
executes both configured validation commands in a fresh environment with index
access disabled.

## Required Validation Gate

Run the one checked-in gate from a clean release checkout:

```powershell
python deploy/validation/verify.py verify
```

The gate reads the versioned RunValidation Resource, image lock, and
deterministic fixture. It fails on drift, builds this Dockerfile for
`linux/amd64`, verifies that the build has the locked image identity, and runs
the declared Python and Git probes without credentials or networking. It then
runs the exact offline bootstrap and complete test command in two separate
writable workspaces: one clean snapshot and one containing only a bounded
documentation change. Both containers use `/workspace`, `--network none`, two
CPUs, 1 GiB of memory, the declared command order, and the shared 600-second
deadline. The Resource checkout is never mounted as a candidate workspace.

During development only, include the current tracked and untracked working-tree
snapshot without claiming release validation:

```powershell
python deploy/validation/verify.py candidate --include-working-tree
```

CI invokes `verify`, not `candidate`, on a clean Docker-capable Linux worker.

## Publish And Promote

Authenticate Docker to the target registry, choose a non-floating candidate
tag, and let the same entrypoint build and test before it pushes:

```powershell
python deploy/validation/verify.py promote `
  --target ghcr.io/yijiazho/agent-engineering-platform-validation:aep-047-next
```

Promotion exits nonzero unless both source workspaces pass. It pushes that
tested image, resolves the registry-reported manifest digest, pulls it by
digest, proves that its image configuration digest equals the tested source
image, and reruns both readiness probes by digest with networking disabled. It
prints a small JSON record containing `image` and `verifiedImageId`; it does not
edit Resources.

In a follow-up reviewed change, copy those two values into
`image.lock.json`, update the RunValidation Resource and deterministic fixture,
version every affected immutable Resource reference, and run `verify` again.
Never record a digest obtained from a separately rebuilt or merely tagged
image. Before public ingress, the credential-free published-image preflight is:

```powershell
python deploy/validation/verify.py published
```
