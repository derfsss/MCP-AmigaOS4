<!-- Thanks for the contribution. See CONTRIBUTING.md for the
     full set of conventions. Below is a checklist; delete sections
     that don't apply. -->

## Summary

What does this change do, and why?

## Changes

-
-
-

## Testing

How did you verify it works? At minimum:

- [ ] `cd host && uv run pytest -q` passes
- [ ] `cd host && uv run ruff check src tests` clean
- [ ] `cd host && uv run mypy src` clean

For changes that touch the daemon:

- [ ] `cd mcpd && make docker-build` succeeds
- [ ] Verified live on a target (note which: real X5000 / A1222 /
      QEMU pegasos2 / ...)

For new tools / methods:

- [ ] Documented in `COMMANDS.md` (and `USAGE.md` if user-facing)
- [ ] Unit test added under `host/tests/unit/`
- [ ] `CHANGELOG.md` entry under `## Unreleased`

## Breaking changes?

If yes, describe what breaks and how a user should migrate.
