# Codex Workflow

This guide defines the default agent-assisted software engineering workflow for
Doctor Collector. It keeps Codex fast for small fixes and disciplined for work
that can affect therapie.de scraping, filtering, CSV/state files, SMTP contact
behavior, config/secrets, Docker, release quality, or safety.

## Default Loop

1. **Scope**
   - State the goal, non-goals, constraints, and "done when" checks.
   - Mention relevant files, failing tests, logs, CSV/config examples, or
     therapie.de/SMTP symptoms when known.
   - For one-line or obvious fixes, go straight to implementation.

2. **Explore And Plan**
   - For ambiguous or multi-file changes, start in plan mode or ask Codex to
     explore before editing.
   - Let Codex read the relevant app, test, docs, config, Docker, and CI files.
   - Produce a short plan with affected files and verification commands.

3. **Use A Lightweight Spec**
   - For material work, keep a brief spec in the thread or task document:
     goal, non-goals, acceptance checks, risks, and task list.
   - This is the useful part of OpenSpec-style work: agree on behavior before
     writing code without adding ceremony to small edits.
   - If the team later adopts OpenSpec itself, map this loop to
     `explore -> propose -> apply -> archive`.

4. **Implement Narrowly**
   - Follow existing Python, async `httpx`, BeautifulSoup, Pydantic, CSV/state,
     SMTP, Docker, and pytest patterns.
   - Prefer small, reviewable changes over broad refactors.
   - Do not add browser automation, scraping escalation, CAPTCHA bypasses,
     analytics, telemetry, new persistence formats, new services, or new
     dependencies without explicit approval.

5. **Verify With Evidence**
   - Run the narrowest meaningful checks first, then broaden as risk grows.
   - Report exact commands and outcomes in the final response.
   - If live network, SMTP, Docker, or environment access blocks a check, say so.
   - Do not run live therapie.de probes or SMTP sends without explicit approval.

6. **Fresh-Context Review**
   - For material changes, ask Codex to spawn `doctor-code-reviewer`.
   - For therapie.de scraping, rate limits, HTML parsing, filtering, CSV/state,
     SMTP/contact, credentials, config, Docker, release, or live verification
     changes, also spawn `doctor-safety-reviewer`.
   - Treat subagent findings as review input, not as a replacement for tests.

7. **Docs**
   - Update README for user-facing behavior, config/env vars, Docker commands,
     CSV/state files, contact workflow, or developer commands.
   - Preserve and update comments in `config.yaml` when keys change.
   - Do not add release-note noise unless a changelog is introduced later.

## When To Use Each Codex Surface

- **Prompt/thread context:** one-off task constraints and task-specific
  decisions.
- **`AGENTS.md`:** durable repo rules, safety constraints, verification, and
  documentation expectations.
- **`.codex/agents/`:** focused project subagents for review and safety risk.
- **`.agents/skills/`:** repeatable workflows that should be reusable across
  Codex sessions.
- **`.codex/config.toml`:** repo-scoped Codex settings such as bounded subagent
  fan-out.
- **Worktrees:** parallel or background tasks where edits should not collide
  with the main checkout, `config.yaml`, `therapists.csv`, or state files.

## Agent Topology Decisions

These decisions adapt multi-agent architecture principles to this repo without
adding platform complexity.

- **Router first:** the main Codex agent handles normal scoping,
  implementation, verification, and final synthesis.
- **Specialists on demand:** route material diffs to `doctor-code-reviewer`.
  Add `doctor-safety-reviewer` only for therapie.de scraping, rate limits,
  HTML parsing, filtering, CSV/state, SMTP/contact, credentials, config,
  Docker, release, or live verification risk.
- **Supervisor only when needed:** use supervisor-style coordination only for
  multi-step work where separate specialist reviews must be sequenced or
  reconciled. Most tasks should need one main agent and at most one reviewer.
- **Repo-local graph, not decentralized mesh:** keep the workflow inside this
  repository with direct child subagents. Do not introduce A2A protocols,
  dynamic agent registries, external orchestration services, or extra MCP
  servers for routine development.
- **Agent cards as contracts:** each `.codex/agents/*.toml` file is the
  practical agent card: name, role, capabilities, trigger conditions, required
  context, and output format.
- **MCP boundary:** use MCP only when an external tool or data source is
  genuinely needed; do not use it as the normal communication layer between
  repo-local Codex agents.

## Project Subagents

Use these explicitly when the task warrants it:

```text
Spawn doctor-code-reviewer to review the current diff for correctness,
regressions, missing tests, architecture issues, scope drift, and docs gaps.
Wait for the result and summarize findings before finalizing.
```

```text
Spawn doctor-safety-reviewer to review this change for therapie.de scraping,
rate limits, HTML parsing, filtering, CSV/state behavior, SMTP/contact safety,
secrets, config/env vars, Docker behavior, and missing live/manual checks.
```

Good review prompts name the plan, changed files, and what counts as a finding.
Ask reviewers to flag correctness, coverage, safety, integration, security,
data-loss, and verification gaps rather than style preferences.

## Verification Matrix

Use the smallest check that proves the change, then broaden when the touched
surface is shared or risky.

- Normal code changes: `python -m ruff check src/ tests/` and
  `python -m pytest tests/ --tb=short`.
- Module-specific changes: start with focused tests, then run the full local
  suite above.
- therapie.de scraping, HTML parsing, profile extraction, pagination,
  rate-limit, retry, or URL construction changes: focused client/parser tests
  with representative HTML or `httpx.MockTransport`, then the full local suite.
  Live probes require explicit approval.
- Filtering changes: tests for include/exclude behavior, empty email handling,
  and contacted-email exclusions where relevant.
- CSV/state changes: tests using `tmp_path` that verify output columns,
  encoding/newline behavior, state compatibility, corrupt-state behavior when
  touched, and no accidental deletion.
- SMTP/contact changes: prove enabled/disabled logic, message construction,
  partial-failure behavior, and state updates without sending real email.
- Config/env var changes: test YAML loading, env overrides, placeholder
  resolution, validation errors, README/config examples, and secret handling.
- Docker/release changes: verify build/runtime commands and README examples.
- Test quality: prefer observable behavior over implementation call assertions.
  Mock external network, SMTP, clock, filesystem, or service boundaries when
  useful, but do not mock away the code path the change is meant to prove.

## Failure Patterns To Avoid

- Long kitchen-sink threads mixing unrelated tasks.
- Specs that drift away from code without verification.
- Chasing every subagent nit instead of correctness and requirement gaps.
- Trusting generated tests that only mirror the implementation.
- Over-mocked tests that would still pass if the real parsing, filtering,
  CSV/state, SMTP/contact, config, Docker, or integration path were broken.
- Running live therapie.de probes or SMTP sends without explicit approval.
- Breaking the collect-first, review-CSV, contact-second safety workflow.
- Committing real collected therapist data, contact state, or credentials.
