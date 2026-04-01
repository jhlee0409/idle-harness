# Idle Harness

> GAN-inspired multi-agent system that autonomously builds full-stack web apps from a single prompt using Claude AI agents

## What is Idle Harness?

Idle Harness is an autonomous multi-agent coding system inspired by GAN (Generative Adversarial Network) architecture. It takes a short natural-language prompt and automatically generates a complete full-stack web application — frontend, backend, database, and styling — without human intervention.

The system orchestrates three specialized AI agents (Planner, Generator, and Evaluator) that collaborate through a structured build-evaluate-iterate loop. Like a GAN's generator-discriminator dynamic, the Generator builds the application while the Evaluator tests it as a real user would — without ever reading the source code. This adversarial relationship drives quality: the Generator can't cut corners because the Evaluator will catch it.

Built on [Anthropic's harness design for long-running apps](https://www.anthropic.com/engineering/harness-design-long-running-apps) and powered by the Claude Agent SDK.

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

## Quick Start

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run (requires Claude CLI login)
python3 orchestrator.py "A tarot reading web app with card-draw animations and AI interpretations"
```

Output is generated in `output/{product-name}/`.

## Prerequisites

- Python 3.11+
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) (OAuth login completed)
- Node.js 18+ (used by Generator for frontend builds)
- Playwright MCP (used by Evaluator for browser testing)

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
├── orchestrator.py      # Main orchestration loop
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
└── output/              # Generated applications
```

## FAQ

### What is Idle Harness?

Idle Harness is a GAN-inspired autonomous coding system that uses three AI agents to build full-stack web applications from a single short prompt. It handles everything from product planning to implementation to quality evaluation — automatically.

### How does the GAN-inspired architecture work?

The system applies the adversarial principle from GANs to software engineering. The Generator agent builds the application, while the Evaluator agent tests it purely through browser interaction — without ever reading the source code. This creates a productive tension where the Generator must produce genuinely working software because the Evaluator will test it like a real user.

### What can I build with Idle Harness?

Any full-stack web application that can be described in a few sentences. Examples include a tarot reading app with AI interpretations, an AI-powered bookmark manager, a recipe finder with dietary filters, or a personal finance tracker. The system generates React frontends, FastAPI backends, and SQLite databases.

### How is Idle Harness different from other AI code generators?

Most AI code generators produce code in a single pass. Idle Harness uses a multi-agent adversarial loop: one agent builds, another independently evaluates the running application (not the code), and feedback drives iterative improvement. This is closer to how a development team works — with separate roles for implementation and quality assurance.

### Does Idle Harness require an API key?

No API key is needed. Idle Harness uses the Claude CLI with OAuth authentication. You need to have the Claude CLI installed and logged in via `claude login`.

### How long does it take to generate an app?

Typical generation takes 10-30 minutes depending on complexity. The system may iterate up to 3 times on each sprint if the Evaluator finds issues, which adds time but improves quality.

## License

MIT
