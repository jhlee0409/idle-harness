# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Idle Harness — a GAN-inspired 3-agent system that autonomously builds full-stack web apps from a single prompt. Based on [Anthropic's harness design article](https://www.anthropic.com/engineering/harness-design-long-running-apps).

## Commands

```bash
# Run tests
.venv/bin/python -m pytest tests/ -v

# Run a single test
.venv/bin/python -m pytest tests/test_orchestrator.py::test_verdict_pass_strict_match -v

# Run the harness
python orchestrator.py "your app idea"

# Other commands
python orchestrator.py serve          # Start last-built app
python orchestrator.py clean          # Clean comms/ staging
python orchestrator.py clean --all    # Clean comms/ + output/
python orchestrator.py --setup        # Interactive dependency setup
```

## Architecture

Three agents orchestrated sequentially: **Planner → Generator → Evaluator**, with a retry loop around Generator+Evaluator.

```
orchestrator.py  →  call_agent() in cli.py  →  Claude Agent SDK  →  claude-opus-4-6
```

### Agent Flow (simple mode — default, per article's Opus 4.6 recommendation)

1. **Planner** reads `agents/frontend-design-skill.md` then generates `comms/spec.md`
2. **Evaluator** generates `comms/testable_criteria.md` — concrete, interaction-level test criteria from the spec
3. **Generator reviews criteria** (`_review_criteria()`) — reads and acknowledges criteria before building, starts continuous session
4. **Generator** builds entire app in `output/{slug}/` with TOOLS_FULL, using criteria as checklist
5. **Smoke test** — HTTP health check before expensive eval. If app crashes, skip eval and feed error directly to Generator
6. **Evaluator** tests running app via Playwright MCP. **Parallel evaluation**: criteria split by `###` section into 3 evaluator agents (1 Opus lead + 2 Sonnet feature) running concurrently via `asyncio.gather()`. Lead handles Visual Design + Quality Assessment. Results merged into single evaluation.md with consolidated Required Changes. Falls back to single evaluator for small criteria sets. Receives previous evaluation for regression comparison (REGRESSION/FIXED/PERSISTENT labels). Empty response (0 criteria) triggers auto-retry.
7. On FAIL: feedback (with score trajectory) → Generator retries. Regression detection resets Generator session on >20pp score drop. Consecutive crashes trigger cooldown (60s exponential backoff). Timeout/InfraError stops retries immediately.
8. If backend passes but frontend design fails → **design refinement loop** (up to 10 iterations)
9. `comms/` artifacts archived to `output/{slug}/.harness/`

### Agent Flow (full mode — sprint decomposition)

1. **Planner** reads `agents/frontend-design-skill.md` then generates `comms/spec.md`
2. Per sprint: **Generator** proposes contract → **Evaluator** reviews → agree or iterate (max 3 rounds)
3. **Generator** builds in `output/{slug}/` with TOOLS_FULL
4. **Evaluator** tests running app via Playwright MCP with TOOLS_EVALUATOR
5. On FAIL: feedback → Generator retries (max 10 attempts, continuous session via `session_id`). Timeout/InfraError stops retries immediately.
6. After all sprints: integration eval with build-fix-eval loop (Generator fixes → Evaluator re-tests)
7. `comms/` artifacts archived to `output/{slug}/.harness/`

### Key Design Decisions

- **Evaluator cannot read source code** — `TOOLS_EVALUATOR = ["Write"]` in config.py. Spec and contract content are passed in the prompt, not read from files. MCP tools (Playwright) are added automatically by the SDK when `mcp_servers` is passed — they bypass the `allowed_tools` filter. This enforces the GAN principle.
- **Generator maintains continuous session** — `session_id` from SDK init messages is reused across retry attempts. Reset on crash (`AgentError` resets `_generator_session_id`).
- **MCP is SDK-managed** — Playwright MCP launched via `mcp_servers` in `ClaudeAgentOptions`, not user-configured `.mcp.json`.
- **Timeout stops retries** — `AgentTimeout` is caught separately from `AgentError` in the retry loop. Timeouts indicate the agent is stuck (hanging command, infinite loop), so retries are stopped immediately rather than wasting build cycles.
- **`comms/` is staging, not persistence** — Wiped on each `setup()`. Project artifacts archived to `output/{slug}/.harness/` after completion.
- **Verdict/contract parsing uses regex** — `_check_verdict_pass()` matches `^#{0,3}\s*Verdict:\s*PASS\s*$` to prevent false positives. `_check_contract_agreed()` matches `^AGREED\b`.
- **Evaluator PASS is validated** — Orchestrator parses evaluation for automation-limited ratio; >10% skipped criteria overrides PASS to FAIL. Canvas drag/interactions are NOT automation-limited (testable via dispatchEvent or API).
- **Evaluation preservation** — After eval, orchestrator compares disk-written evaluation (via Write tool) vs agent text response. Keeps whichever has more criteria, preventing data loss when Evaluator writes detailed eval to disk but returns a summary.
- **Previous evaluation comparison** — On retry, Evaluator receives the previous evaluation and must label regressions (was PASS, now FAIL), fixes, and persistent failures. Required Changes include blast radius and priority.
- **Smoke test** — Post-build HTTP health check (`_smoke_test()`) before expensive Evaluator. Catches app crashes in 5 seconds vs 30+ minutes. Failure is written to evaluation.md as direct feedback.
- **Regression detection** — `_check_regression()` tracks eval scores in state.json. Resets Generator session on >20pp score drop from best, or three consecutive drops. Prevents context saturation.
- **No attempt limiting** — Every build-eval cycle runs to completion. Optimizes for first-time PASS, not per-run cost savings. Re-running is always more expensive than extra attempts.
- **Score trajectory in feedback** — Generator receives eval score history ("30% → 40% ↑ 10pp") to inform REFINE/PIVOT decisions with actual data.
- **Parallel eval model tiering** — Lead evaluator (Quality Assessment) uses Opus, feature evaluators use Sonnet. Reduces API rate limit pressure while maintaining quality where it matters.
- **Consolidated Required Changes** — After parallel merge, all evaluators' Required Changes are collected into one section. Generator sees one prioritized list, not N scattered lists.
- **Crash cooldown** — After 3+ consecutive `AgentError`/`RuntimeError`, exponential backoff (60s, 120s, 240s, cap 300s) before retry. Prevents burning attempts against a temporary infrastructure issue.
- **Playwright cleanup** — `_cleanup_playwright()` kills zombie Chromium/Playwright processes after each parallel eval to prevent resource accumulation.
- **Criteria review** — `_review_criteria()` in simple mode: Generator reads and acknowledges criteria before building, starting continuous session with criteria context.
- **Generator writes self_eval.md** — Mandatory self-evaluation with actual verification (curl output, build results). Orchestrator logs discrepancy if self-eval claims >95% but last Evaluator scored <80%.
- **Automation-limited feedback loop** — Items the evaluator couldn't test are extracted and passed back to the generator on retry with explicit self-test instructions.
- **Criteria generation has minimum bound** — If evaluator produces <10 criteria, harness raises RuntimeError. No upper cap or artificial limits — count depends on app complexity.
- **Evaluator gets spec in simple mode** — Product spec (with Visual Design Language) is passed inline alongside testable criteria so evaluator can assess design quality against the original design direction.
- **Contract is cached in memory** — `_cached_contracts` dict initialized in `__init__`, caches contract text on first read to prevent generator from modifying criteria between retries (GAN integrity).
- **Design refinement includes contract** — Generator receives testable criteria during design refinement so it knows which specific frontend criteria the Evaluator will re-test.

### File Roles

- `orchestrator.py` — Orchestration + CLI + preflight + logging + parallel eval (single file)
- `cli.py` — `call_agent()` wrapper around `claude_agent_sdk.query()`. Returns `AgentResult` with session_id, tokens, cost. Exports `fmt_tokens()` and `fmt_elapsed()` shared formatters.
- `config.py` — `CONFIG` dict, tool lists (`TOOLS_READ_WRITE`, `TOOLS_FULL`, `TOOLS_EVALUATOR`), `CONTRACT_AGREED` constant
- `state.py` — `HarnessState` persists to `comms/status.json`. Per-sprint attempt counters, per-phase cost tracking, sprint results, eval score history (`add_eval_score`, `get_eval_scores`, `get_last_eval_score`).
- `server.py` — `DevServer` auto-detects project structure, manages subprocess lifecycle with process groups (`os.setsid`), health-checks via HTTP polling.
- `agents/*.md` — System prompts. Planner reads `frontend-design-skill.md` at runtime via Read tool.

### Evaluator Criteria (two-part assessment)

Every evaluation assesses both parts. Any single FAIL in either part = entire evaluation FAIL.

- **Frontend part**: Design Quality (HIGH), Originality (HIGH), Craft (NORMAL), UI Functionality (NORMAL)
- **Backend part**: Product Depth (HIGH), Functionality (HIGH), Code Quality (NORMAL)

### Production Readiness (mandatory in all evaluations)

Every evaluation includes mandatory production readiness checks. An app that passes features but fails production readiness is a FAIL.

- **Responsive**: Evaluator tests at 375px viewport width. Navigation must collapse, no horizontal scroll, text readable.
- **UI States**: Every data-driven component must show loading, empty, error, and success states. Blank pages = FAIL.
- **Error Handling**: Forms must show inline validation errors. API failures must show user-friendly messages with retry. Silent failures = FAIL.
- **Criteria generation**: Evaluator's testable criteria always include responsive, UI states, and error handling criteria as mandatory sections.

## Testing Patterns

- Tests use `tempfile.TemporaryDirectory()` for isolated state
- Async tests use `@pytest.mark.anyio`
- Agent calls mocked with `patch("orchestrator.call_agent", ...)` returning `AgentResult`
- `_mock_result(text)` helper creates `AgentResult` with dummy token counts
- Tests that need `_init_output()` must set `orch._spec = None` first to force re-read

## gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Available skills: `/office-hours`, `/plan-ceo-review`, `/plan-eng-review`, `/plan-design-review`, `/design-consultation`, `/design-shotgun`, `/design-html`, `/review`, `/ship`, `/land-and-deploy`, `/canary`, `/benchmark`, `/browse`, `/connect-chrome`, `/qa`, `/qa-only`, `/design-review`, `/setup-browser-cookies`, `/setup-deploy`, `/retro`, `/investigate`, `/document-release`, `/codex`, `/cso`, `/autoplan`, `/plan-devex-review`, `/devex-review`, `/careful`, `/freeze`, `/guard`, `/unfreeze`, `/gstack-upgrade`, `/learn`.

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke checkpoint
- Code quality, health check → invoke health

## Git

- Author: `jhlee0409 <60695299+jhlee0409@users.noreply.github.com>`
- Must `gh auth switch --user jhlee0409` before push (multi-account setup)
