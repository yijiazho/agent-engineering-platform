# Agent Engineering Platform

A framework and reference implementation for building, testing, and operating AI agents with reproducible workflows and automated evaluation.

## Repository Layout

```
.
├── deploy/
│   ├── local/
│   ├── self-hosting/
│   └── validation/
├── docs/
├── schemas/
├── fixtures/
├── skills/
├── src/
└── tests/
```

- deploy/: Deployment assets used for local development, self-hosting pilots, and validation
  - local/: Local docker-compose and development images
  - self-hosting/: Self-hosted pilot deployment assets and secrets guidance
  - validation/: Offline/locked images and materials for validation runs
- docs/: Documentation and architecture notes
- schemas/: JSON/YAML schemas and data contracts
- fixtures/: Sample data, snapshots, and test fixtures
- skills/: Packaged skills/modules for agents
- src/: Source code
- tests/: Test suites

## Development

- See docs/ for architecture, tasks, and contribution guidance.
- Use tests/ to validate changes locally before opening a pull request.
- No runtime behavior or deployment configuration is modified by this change; it only updates the documented repository layout.
