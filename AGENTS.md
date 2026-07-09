# Repository Guidelines

## Project Structure & Module Organization

This repository is currently documentation-first. Core product and architecture material lives under `docs/`:

* `docs/prd.md` defines the product direction.
* `docs/architecture/` contains subsystem architecture.
* `docs/adr/` records architectural decisions.
* `docs/tasks/` contains one implementation task per file.
* `docs/execution-plan.md` tracks topological task order and status.

There is no application source tree yet. When implementation begins, keep source code, tests, and deployment assets in clearly named top-level directories such as `src/`, `tests/`, and `deploy/`.

## Build, Test, and Development Commands

No build or test toolchain is defined yet. For now, use documentation and repository checks:

* `rg --files docs` lists all project documents.
* `git status --short` reviews changed and untracked files.
* `git log --oneline -5` checks recent commit style.

Add concrete build and test commands here when the first implementation stack is introduced.

## Coding Style & Naming Conventions

Use Markdown for project guidance. Keep headings descriptive, sections short, and examples concrete. Task files should follow the pattern `docs/tasks/AEP-###-short-kebab-name.md`, for example `AEP-001-define-resource-schemas.md`.

Use ASCII text unless a file already requires another character set. Prefer precise platform terms such as `WorkflowExecution`, `ContextPackage`, `ResolvedAgent`, and `GeneratedArtifact`.

## Testing Guidelines

For documentation changes, verify links and task counts manually. When code is added, each implementation task should include tests satisfying its acceptance criteria. Keep fixtures small and deterministic.

## Commit & Pull Request Guidelines

Recent commits use concise imperative summaries, such as `Initialize with design document`. Follow that style: start with a verb and keep the subject specific.

Pull requests should describe the changed documents or implementation tasks, link related issues, and note any validation performed.

## Architecture Guardrails

Do not model Artifact as a declarative Resource. Agents must not retrieve repository knowledge directly. Model providers belong to Model resources, not Tools.
