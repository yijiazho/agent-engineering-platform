# AEP-024: Implement GitHub Tool

**Status:** Not Started

## Context

The MVP reads issue data and creates pull requests.

## Deliverable

Implement GitHub Tool adapter for read issue and create pull request.

## Dependencies

* AEP-019
* AEP-020

## Acceptance Criteria

* Read issue returns structured issue data.
* Create pull request accepts branch, title, body, and base branch.
* PR creation requires Pre-Execution Capability Policy and Publication Policy.
* Adapter records GitHub response metadata.
* Tests use a fake GitHub client.
