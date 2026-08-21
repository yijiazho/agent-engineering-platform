# Offline Validation Bootstrap

This directory contains the pinned artifacts required to turn the
digest-pinned Python runtime into AEP's repository validation environment
without network access.

`Dockerfile` builds the dedicated validation image from a digest-pinned Python
base and adds only Git and CA certificates. The published digest in
`image.lock.json` must match `.ai/tasks/run-validation.yaml`. Before enabling
ingress, use `docker run --rm --network none <pinned-image> python --version`
and `git --version`; do not mount host executables or credentials. RunValidation
repeats those bounded checks in its sandbox before build/test commands. A
missing executable or version mismatch is configuration evidence, not a failed
candidate test.

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
