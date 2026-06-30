# Doctor Collector Agent Guide

These instructions apply to the whole repository.

## Project Shape

- Python 3.11+ CLI package for collecting therapist contacts from therapie.de
  and optionally contacting them by SMTP.
- Core package: `src/doctor_collector/`.
- Tests: `tests/`.
- Configuration: `config.yaml`, env var overrides in `src/doctor_collector/config.py`.
- Runtime outputs: `therapists.csv` and `.contacted_therapists.json`; avoid
  committing real user data or contact state.

## Commands

Run these when code or tests change:

- `python -m ruff check src/ tests/`
- `python -m pytest tests/ --tb=short`

For Docker or packaging changes, also verify the relevant Docker command or
document why it could not run.

Live therapie.de probes, SMTP sends, network checks, and commands using real
credentials require explicit approval and must use safe test data.

## Safety And Product Constraints

- Do not send real emails from tests or agent verification. Keep SMTP tests at
  the boundary unless the user explicitly approves a live send.
- Treat therapist contact details, user postal codes, SMTP credentials, CSV
  files, and state files as sensitive local data.
- Do not commit real credentials, real patient/user data, collected therapist
  data, or generated contact-state files.
- Preserve the collect-first, review-CSV, contact-second workflow unless the
  user explicitly asks for a different safety model.
- Respect therapie.de rate limits and pacing. Do not add browser automation,
  scraping escalation, CAPTCHA bypasses, or aggressive concurrency without
  explicit approval.
- Do not add new external services, notification channels, persistence formats,
  analytics, telemetry, MCP servers, A2A-style protocols, or extra agents
  unless there is a repeated need and the user approves.

## Code Style

- Follow existing async `httpx`, BeautifulSoup, Pydantic, pytest, and Ruff
  patterns.
- Keep changes small and reviewable. Prefer local helpers over broad refactors.
- Use `Field(default_factory=...)` for mutable Pydantic defaults.
- Prefer typed, explicit data flow over ad hoc dictionaries when models already
  exist.
- Keep CLI output useful for non-technical users and avoid logging secrets.

## Tests

- Prefer behavior-oriented tests that exercise real config, parsing, filtering,
  CSV/state, retry/rate-limit, and contact-selection behavior.
- Use `httpx.MockTransport` or focused fakes for external network boundaries,
  but do not mock away the logic the test is meant to prove.
- Do not use real therapie.de, SMTP providers, or credentials in routine tests.
- For parser changes, include representative HTML snippets that prove the real
  selectors/decoding behavior.
- For state or CSV changes, use `tmp_path` and assert persisted content.
- For contact flow changes, prove already-contacted filtering and successful
  contact state updates without sending real email.
- Parametrize when three or more tests follow the same pattern.

## Documentation

Update README or config examples when behavior, commands, env vars, Docker,
configuration, CSV/state files, or contact workflow changes.

## Codex Operating Workflow

- For material feature, bug, refactor, scraping, filtering, contact, config,
  Docker, release, privacy, safety, or developer workflow changes, follow
  `docs/CODEX_WORKFLOW.md`.
- Start hard or ambiguous work in plan mode, or explicitly ask for an
  exploration-and-plan pass before edits. Skip ceremony for one-line fixes.
- Use a lightweight spec-first loop for material changes: goal, non-goals,
  affected files, acceptance checks, task list, implementation, verification,
  fresh-context review, and docs decision.
- Keep one Codex thread per task. Clear or start fresh after repeated failed
  corrections or when switching to unrelated work.
- Prefer worktree isolation for parallel/background tasks so generated edits do
  not collide with the main checkout, `config.yaml`, runtime CSV, or state
  files.
- Use the repo-local router-first topology from `docs/CODEX_WORKFLOW.md`: the
  main Codex agent routes normal work and escalates to specialist review only
  when risk or task complexity warrants it.
- Use project-scoped subagents when their focused context helps:
  - `doctor-code-reviewer`: fresh-context review of diffs for correctness,
    Python architecture, regressions, scope drift, missing tests, and docs.
  - `doctor-safety-reviewer`: focused review for therapie.de scraping,
    rate limits, CSV/state, SMTP/contact behavior, secrets, Docker, and live
    verification risk.
- Invoke `$doctor-change-workflow` for repeatable feature, bug, refactor,
  scraping, filtering, contact, config, docs, Docker, release, safety, or
  workflow tasks.
- Verification order: run the narrowest relevant tests first, then broaden to
  Ruff and the full pytest suite when code changes.
- Always include command evidence or explain why a check could not be run.
- Do not use subagents as a replacement for tests or human review; use them to
  find gaps before the final response or PR.
