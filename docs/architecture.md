# Idle Harness Architecture

Idle Harness is a GAN-inspired, multi-agent autonomous coding system that builds full-stack web applications from a single natural-language prompt. It orchestrates three specialized Claude AI agents — Planner, Generator, and Evaluator — through an adversarial build-evaluate-iterate loop powered by the Claude Agent SDK.

This document explains how the system works at the implementation level.

## GAN Principle Applied to Software Engineering

The core architectural insight is borrowed from Generative Adversarial Networks (GANs). In a GAN, a generator produces outputs and a discriminator judges them, with neither having access to the other's internals. Idle Harness applies this adversarial dynamic to code generation:

- The **Generator** agent writes source code and builds the application.
- The **Evaluator** agent tests the running application through a browser — it never reads source code.

This separation is enforced at the tool-access level. The Evaluator is restricted to a single SDK tool (`Write`) plus Playwright MCP browser tools. It cannot use `Read`, `Glob`, `Grep`, or `Bash`. All context the Evaluator needs (the product spec, sprint contracts) is injected directly into its prompt by the orchestrator.

This means the Generator cannot pass evaluation by producing code that merely "looks correct." The Evaluator interacts with the live application the same way a real user would — clicking buttons, filling forms, checking that data persists after a page refresh. If the app does not genuinely work end-to-end, the Evaluator will fail it.

### Tool Access Control

Tool permissions are defined in `config.py` and enforced by the orchestrator when calling each agent via the Claude Agent SDK:

| Agent | Allowed Tools | Rationale |
|-------|--------------|-----------|
| Planner | `Read`, `Write` | Reads the user prompt context; writes the spec to `comms/spec.md` |
| Generator | `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep` | Full filesystem and shell access to build the application |
| Evaluator | `Write` only (+ Playwright MCP) | Cannot read source code; tests the running app via browser interaction |

The Evaluator's Write-only restriction is the mechanism that enforces the GAN principle. Without Read access, the Evaluator has no way to inspect implementation details — it can only judge the output.

## Agent Pipeline

The system follows a five-phase pipeline. The orchestrator (`orchestrator.py`) drives each phase sequentially.

### Phase 1: Planning

The Planner agent receives the user's raw prompt and generates a complete product specification. This spec includes:

- Product vision and target users
- Visual design language (specific hex colors, typography, component styles)
- Prioritized features (P0/P1/P2)
- UX flow descriptions
- Sprint decomposition with goals and acceptance criteria

The spec is written to `comms/spec.md`. The Planner is explicitly instructed to focus on product design, not technology choices — tech stack decisions belong to the Generator.

The orchestrator then parses the spec's `## Sprints` section using `sprint.py` to extract individual `Sprint` objects (number, name, features, goal). If no sprints section is found, the system falls back to a single sprint.

### Phase 2: Contract Negotiation

Before each sprint begins, the Generator and Evaluator negotiate a sprint contract. This is an adversarial negotiation loop:

1. The **Generator** proposes a contract with testable criteria, design decisions, and scope boundaries.
2. The **Evaluator** reviews the proposal — checking whether criteria are specific enough to test through browser interaction.
3. If the Evaluator writes `AGREED` at the top of its review, the proposal becomes the sprint contract.
4. If not agreed, the Generator revises based on feedback. This repeats for up to `max_negotiation_rounds` (default: 3).

If no agreement is reached after all rounds, the last proposal is used as the contract. The agreed contract is saved to `comms/sprints/sprint-N/sprint_contract.md`.

### Phase 3: Build

The Generator receives the sprint contract and full spec, then builds the application. It operates with full tool access (`Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`) and works inside the `output/{project-name}/` directory.

Key implementation details:

- The Generator runs in a **continuous session** across retry attempts. The orchestrator preserves the `conversation_id` from the Claude Agent SDK and passes it back via the `resume` parameter on subsequent calls. This means the Generator retains full context of what it built previously and what feedback it received — it does not start from scratch on retries.
- The Generator is capped at `generator_max_turns` (default: 200) per build attempt.
- After building, the Generator writes a `dev_server.json` file to the comms directory with the command needed to start the application.
- The Generator commits its work with git after each build.

### Phase 4: Evaluate

The orchestrator starts the dev server (detecting the start command from `dev_server.json` or project structure), then invokes the Evaluator.

The Evaluator:

1. Navigates to the running application via Playwright MCP (`browser_navigate`, `browser_click`, `browser_fill_form`, `browser_snapshot`, `browser_take_screenshot`)
2. Tests every criterion in the sprint contract
3. Takes screenshots as evidence, saved to `comms/sprints/sprint-N/screenshots/`
4. Writes a structured evaluation with per-criterion PASS/FAIL verdicts
5. Assesses design quality across four criteria: Design Quality, Originality, Craft, and Functionality
6. Issues a final `Verdict: PASS` or `Verdict: FAIL`

The Evaluator is designed to be strict. Its prompt includes anti-rationalization rules — it must write PASS/FAIL verdicts before writing justifications to prevent talking itself out of failures. Any single design criterion failure means the entire evaluation fails.

After evaluation, the orchestrator stops the dev server and checks the verdict.

### Phase 5: Iterate

If the Evaluator returns `Verdict: FAIL`, the orchestrator feeds the evaluation feedback back to the Generator for another build attempt. This loop repeats up to `max_build_attempts` (default: 3).

On retry, the Generator makes a strategic decision:

- **Refine** if most criteria passed and failures are specific, fixable issues.
- **Pivot** if the Evaluator flagged the design as generic or template-like — this means a complete visual redesign while preserving functionality.

If all retry attempts are exhausted, the orchestrator asks the user whether to continue to the next sprint or abort.

## Two Modes: Full vs. Simple

The system supports two operating modes, controlled by the `mode` setting in `config.py`.

### Full Mode (default)

Full mode decomposes the project into multiple sprints:

1. Plan: generate spec with sprint decomposition
2. For each sprint:
   a. Negotiate contract between Generator and Evaluator
   b. Build-evaluate-iterate loop (up to 3 attempts)
3. Final integration evaluation across all sprints

The integration evaluation is a separate pass where the Evaluator tests the complete application end-to-end, verifying that features from different sprints work together correctly.

### Simple Mode

Simple mode skips sprint decomposition and contract negotiation:

1. Plan: generate spec (sprint structure is ignored)
2. Single build-evaluate-iterate loop using the full spec as the contract

Simple mode is faster but lacks the incremental quality gates of full mode.

## Communication via Shared comms/ Directory

Agents do not communicate directly. All inter-agent communication flows through the filesystem, coordinated by the orchestrator. The `comms/` directory serves as the shared communication channel:

```
comms/
  spec.md                          # Product spec (written by Planner)
  status.json                      # Pipeline state (managed by orchestrator)
  dev_server.json                  # Server start command (written by Generator)
  integration_evaluation.md        # Final cross-sprint evaluation
  sprints/
    sprint-1/
      contract_proposal.md         # Generator's proposed contract
      contract_review.md           # Evaluator's review of proposal
      sprint_contract.md           # Agreed contract (copy of final proposal)
      evaluation.md                # Evaluator's test results
      screenshots/                 # Screenshot evidence from evaluation
    sprint-2/
      ...
```

The orchestrator reads from and writes to this directory structure. Agents receive file paths in their prompts and use the `Write` tool to produce their outputs. The orchestrator then reads those outputs to drive the next phase.

## State Tracking in status.json

The `HarnessState` class (`state.py`) maintains pipeline state in `comms/status.json`. This file tracks:

- **Phase**: current pipeline phase (`planning`, `building`, `completed`)
- **Sprint progress**: current sprint number and total sprint count
- **Attempt counts**: number of build and eval attempts per sprint
- **Sprint results**: PASS/FAIL outcome for each sprint and integration evaluation
- **Cost tracking**: cumulative input/output token counts, USD cost, broken down by phase (planner, negotiate, build, eval)
- **Timing**: elapsed seconds for planning, and per-sprint timing for negotiation, build, and eval phases

State is persisted to disk after every update, making the pipeline resilient to inspection during long runs. The final report printed at the end of a run reads from this state file.

## Continuous Session for Generator Across Retries

A critical implementation detail: the Generator maintains a continuous conversation session across build-evaluate-iterate cycles within a sprint. This is implemented via the Claude Agent SDK's conversation resumption feature.

In `orchestrator.py`, the orchestrator stores `_generator_conversation_id` after each build call. On the next build attempt, it passes this ID as the `resume` parameter to `call_agent()`, which maps to the SDK's `options.resume` field. The SDK then continues the existing conversation rather than starting a new one.

This means the Generator accumulates context across retries:

- Attempt 1: builds from scratch based on contract and spec
- Attempt 2: resumes with full memory of attempt 1, plus the Evaluator's feedback
- Attempt 3: resumes with memory of both previous attempts and all feedback

If a build attempt crashes (raises `AgentError`), the conversation ID is reset to `None`, forcing a fresh session on the next attempt — a crashed session cannot be reliably resumed.

## Dev Server Management

The `DevServer` class (`server.py`) handles starting and stopping the application for evaluation:

1. **Detection**: reads `dev_server.json` from the comms or output directory; falls back to detecting `package.json` or `frontend/` + `backend/` directory structure
2. **Port cleanup**: kills any existing processes on configured ports (default: 5173, 8000) before starting
3. **Full-stack support**: automatically starts both frontend (`npm run dev`) and backend (`uvicorn`) when a `frontend/` + `backend/` structure is detected
4. **Health check**: polls the configured URL until the server responds or a timeout is reached (default: 60 seconds)
5. **Cleanup**: sends SIGTERM to process groups, with SIGKILL fallback after 5 seconds

## Agent Invocation via Claude Agent SDK

All agents are invoked through `cli.py`, which wraps the `claude_agent_sdk`. The `call_agent()` function:

- Configures `ClaudeAgentOptions` with the agent's system prompt, allowed tools, working directory, and permission mode (`bypassPermissions`)
- Streams responses via `query()`, counting turns and accumulating token usage
- Supports MCP tool integration (e.g., `mcp__playwright__*` for the Evaluator)
- Classifies errors into `AgentError` (retryable) and `InfraError` (not retryable — e.g., buffer overflow from large screenshots)
- Returns an `AgentResult` with the response text, token counts, cost, and conversation ID for session continuity

## Preflight Checks

Before running, the orchestrator validates all dependencies:

- `claude_agent_sdk` Python package is installed
- `node`, `npm`, and `git` CLI tools are available
- Claude CLI is installed and authenticated
- Playwright MCP server is configured in `~/.claude.json`

This prevents confusing mid-run failures from missing dependencies.
