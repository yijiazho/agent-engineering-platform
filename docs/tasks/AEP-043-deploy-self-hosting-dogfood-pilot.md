# AEP-043: Deploy Self-Hosting Dogfood Pilot

Status: Completed

Summary
- Deploy an initial self-hosted dogfood pilot of the platform for internal use.

Implementation
- Deployment and runtime modules: src/aep/dogfood_deployment.py, src/aep/dogfood_runtime.py
- Operations guide: docs/operations/self-hosting-dogfood.md

Manual Testing
- Pilot deployment validated on 2026-08-28; startup, basic workflows, and teardown exercised without issues.

Acceptance Criteria
- Dogfood environment can be brought up and used to process representative issues end-to-end.
- Operations documentation is sufficient to reproduce the deployment.

Notes
- Status moved to Completed after manual pilot verification.