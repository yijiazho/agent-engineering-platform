# AEP-029: Implement AnalyzeIssue Task Handler

**Status:** Not Started

## Context

AnalyzeIssue extracts issue intent and acceptance criteria from the GitHub event and context.

## Deliverable

Implement Task handler using ContextPackage, ResolvedAgent, and AgentInvocation.

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
