# Codex Feature Request Template

Use this template when starting a new Codex thread for Doctor Collector feature
work. Start the thread from the repository root so repo-scoped skills, agents,
and `AGENTS.md` are visible.

For token efficiency, use the smallest prompt that matches the risk. Tiny tasks
should rely on automatically loaded `AGENTS.md`; use the workflow skill for
material work where the extra context is worth it.

## Tiny Or Low-Risk Prompt

Use this for typos, small docs edits, obvious one-file fixes, or focused test
adjustments where no scraping, contact, config, CSV/state, Docker, or safety
surface is likely to change.

```text
Small fix:
<Describe the task in one or two sentences.>

Follow AGENTS.md only. Keep context narrow, read only the relevant files first,
and use rg to locate references. Do not invoke $doctor-change-workflow, read
workflow docs, or spawn subagents unless you find material risk. Run the focused
check that proves the change.
```

## Standard Feature Prompt

Use this for material feature work, nontrivial bug fixes, and changes that need
the repo's full plan/spec/verify/review loop.

```text
$doctor-change-workflow

Feature request:
Add <feature>.

Goal:
<Describe the user-visible behavior that should exist when this is done.>

Non-goals:
<List behavior, refactors, config/env vars, Docker behavior, CSV/state changes,
scraping behavior, contact behavior, or release work that should not change.>

Token mode:
- Keep context narrow; use rg and read only relevant files first.
- Do not spawn reviewers unless the diff is material.
- Use doctor-safety-reviewer only when the touched surface matches its
  safety/integration scope.
- Summarize long command output instead of pasting logs.

Important constraints:
- Follow AGENTS.md and docs/CODEX_WORKFLOW.md.
- Keep the implementation narrow and consistent with existing Python, async
  httpx, BeautifulSoup, Pydantic, CSV/state, SMTP, Docker, and pytest patterns.
- Preserve the collect-first, review-CSV, contact-second safety workflow.
- Do not add browser automation, scraping escalation, CAPTCHA bypasses,
  analytics, telemetry, new persistence formats, new services, new
  dependencies, MCP, A2A, or extra agents unless you explain why and ask first.
- Use the router-first workflow: the main Codex agent implements and only
  escalates to specialist reviewers when risk or task complexity warrants it.

Acceptance checks:
- <Concrete behavior 1>
- <Concrete behavior 2>
- <README/config/Docker/docs expectation, if any>

Verification:
Run the narrowest meaningful checks first, then broaden according to
docs/CODEX_WORKFLOW.md. Prefer behavior-oriented tests; use mocks for external
boundaries, not for the logic being proved. After implementation, spawn the
appropriate review agent before finalizing if the diff is material.
```

## Safety-Sensitive Feature Prompt

Use this when the change touches therapie.de scraping, rate limits, HTML
parsing, filtering, CSV/state persistence, SMTP/contact behavior, credentials,
config/env vars, Docker, release behavior, or live verification.

```text
$doctor-change-workflow

Feature request:
Add <feature>.

Goal:
<Describe the desired behavior and affected scraping, filtering, CSV/state,
contact, config, Docker, or release surface.>

Non-goals:
<List therapie.de behavior, contact behavior, credentials, config keys,
CSV/state files, Docker behavior, or release work that should stay unchanged.>

Safety/integration-sensitive surfaces:
- <therapie.de / rate limits / HTML parsing / filtering / CSV / state / SMTP /
  credentials / config / Docker / release / live verification>

Token mode:
- Keep context narrow; read only the relevant client, service, config, tests,
  docs, and Docker files first.
- Use exactly the listed reviewers; do not spawn extra agents unless a new
  concrete risk appears.
- Summarize long command output instead of pasting logs.

Important constraints:
- Follow AGENTS.md and docs/CODEX_WORKFLOW.md.
- Preserve the collect-first, review-CSV, contact-second workflow unless
  explicitly approved otherwise.
- Do not send real emails during implementation or verification without
  explicit approval.
- Do not run live therapie.de probes without explicit approval.
- Do not commit real credentials, real collected therapist data, or generated
  contact state.
- Preserve request pacing and rate-limit handling when scraping behavior
  changes.
- Do not add browser automation, CAPTCHA bypasses, aggressive concurrency,
  analytics, telemetry, new dependencies, MCP, A2A, dynamic registries, or
  extra agents without asking first.

Acceptance checks:
- <Concrete behavior 1>
- <Concrete behavior 2>
- <Data/safety/config/Docker check>

Verification:
Run focused tests first. Run Ruff and the relevant pytest suite. Run Docker or
manual CLI checks only when the touched surface justifies it. Avoid over-mocked
tests that would pass while the real therapie.de parsing, filtering, CSV/state,
SMTP/contact, config, or Docker behavior is broken.

Review routing:
Spawn doctor-code-reviewer for material diffs. Also spawn
doctor-safety-reviewer for this safety/integration-sensitive change. Summarize
findings and fix confirmed issues before finalizing.
```
