# AEP-029: Implement AnalyzeIssue Task Handler

**Status:** Not Started

## Context

`AnalyzeIssue` is the first cognitive Task in the `issue-to-pr` workflow. It transforms the normalized GitHub issue and relevant repository context into a structured statement of requested change, candidate acceptance criteria, risks, and likely repository areas.

The handler coordinates existing runtime boundaries rather than embedding retrieval or model-provider logic: it requests a ContextPackage, resolves the Issue Analyzer Agent, invokes it, persists the output, and runs schema Evaluation.

## Deliverable

Implement the `AnalyzeIssue` Task handler that:

* requests and validates the Task-specific ContextPackage;
* resolves and invokes the explicitly versioned Issue Analyzer Agent;
* persists a typed issue-analysis GeneratedArtifact with provenance;
* runs schema Evaluation and updates TaskExecution lifecycle state; and
* tests success, invalid model output, missing context, and recoverable provider failure using fakes.

## Dependencies

* AEP-010
* AEP-013
* AEP-017
* AEP-018
* AEP-025

## Acceptance Criteria

* Handler creates ContextPackage.
* Handler resolves Issue Analyzer Agent.
* Handler invokes fake or configured model provider.
* Handler persists issue analysis GeneratedArtifact.
* Handler runs schema Evaluation.
* Tests cover successful analysis and invalid model output.
