# AEP-061: Implement Versioned AI Configuration Experiments

**Status:** Not Started

## Context

Once AEP can replay historical work and run evaluation suites, operators need a controlled way to compare a baseline AI configuration against one or more candidates. Today those comparisons would require ad hoc scripts and manual aggregation, weakening the provenance and governance benefits of versioned AEP Resources.

Experiments should compare declared configurations; they must not dynamically mutate production resources or automatically promote a winner solely from an LLM-generated judgment.

## Deliverable

Implement a versioned experiment definition and runner that executes baseline and candidate Resource configurations against the same evaluation suite, computes deterministic comparative metrics, and persists an inspectable ExperimentResult.

Support overrides for versioned Agent, Prompt, Model, Context/Knowledge configuration, Evaluation, and Policy resources where compatible with the selected workflow.

## Dependencies

* AEP-052
* AEP-055
* AEP-057
* AEP-060

## Acceptance Criteria

* An Experiment declares one immutable baseline, one or more immutable candidates, one EvaluationSuite version, and the metrics used for comparison.
* Baseline and candidates run against the identical deterministic case set and source revisions; case drift between arms is rejected.
* ExperimentResult reports absolute and relative differences for configured quality metrics, pass rates, token usage, cost when available, latency, and invocation counts.
* Case-level evidence remains linked to the underlying WorkflowExecutions and EvaluationResults so aggregate differences can be investigated with AEP-056.
* A candidate can override selected versioned Resources without modifying baseline resources or historical executions.
* Failed or incomplete arms are represented explicitly and are not silently removed from aggregate comparisons.
* The runner can operate with deterministic fixture model providers for credential-free tests.
* Experiment execution performs no publication or other external side effects by default.
* Tests cover two-arm comparison, multiple candidates, incompatible override rejection, partial failure, deterministic aggregation, and metric regression/improvement calculations.
* Documentation defines how experiment evidence can inform a later explicit resource-version promotion without automatically changing production configuration.
