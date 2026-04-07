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
2. **Evaluator** generates `comms/testable_criteria.md` — 50-150 concrete, interaction-level test criteria from the spec (replaces contract negotiation)
3. **Generator** builds entire app in `output/{slug}/` with TOOLS_FULL, using criteria as checklist
4. **Evaluator** tests running app via Playwright MCP against each criterion individually
5. On FAIL: feedback → Generator retries (max 10 build attempts, continuous session via `session_id`). Timeout/InfraError stops retries immediately.
6. If backend passes but frontend design fails → **design refinement loop** (up to 10 iterations)
7. `comms/` artifacts archived to `output/{slug}/.harness/`

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
- **Evaluator PASS is validated** — Orchestrator parses evaluation for automation-limited ratio; >10% skipped criteria overrides PASS to FAIL. Prevents evaluator from rubber-stamping untested features.
- **Generator writes self_eval.md** — Mandatory self-evaluation file with per-criterion pass/fail. Orchestrator parses pass rate and logs warning if <90%.
- **Automation-limited feedback loop** — Items the evaluator couldn't test are extracted and passed back to the generator on retry with explicit self-test instructions.
- **Criteria generation has no fallback** — If evaluator fails to write testable_criteria.md or produces <10 criteria, harness raises RuntimeError instead of silently degrading.
- **Evaluator gets spec in simple mode** — Product spec (with Visual Design Language) is passed inline alongside testable criteria so evaluator can assess design quality against the original design direction.
- **Contract is cached in memory** — `_cached_contracts` dict initialized in `__init__`, caches contract text on first read to prevent generator from modifying criteria between retries (GAN integrity).
- **Design refinement includes contract** — Generator receives testable criteria during design refinement so it knows which specific frontend criteria the Evaluator will re-test.

### File Roles

- `orchestrator.py` — Orchestration + CLI + preflight + logging (~1000 lines, intentionally single file)
- `cli.py` — `call_agent()` wrapper around `claude_agent_sdk.query()`. Returns `AgentResult` with session_id, tokens, cost. Exports `fmt_tokens()` and `fmt_elapsed()` shared formatters.
- `config.py` — `CONFIG` dict, tool lists (`TOOLS_READ_WRITE`, `TOOLS_FULL`, `TOOLS_EVALUATOR`), `CONTRACT_AGREED` constant
- `state.py` — `HarnessState` persists to `comms/status.json`. Per-sprint attempt counters, per-phase cost tracking, sprint results.
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
