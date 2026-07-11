# Repository Guidelines

## Project Structure & Module Organization

This repository is currently documentation-first. Core product and architecture material lives under `docs/`:

* `docs/prd.md` defines the product direction.
* `docs/architecture/` contains subsystem architecture.
* `docs/adr/` records architectural decisions.
* `docs/tasks/` contains one implementation task per file.
* `docs/execution-plan.md` tracks topological task order and status.

Implementation code lives under `src/`, with automated tests under `tests/`. Keep future source code, tests, and deployment assets in clearly named top-level directories such as `src/`, `tests/`, and `deploy/`.

## Documentation Synchronization

Every code change must include matching documentation updates when behavior, setup, commands, configuration, or public APIs change. Review `README.md` first, then update related task files, architecture notes, ADRs, schemas, fixtures, or execution-plan status as appropriate.

Do not leave documentation describing stale project structure, commands, or task status. If no documentation update is needed for a code change, note that decision in the final handoff or pull request description.

## Build, Test, and Development Commands

Use the local Python test suite and repository checks:

* `python -m venv .venv` creates the repo-local virtual environment.
* `.\.venv\Scripts\Activate.ps1` activates it on Windows PowerShell.
* `python -m pip install -e ".[dev,yaml]"` installs local development dependencies.
* `python -m pytest` runs the current automated tests.
* `rg --files docs` lists all project documents.
* `git status --short` reviews changed and untracked files.
* `git log --oneline -5` checks recent commit style.

Use the virtual environment for local development. Docker and Kubernetes remain the runtime and Tool execution isolation boundary; they do not replace a local Python venv for contributor workflows.

Add concrete build and test commands here when additional implementation stacks are introduced.

## Coding Style & Naming Conventions

Use Markdown for project guidance. Keep headings descriptive, sections short, and examples concrete. Task files should follow the pattern `docs/tasks/AEP-###-short-kebab-name.md`, for example `AEP-001-define-resource-schemas.md`.

Use ASCII text unless a file already requires another character set. Prefer precise platform terms such as `WorkflowExecution`, `ContextPackage`, `ResolvedAgent`, and `GeneratedArtifact`.

## Testing Guidelines

For documentation changes, verify links and task counts manually. When code is added, each implementation task should include tests satisfying its acceptance criteria. Keep fixtures small and deterministic.

## Project Skills

Use `skills/review-aep-pr/SKILL.md` when reviewing pull requests or local diffs for severity-ranked findings and the project scoring rubric.

## Commit & Pull Request Guidelines

Recent commits use concise imperative summaries, such as `Initialize with design document`. Follow that style: start with a verb and keep the subject specific.

Pull requests should describe the changed documents or implementation tasks, link related issues, and note any validation performed.

## Architecture Guardrails

Do not model Artifact as a declarative Resource. Agents must not retrieve repository knowledge directly. Model providers belong to Model resources, not Tools.
