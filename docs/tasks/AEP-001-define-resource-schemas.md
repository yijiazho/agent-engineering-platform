# AEP-001: Define Resource Schemas

**Status:** Not Started

## Context

AEP uses declarative Resources stored in `.ai/`. The MVP Resources are Workspace, Workflow, Task, Agent, Prompt, Model, Tool, KnowledgeBase, Policy, Evaluation, and Event. Artifact is not a Resource.

## Deliverable

Define versioned schema contracts for all MVP Resources.

## Dependencies

None.

## Acceptance Criteria

* Schemas exist for every MVP Resource.
* Every Resource includes `apiVersion`, `kind`, `metadata.name`, `metadata.version`, and `spec`.
* Schemas reject floating references such as `latest`.
* Schemas distinguish `Model` from non-model `Tool`.
* Schemas do not define `Artifact` as a Resource.
* Valid and invalid fixtures exist for every Resource.
