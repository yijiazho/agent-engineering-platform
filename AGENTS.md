# Repository Guidelines

## Project Structure & Module Organization

This repository is contract-first and in active MVP implementation. Product and architecture material lives under `docs/`:

* `docs/prd.md` defines the product direction.
* `docs/architecture/` contains subsystem architecture.
* `docs/adr/` records architectural decisions.
* `docs/tasks/` contains one implementation task per file.
* `docs/execution-plan.md` tracks topological task order and status.

Implementation code lives under `src/`, with automated tests under `tests/`.
JSON Schemas live under `schemas/`, deterministic examples under `fixtures/`,
and repository-specific agent workflows under `skills/`. Keep future source
code, tests, and deployment assets in clearly named top-level directories such
as `src/`, `tests/`, and `deploy/`.

## Documentation Synchronization

Every code change must include matching documentation updates when behavior, setup, commands, configuration, or public APIs change. Review `README.md` first, then update related task files, architecture notes, ADRs, schemas, fixtures, or execution-plan status as appropriate.

Do not leave documentation describing stale project structure, commands, or task status. If no documentation update is needed for a code change, note that decision in the final handoff or pull request description.

## Build, Test, and Development Commands

Use the local Python test suite and repository checks on Windows, macOS, or
Linux. Python 3.12 is the pinned validation interpreter; newer 3.12+
interpreters are supported for development.

* `python3 -m venv .venv` creates the repo-local virtual environment.
* `.\.venv\Scripts\Activate.ps1` activates it in Windows PowerShell.
* `source .venv/bin/activate` activates it in macOS or Linux shells.
* `python -m pip install -e ".[dev,yaml]"` installs local development dependencies.
* `python -m pytest` runs the current automated tests.
* `python deploy/validation/verify.py verify` runs the Docker-capable
  validation gate described in `deploy/validation/README.md`.
* `rg --files docs` lists all project documents.
* `git status --short` reviews changed and untracked files.
* `git log --oneline -5` checks recent commit style.

Use the virtual environment for local development. Docker remains the runtime
and Tool execution isolation boundary for validation; Kubernetes is a future
deployment target. Neither replaces a local Python venv for contributor
workflows.

The hash-locked offline bootstrap under `deploy/validation/wheelhouse/` is
pinned to CPython 3.12 for Linux x86_64, Windows amd64, and macOS arm64/x86_64. Its
focused test skips on other interpreters or platforms. Run the full suite with
CPython 3.12 before treating a platform result as validation evidence.

## Coding Style & Naming Conventions

Use Markdown for project guidance. Keep headings descriptive, sections short, and examples concrete. Task files should follow the pattern `docs/tasks/AEP-###-short-kebab-name.md`, for example `AEP-001-define-resource-schemas.md`.

Use ASCII text unless a file already requires another character set. Prefer precise platform terms such as `WorkflowExecution`, `ContextPackage`, `ResolvedAgent`, and `GeneratedArtifact`.

## Testing Guidelines

For documentation changes, verify links and task counts manually. When code is added, each implementation task should include tests satisfying its acceptance criteria. Keep fixtures small and deterministic.

## Project Skills

Use `skills/implement-aep-feature/SKILL.md` when implementing an AEP task or feature from its contracts and acceptance criteria.

Use `skills/review-aep-pr/SKILL.md` for an independent, read-only review of another agent's implementation, a pull request, or a local diff using severity-ranked findings and the project scoring rubric.

## Commit & Pull Request Guidelines

Recent commits use concise imperative summaries, such as `Initialize with design document`. Follow that style: start with a verb and keep the subject specific.

Pull requests should describe the changed documents or implementation tasks, link related issues, and note any validation performed.

## Architecture Guardrails

Do not model Artifact as a declarative Resource. Agents must not retrieve repository knowledge directly. Model providers belong to Model resources, not Tools.
