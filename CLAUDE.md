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

### Agent Flow (full mode)

1. **Planner** reads `agents/frontend-design-skill.md` then generates `comms/spec.md`
2. Per sprint: **Generator** proposes contract → **Evaluator** reviews → agree or iterate (max 3 rounds)
3. **Generator** builds in `output/{slug}/` with TOOLS_FULL (Read/Write/Edit/Bash/Glob/Grep)
4. **Evaluator** tests running app via Playwright MCP with TOOLS_EVALUATOR (Write only — no Read, enforcing GAN principle)
5. On FAIL: feedback → Generator retries (max 3 attempts, continuous session via `session_id`)
6. After all sprints: integration evaluation across full app
7. `comms/` artifacts archived to `output/{slug}/.harness/`

### Key Design Decisions

- **Evaluator cannot read source code** — `TOOLS_EVALUATOR = ["Write"]` in config.py. Spec and contract content are passed in the prompt, not read from files. This enforces the GAN principle.
- **Generator maintains continuous session** — `session_id` from SDK init messages is reused across retry attempts. Reset on crash (`AgentError` resets `_generator_session_id`).
- **MCP is SDK-managed** — Playwright MCP launched via `mcp_servers` in `ClaudeAgentOptions`, not user-configured `.mcp.json`.
- **`comms/` is staging, not persistence** — Wiped on each `setup()`. Project artifacts archived to `output/{slug}/.harness/` after completion.
- **Verdict/contract parsing uses regex** — `_check_verdict_pass()` matches `^#{0,3}\s*Verdict:\s*PASS\s*$` to prevent false positives. `_check_contract_agreed()` matches `^AGREED\b`.

### File Roles

- `orchestrator.py` — Orchestration + CLI + preflight + logging (~1000 lines, intentionally single file)
- `cli.py` — `call_agent()` wrapper around `claude_agent_sdk.query()`. Returns `AgentResult` with session_id, tokens, cost. Exports `fmt_tokens()` and `fmt_elapsed()` shared formatters.
- `config.py` — `CONFIG` dict, tool lists (`TOOLS_READONLY`, `TOOLS_FULL`, `TOOLS_EVALUATOR`), `CONTRACT_AGREED` constant
- `state.py` — `HarnessState` persists to `comms/status.json`. Per-sprint attempt counters, per-phase cost tracking, sprint results.
- `server.py` — `DevServer` auto-detects project structure, manages subprocess lifecycle with process groups (`os.setsid`), health-checks via HTTP polling.
- `agents/*.md` — System prompts. Planner reads `frontend-design-skill.md` at runtime via Read tool.

### Evaluator Criteria (full-stack adapted)

Product Depth (HIGH), Functionality (HIGH), Visual Design (NORMAL), Code Quality (NORMAL). Any one FAIL = entire evaluation FAIL.

## Testing Patterns

- Tests use `tempfile.TemporaryDirectory()` for isolated state
- Async tests use `@pytest.mark.anyio`
- Agent calls mocked with `patch("orchestrator.call_agent", ...)` returning `AgentResult`
- `_mock_result(text)` helper creates `AgentResult` with dummy token counts
- Tests that need `_init_output()` must set `orch._spec = None` first to force re-read

## Git

- Author: `jhlee0409 <60695299+jhlee0409@users.noreply.github.com>`
- Must `gh auth switch --user jhlee0409` before push (multi-account setup)
