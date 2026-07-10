# AI Agent Engineering Platform

AI Agent Engineering Platform (AEP) is a declarative, event-driven control plane for software engineering automation.

The project explores a Kubernetes-inspired architecture where AI workflows, agents, prompts, models, tools, policies, evaluations, knowledge sources, and runtime evidence are managed through explicit contracts rather than ad hoc chat sessions.

The initial MVP focuses on a GitHub-centered engineering loop:

1. Receive a GitHub Issue event.
2. Normalize the event.
3. Resolve a declarative Workflow and Task DAG.
4. Build deterministic ContextPackages.
5. Resolve Agents into ResolvedAgent runtime objects.
6. Generate a plan and patch.
7. Run validation.
8. Evaluate outputs.
9. Apply policy.
10. Open a pull request.
11. Persist execution history and GeneratedArtifacts.

## Design Principles

* Declarative Resources define desired AI behavior.
* Runtime objects capture observed execution state.
* Workflows orchestrate; Agents reason.
* Agents never retrieve repository knowledge directly.
* Context construction is deterministic and provenance-rich.
* Model providers are represented by Model resources, not Tools.
* Tools are non-model external capabilities governed by policy.
* GeneratedArtifacts are runtime outputs, not declarative Resources.

## Repository Layout

```text
docs/
  prd.md
  execution-plan.md
  implementation-tasks.md
  adr/
  architecture/
  tasks/
schemas/
  resources/
  runtime/
fixtures/
  resources/
  runtime/
src/
  aep/
tests/
```

## Local Development

Use a repository-local virtual environment for Python development. This isolates local test and tooling dependencies from the host interpreter and does not conflict with Docker or Kubernetes; containers remain the planned runtime and Tool execution isolation boundary.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,yaml]"
python -m pytest
```

`PyYAML` is optional for full YAML parsing in local `.ai/` resource files. JSON content in `.yaml` files works without that extra dependency.

Installed development package versions are captured in [requirements-dev.lock](requirements-dev.lock). Refresh it after dependency changes with `.\.venv\Scripts\python.exe -m pip list --format=freeze`.

## Key Documents

* [Product Requirements](docs/prd.md)
* [Architecture Overview](docs/architecture/overview.md)
* [Runtime Object Model](docs/adr/ADR-002-runtime-object-model.md)
* [MVP Vertical Slice](docs/adr/ADR-003-mvp-vertical-slice.md)
* [Execution Plan](docs/execution-plan.md)
* [Implementation Tasks](docs/implementation-tasks.md)

## Current Status

This repository is currently transitioning from documentation-first contracts into Python implementation.

The implementation plan is split into independent task files under [docs/tasks](docs/tasks/). Each task includes context, dependencies, deliverable, and acceptance criteria.

Task status is tracked in [docs/execution-plan.md](docs/execution-plan.md).

## MVP Scope

The first vertical slice targets one repository, one workspace, one workflow, and one GitHub event type: `github.issue.created`.

The MVP intentionally excludes pull request merge, deployment, multi-tenant authentication, multi-repository workflows, workflow generation, and LLM-as-judge evaluation.

## License

See [LICENSE](LICENSE).
