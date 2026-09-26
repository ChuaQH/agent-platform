# Portfolio release checklist

No tag, GitHub release, deployment, or public-repository action is performed
by this checklist. Make those decisions explicitly after the checks pass.

## Clean, reproducible checkout

- [ ] Start from a clean checkout; copy `.env.example` to `.env` only for a
  local app demo, never for scripted tests or CI.
- [ ] Confirm Python 3.11 and Node.js 20, then install with
  `python -m pip install -e ".[dev]"` and `npm ci` in `apps/assistant-web`.
- [ ] Treat `package-lock.json` as the frontend lock; Python currently uses
  the declared `pyproject.toml` constraints and has no separate lockfile.
  Record `python -m pip check` with the release verification instead of
  representing Python resolution as pinned.
- [ ] Start the development database separately from the test database.
- [ ] Run `python -m alembic upgrade head`; confirm `python -m alembic heads`.

## Verification

- [ ] `python scripts/check_imports.py`
- [ ] `python -m pip check`
- [ ] `python -m pytest -m "not integration"`
- [ ] Start only the disposable test service, set `ACA_TEST_DATABASE_URL` to
  exactly `agent_platform_test`, then run `python -m pytest -m integration`.
- [ ] `npx tsc --noEmit`, `npm test`, and `npm run build` in
  `apps/assistant-web`.
- [ ] Run the scripted control, React/plan-react strategy, and v1.3 recovery
  commands from `docs/AGENT_CONTROL_EVALUATIONS.md`. Live-model evaluation is
  optional, explicitly invoked, and never a release gate.
- [ ] `git diff --check`.

## Security and documentation

- [ ] Manually inspect staged changes and `.env` handling; search for leaked
  credentials without printing any discovered value.
- [ ] Review [THREAT_MODEL.md](THREAT_MODEL.md), especially residual risks and
  external-side-effect ambiguity.
- [ ] Validate README and documentation links locally.
- [ ] Follow [DEMO_WALKTHROUGH.md](DEMO_WALKTHROUGH.md) with a local demo
  account and non-sensitive workspace content.
- [ ] Capture the five real screenshots in the demo checklist; replace the
  README placeholder only after redaction review.

## Release decision and rollback

- [ ] Decide version/tag and public-release scope; do not infer either from CI.
- [ ] Record known limitations, dependency-audit findings, and whether an
  optional live evaluation was run.
- [ ] Re-run both `npm audit --json` and `npm audit --omit=dev --json`.
  The Phase 6B review leaves the development-only `vitest` 3.2.6 /
  `@vitest/mocker` moderate advisory as a deliberate residual risk: the
  production test command is `vitest run` (not its UI server), and npm offers
  only the breaking Vitest 5 upgrade. Re-evaluate that upgrade before a later
  dependency refresh; do not represent this as a production-runtime finding.
- [ ] For a rollback, deploy the prior application artifact only after checking
  migration compatibility. Do not downgrade a database casually: back up and
  review the relevant Alembic downgrade before any destructive action.
