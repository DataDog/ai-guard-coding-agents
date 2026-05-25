# Contributing

Thanks for your interest in AI Guard for Coding Agents. We welcome bug reports,
feature requests, and pull requests.

## Filing issues

- Search [existing issues](https://github.com/DataDog/ai-guard-coding-agents/issues) before
  opening a new one.
- Use the bug report or feature request template. Please include enough context
  for someone else to reproduce or evaluate the request.
- Do **not** file security vulnerabilities as public issues. See
  [Reporting security issues](#reporting-security-issues) below.

## Reporting security issues

If you believe you've found a security vulnerability, please follow the
disclosure process at <https://www.datadoghq.com/security/>. Do not open a
public GitHub issue.

## Development setup

```bash
# Python 3.11+ and uv (https://docs.astral.sh/uv/) are required.
uv sync --extra test --extra build

# Run the unit and integration suites (skips binary-marker tests if no binary).
uv run pytest -q

# Build the single-file executable.
uv run pyinstaller ai-guard.spec

# Then run the binary smoke tests.
uv run pytest -m binary -v

# Lint and format.
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

See [`AGENTS.md`](AGENTS.md) for an in-depth walkthrough of the codebase and
the conventions we follow.

## Pull requests

1. Fork the repository and create a topic branch from `main`.
2. Make your change. Keep diffs focused: one logical change per PR.
3. Add or update tests so the suite covers the new behaviour. CI runs
   `ruff check`, `ruff format --check`, and the full pytest suite on Linux,
   macOS, and Windows; see [`.github/workflows/test.yml`](.github/workflows/test.yml).
4. Open the PR against `main` and fill in the pull request template.
5. **Use a Conventional Commits PR title** (e.g. `feat: add proxy retry`,
   `fix: handle ECONNRESET`, `chore: bump linter`). PRs are squash-merged
   and the PR title becomes the commit subject; release automation parses
   those subjects to compute version bumps and the CHANGELOG. See
   [Release process](#release-process) below for the bump table.
6. By submitting a PR you agree to license your contribution under the
   [Apache 2.0 License](LICENSE) that covers this repository.

## Adding third-party dependencies

If your change pulls in a new runtime dependency, please update
[`LICENSE-3rdparty.csv`](LICENSE-3rdparty.csv) in the same PR so the third-party
inventory stays accurate. Only Apache-2.0-compatible licenses are accepted
(Apache-2.0, MIT, BSD-2-Clause, BSD-3-Clause, ISC, and similar permissive licenses).

## File header

Every new source file should carry the standard Datadog header:

```
Unless explicitly stated otherwise all files in this repository are licensed
under the Apache 2.0 License.

This product includes software developed at Datadog (https://www.datadoghq.com/).
Copyright 2026-Present Datadog, Inc.
```

Use the comment syntax for the file's language.

## Release process

Releases are automated via
[release-please](https://github.com/googleapis/release-please-action),
configured in [`release-please-config.json`](release-please-config.json) and
driven by [`.github/workflows/release.yml`](.github/workflows/release.yml).

How it works:

1. Every push to `main` triggers `release.yml`, which runs `release-please`.
2. `release-please` inspects conventional-commit messages since the last
   release tag and maintains a long-lived release PR titled
   `ci: release X.Y.Z` that bumps `pyproject.toml`, `src/aiguard/__init__.py`,
   and `.release-please-manifest.json`, and updates `CHANGELOG.md`.
3. **Merging that PR** cuts a `vX.Y.Z` git tag, creates a GitHub Release
   with the generated notes, and invokes the build job — which builds the
   three platform tarballs and attaches them (plus `.sha256` sidecars) to
   the Release. [`scripts/install.sh`](scripts/install.sh) downloads from
   the Release assets.

Version bump table (pre-1.0):

| Commit prefix                       | Bump  | Example         |
| ----------------------------------- | ----- | --------------- |
| `fix:` / `perf:`                    | patch | `0.1.0 → 0.1.1` |
| `feat:`                             | minor | `0.1.0 → 0.2.0` |
| `feat!:` / `BREAKING CHANGE:` body  | minor | `0.1.0 → 0.2.0` |
| `chore:` / `docs:` / `ci:` / `test:`/ `refactor:` / `style:` | none  | no release      |

Pre-1.0 behaviour comes from `bump-minor-pre-major: true`. Once the project
is ready for `1.0.0`, cut it explicitly with a `Release-As: 1.0.0` trailer
in any commit body and merge to `main`.

Manual recovery: if `release-please` is broken, you can still cut a release
by hand — push a `v*` tag (`git tag v0.2.0 && git push origin v0.2.0`),
create the GitHub Release manually, and `build.yml`'s tag-push trigger
will build and attach the assets.