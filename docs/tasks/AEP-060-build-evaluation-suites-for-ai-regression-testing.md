# AEP-060: Build Evaluation Suites for AI Regression Testing

**Status:** Not Started

## Context

AEP evaluates outputs inside individual workflow executions, but it does not yet provide a deterministic way to run the same AI configuration across a curated set of historical or fixture engineering tasks. Without regression suites, changes to prompts, models, agents, context construction, or evaluation logic cannot be measured systematically before promotion.

Evaluation suites should reuse AEP's existing execution, replay, artifact, and EvaluationResult contracts instead of creating a separate benchmark runtime.

## Deliverable

Introduce a versioned evaluation-suite definition and runner that executes a declared set of cases against a selected workflow/agent configuration and aggregates normalized quality, cost, token, and latency metrics.

Provide CLI support to run a suite and inspect case-level failures and aggregate results. Suite execution must be side-effect-safe by default.

## Dependencies

* AEP-025
* AEP-026
* AEP-027
* AEP-033
* AEP-051
* AEP-052
* AEP-056
* AEP-057

## Acceptance Criteria

* A versioned evaluation-suite contract can reference immutable fixture or historical execution cases and the Workflow/Evaluation resources used to score them.
* `aep eval <suite>` executes all selected cases in isolated replay/evaluation runs without publishing pull requests or performing other external side effects by default.
* Every case records its source revision/input identity, resolved Resource versions, generated artifacts, EvaluationResults, model/token usage, cost when available, and elapsed time.
* Suite aggregation reports at least case pass rate, configured evaluation metrics, build/test success where applicable, model invocation count, token usage, cost when available, and latency.
* One failed case does not erase successful case evidence; suite status and partial results remain durable and inspectable.
* Re-running the same suite definition against the same pinned configuration produces the same case selection, ordering, and aggregate calculation even if model outputs differ.
* Suites can include deterministic expected-output/fixture cases that run without live provider credentials.
* Evaluation schema/version changes are reflected explicitly in result provenance and never silently reinterpret historical scores.
* Tests cover mixed pass/fail suites, aggregation, interrupted suite execution, side-effect suppression, deterministic case ordering, and historical execution references.
* Documentation includes a repository regression-suite example for issue-to-PR behavior.
