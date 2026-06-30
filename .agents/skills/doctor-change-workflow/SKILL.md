---
name: doctor-change-workflow
description: Use for Doctor Collector feature work, bug fixes, refactors, therapie.de scraping/rate-limit changes, filtering changes, CSV/state changes, SMTP/contact changes, config/env var changes, Docker/release work, docs, safety/privacy changes, and developer workflow changes that need the repo's plan/spec/verify/review loop.
---

# Doctor Collector Change Workflow

Use this skill to keep material Doctor Collector changes scoped, verified, and
reviewable without adding ceremony to small edits.

## 1. Load The Local Contract

Before editing, read the relevant parts of:

- `AGENTS.md`
- `docs/CODEX_WORKFLOW.md`
- `pyproject.toml`
- The files directly involved in the requested change

If the request touches therapie.de scraping, rate limits, HTML parsing,
filtering, CSV/state persistence, SMTP/contact behavior, credentials, config,
Docker, or release behavior, treat it as safety/integration-sensitive.

## 2. Scope Before Editing

For material changes, write a short plan in the thread:

- Goal
- Non-goals
- Files likely to change
- Acceptance checks
- Test/verification commands
- README/config/Docker/docs impact

Ask one concise clarifying question only when a wrong assumption would create
user-visible behavior, lost CSV/state data, leaked secrets, unintended email
sending, aggressive scraping, or release risk.

## 3. Implement Narrowly

Follow existing Python, async `httpx`, BeautifulSoup, Pydantic, CSV/state,
SMTP, Docker, and pytest patterns. Keep changes close to the requested
behavior. Do not add browser automation, scraping escalation, CAPTCHA bypasses,
new notification providers, new persistence formats, analytics, telemetry, new
services, or new dependencies without explicit user approval.

## 4. Route Specialist Review Intentionally

Use a repo-local, router-first topology:

- The main Codex agent owns implementation and decides whether focused review
  is needed.
- For most material diffs, route once to `doctor-code-reviewer`.
- Add `doctor-safety-reviewer` only for therapie.de scraping, rate limits,
  HTML parsing, filtering, CSV/state, SMTP/contact, credentials, config,
  Docker, release, or live verification risk.
- Treat each subagent TOML as the agent card: it defines the agent's role,
  capabilities, required context, and expected findings format.
- Keep subagents as direct children. Do not introduce MCP, A2A, registries, or
  extra orchestration for normal repo work.

Use supervisor-style coordination only when a task has multiple dependent
review streams that must be synthesized before finalizing.

## 5. Verify With Evidence

Run the narrowest meaningful checks first. Prefer this order:

1. Focused tests for the changed module or behavior.
2. `python -m ruff check src/ tests/`
3. `python -m pytest tests/ --tb=short`
4. Docker/manual CLI checks only when Docker, packaging, CLI, config examples,
   or runtime behavior changed.
5. Live therapie.de probes or SMTP sends only with explicit user approval and
   safe test data.

Before treating new tests as evidence, check that they assert observable
behavior and would fail if the changed logic were broken. Use mocks for
external network, SMTP, clock, or filesystem boundaries, not for the code path
being proved.

Report exact commands and results. If a check cannot run, explain why.

## 6. Review Before Finalizing

For material changes, spawn `doctor-code-reviewer` for a fresh-context diff
review. For safety/integration-sensitive changes, also spawn
`doctor-safety-reviewer`. Fix confirmed findings, then rerun relevant checks.

Update README, config examples, Docker docs, and workflow docs when behavior,
commands, env vars, output files, safety posture, or developer workflow changes.
