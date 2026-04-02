# Idle Harness

> GAN-inspired multi-agent system that autonomously builds full-stack web apps from a single prompt using Claude AI agents

## What is Idle Harness?

Idle Harness is an autonomous multi-agent coding system inspired by GAN (Generative Adversarial Network) architecture. It takes a short natural-language prompt and automatically generates a complete full-stack web application — frontend, backend, database, and styling — without human intervention.

The system orchestrates three specialized AI agents (Planner, Generator, and Evaluator) that collaborate through a structured build-evaluate-iterate loop. Like a GAN's generator-discriminator dynamic, the Generator builds the application while the Evaluator tests it as a real user would — without ever reading the source code. This adversarial relationship drives quality: the Generator can't cut corners because the Evaluator will catch it.

Built on [Anthropic's harness design for long-running apps](https://www.anthropic.com/engineering/harness-design-long-running-apps) and powered by the Claude Agent SDK.

## Quick Start

```bash
git clone https://github.com/jhlee0409/idle-harness.git
cd idle-harness
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Interactive setup — configures auth + Playwright MCP automatically
python orchestrator.py --setup

# Build an app
python orchestrator.py "A tarot reading web app with card-draw animations and AI interpretations"
```

That's it. If anything is missing, the harness detects it and offers to fix it automatically.

## Setup

Idle Harness includes a built-in interactive setup that detects and configures all dependencies.

### Option A: Auto-detect on first run

Just run the harness. If dependencies are missing, it offers to fix them:

```
$ python orchestrator.py "my app idea"

Preflight checks
  ✓ claude_agent_sdk
  ✓ node (v20.11.0)
  ✓ npm (10.2.4)
  ✓ git (2.43.0)
  ✗ auth — No auth configured
  ✗ MCP: playwright — 'playwright' not configured

  Fix 2 issue(s) automatically? [Y/n]: y

  Choose auth method:
    [o] OAuth login (uses subscription quota)
    [a] API key (pay per use, no quota limit)
  Choose: o
  → Running: claude login
  ✓ OAuth authenticated

  → Configuring playwright MCP in .mcp.json
  ✓ MCP: playwright configured in .mcp.json

All issues fixed
```

### Option B: Explicit setup

```bash
python orchestrator.py --setup
```

Runs all checks and auto-fixes everything without asking.

### Option C: CI / Non-interactive

```bash
CI=1 python orchestrator.py "my app idea"
```

Fails hard with exit code 1 if any dependency is missing. No interactive prompts.

### What gets checked

| Check | Auto-fixable | How |
|---|---|---|
| `claude_agent_sdk` | Yes | `pip install claude-agent-sdk` |
| `node`, `npm`, `git` | No | Prints install link |
| Claude CLI | No | Prints install link |
| Auth (OAuth or API key) | Yes | `claude login` or API key input |
| Playwright MCP | Yes | Writes `.mcp.json` automatically |

### Authentication options

| Method | How to configure | Cost model |
|---|---|---|
| **OAuth** | `claude login` (interactive setup handles this) | Uses subscription quota (Pro/Max plan) |
| **API key** | Set `ANTHROPIC_API_KEY` env var | Pay per token, no quota limit |

## How It Works

```
User Prompt (1-4 sentences)
    ↓
┌─────────┐     ┌───────────┐     ┌───────────┐
│ Planner │ ──→ │ Generator │ ←─→ │ Evaluator │
│         │     │           │     │           │
│ Spec    │     │ React     │     │ Browser   │
│ Design  │     │ Vite      │     │ Testing   │
│ Language│     │ FastAPI   │     │ Screenshot│
│         │     │ SQLite    │     │ Grading   │
└─────────┘     └───────────┘     └───────────┘
                      ↕
              Build → Evaluate → Feedback Loop (max 3 rounds)
```

1. **Plan** — Planner expands the prompt into a full product spec (including visual design language)
2. **Negotiate** — Generator and Evaluator negotiate sprint contracts with testable criteria
3. **Build** — Generator implements the full-stack app (continuous session preserves context across retries)
4. **Evaluate** — Evaluator tests the running app via Playwright, collecting screenshot evidence
5. **Iterate** — On FAIL, feedback is returned to the Generator for another attempt (up to 3 rounds)

### The GAN Principle

The Evaluator never reads source code. It can only interact with the running application through a browser — clicking buttons, filling forms, taking screenshots. This mirrors how a GAN's discriminator only sees the output, never the generator's internals. The result: the Generator must produce genuinely working software, not just code that looks correct.

## Agents

| Agent | Role | Key Behavior |
|-------|------|-------------|
| **Planner** | Prompt → Product Spec | Defines visual design language, explores AI integration opportunities, excludes technical details |
| **Generator** | Spec → Full-Stack Implementation | React+Vite+FastAPI+SQLite, self-verifies before handoff |
| **Evaluator** | Browser-Tests the Running App | Never reads source code (GAN principle), screenshot evidence, grades on 4 criteria |

## Evaluation Criteria

| Criterion | Weight | Description |
|-----------|--------|-------------|
| Design Quality | High | Does the whole app feel like one cohesive product? |
| Originality | High | Are there intentional design choices, not template defaults? |
| Craft | Normal | Typography, spacing, color harmony, contrast |
| Functionality | Normal | Do core interactions work end-to-end? |

## Configuration

Editable in `config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `mode` | `full` | `full` (sprints + contracts + iteration) / `simple` (single build + eval) |
| `max_build_attempts` | `3` | Max build→evaluate retry rounds |
| `max_negotiation_rounds` | `3` | Max contract negotiation rounds |
| `generator_max_turns` | `200` | Max turns for Generator agent |
| `dev_server_url` | `http://localhost:5173` | Frontend server URL |
| `mcp_tool` | `playwright` | Evaluator browser testing tool |

## Project Structure

```
idle-harness/
├── orchestrator.py      # Main orchestration loop + preflight + setup
├── cli.py               # Claude Agent SDK wrapper
├── config.py            # Settings (mode, servers, limits)
├── state.py             # State management (status.json)
├── server.py            # Dev server start/stop
├── sprint.py            # Sprint parsing
├── agents/
│   ├── planner.md       # Planner system prompt
│   ├── generator.md     # Generator system prompt
│   └── evaluator.md     # Evaluator system prompt
├── tests/               # pytest tests
├── comms/               # Runtime artifacts (spec, contracts, evaluations)
└── output/              # Generated applications
```

## FAQ

### What can I build with Idle Harness?

Any full-stack web application that can be described in a few sentences. Examples: a tarot reading app with AI interpretations, an AI-powered bookmark manager, a recipe finder with dietary filters, a personal finance tracker, a kanban board with drag-and-drop.

### How is this different from other AI code generators?

Most AI code generators produce code in a single pass. Idle Harness uses a multi-agent adversarial loop: one agent builds, another independently evaluates the running application (not the code), and feedback drives iterative improvement. This is closer to how a development team works — with separate roles for implementation and quality assurance.

### How long does it take?

Typical: 30 minutes to 2 hours depending on complexity. A 4-sprint app with retries can take 3+ hours but produces significantly better results than a single-pass generation.

### What does it cost?

Depends on your auth method:
- **OAuth (subscription)**: Uses your Claude Pro/Max quota. A typical 2-sprint app uses roughly 30-60 minutes of agent time.
- **API key**: Pay per token. A typical run costs $50-200 depending on complexity and retries.

### Does the `simple` mode skip sprints?

Yes. `simple` mode builds the entire app in one pass and runs a single evaluation. It still retries up to 3 times on failure, but skips sprint decomposition and contract negotiation. Good for simpler apps or faster iteration.

## License

MIT
