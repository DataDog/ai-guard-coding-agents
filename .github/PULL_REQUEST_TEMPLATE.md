<!--
Thanks for sending a pull request! Please make sure you've read
CONTRIBUTING.md and filled in the sections below.
-->

## What does this PR do?

<!-- A short description of what changes and why. -->

## Motivation

<!-- The user-facing problem, internal pain point, or upstream issue this addresses. -->

## How was it tested?

<!-- Manual steps, `pytest` invocations, smoke tests, or a description of the
     scenario you ran end-to-end. -->

## Checklist

- [ ] Tests cover the new behaviour (or an explanation of why they don't).
- [ ] `uv run ruff check src/ tests/` and `uv run ruff format --check src/ tests/` pass.
- [ ] `uv run pytest -q` passes locally.
- [ ] If a new runtime dependency was added, `LICENSE-3rdparty.csv` is updated.
- [ ] New source files include the Datadog header (see [CONTRIBUTING.md](../CONTRIBUTING.md)).