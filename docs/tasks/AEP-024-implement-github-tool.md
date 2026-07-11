# AEP-024: Implement GitHub Tool

**Status:** Not Started

## Context

GitHub is both the MVP event source and the external publication target. Interactions must use a Tool adapter so issue reads and pull-request creation have stable schemas, policy decisions, retry classification, trace metadata, and auditable responses.

Reading issue data is distinct from publishing a PR. PR creation may occur only after technical evaluation, Publication Policy, and pre-execution authorization; branch creation and push remain responsibilities of the Git Tool.

## Deliverable

Implement a GitHub Tool adapter that:

* defines structured operations for reading an issue and creating a pull request;
* maps provider responses and errors into stable AEP result and failure types;
* requires both Publication Policy and `github.create_pr` authorization for PR creation;
* records repository, issue or PR identifiers, URLs, provider request IDs, and trace metadata; and
* uses a fake client to test reads, successful publication, denial, invalid input, rate limits, and provider failures.

## Dependencies

* AEP-019
* AEP-020

## Acceptance Criteria

* Read issue returns structured issue data.
* Create pull request accepts branch, title, body, and base branch.
* PR creation requires Pre-Execution Capability Policy and Publication Policy.
* Adapter records GitHub response metadata.
* Tests use a fake GitHub client.
