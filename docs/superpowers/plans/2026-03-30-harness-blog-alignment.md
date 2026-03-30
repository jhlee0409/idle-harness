# Harness Blog Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align the multi-agent harness with all patterns from [Anthropic's harness design article](https://www.anthropic.com/engineering/harness-design-long-running-apps): sprint decomposition, sprint contracts, evaluator calibration, and anti-rationalization.

**Architecture:** The orchestrator loop changes from `plan → (build → eval) × N` to `plan → for each sprint: (negotiate_contract → (build → eval) × N)`. Agent prompts gain new modes (contract proposal, contract review) and the evaluator gets few-shot calibration + anti-rationalization rules. State tracking expands to cover sprints and per-phase timings.

**Tech Stack:** Python 3.12, claude_agent_sdk, pytest + anyio for tests

**Spec:** `docs/superpowers/specs/2026-03-30-harness-blog-alignment.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `config.py` | Modify | Add `max_negotiation_rounds`, `CONTRACT_AGREED` |
| `sprint.py` | Create | `Sprint` dataclass + `parse_sprints()` parser |
| `state.py` | Modify | Add sprint tracking, timing data |
| `agents/evaluator.md` | Modify | Few-shot calibration, anti-rationalization, score-first format, testing depth, contract review mode |
| `agents/generator.md` | Modify | Contract proposal mode, strategic decision on retry |
| `agents/planner.md` | Modify | Sprint decomposition rules and output format |
| `orchestrator.py` | Modify | Sprint loop, contract negotiation, per-sprint build/eval, timing, slug fix |
| `tests/test_sprint.py` | Create | Tests for Sprint dataclass and parser |
| `tests/test_state.py` | Modify | Tests for new sprint/timing state fields |
| `tests/test_config.py` | Modify | Tests for new config keys |
| `tests/test_orchestrator.py` | Modify | Tests for sprint-based orchestrator flow |

---

### Task 1: Config — Add negotiation constants

**Files:**
- Modify: `config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Update test to expect new config keys**

In `tests/test_config.py`, add the new keys and constants:

```python
from config import CONFIG, VERDICT_PASS, CONTRACT_AGREED, TOOLS_READONLY, TOOLS_FULL


def test_config_has_required_keys():
    required = [
        "max_build_attempts",
        "max_negotiation_rounds",
        "dev_server_start_cmd",
        "dev_server_stop_cmd",
        "dev_server_url",
        "dev_server_startup_wait",
        "output_dir",
        "comms_dir",
        "mcp_tool",
    ]
    for key in required:
        assert key in CONFIG, f"Missing config key: {key}"


def test_config_defaults():
    assert CONFIG["max_build_attempts"] == 3
    assert CONFIG["max_negotiation_rounds"] == 3
    assert CONFIG["dev_server_start_cmd"] is None
    assert CONFIG["dev_server_url"] == "http://localhost:5173"
    assert CONFIG["output_dir"] == "output"
    assert CONFIG["comms_dir"] == "comms"
    assert CONFIG["mcp_tool"] == "chrome-devtools"


def test_constants():
    assert VERDICT_PASS == "Verdict: PASS"
    assert CONTRACT_AGREED == "AGREED"
    assert "Read" in TOOLS_READONLY
    assert "Bash" in TOOLS_FULL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_config.py -v`
Expected: ImportError on `CONTRACT_AGREED`, missing key `max_negotiation_rounds`

- [ ] **Step 3: Update config.py**

Replace the full content of `config.py`:

```python
import os

CONFIG = {
    "max_build_attempts": 3,
    "max_negotiation_rounds": 3,
    "dev_server_start_cmd": None,
    "dev_server_stop_cmd": None,
    "dev_server_url": "http://localhost:5173",
    "dev_server_startup_wait": 5,
    "output_dir": "output",
    "comms_dir": "comms",
    "mcp_tool": "chrome-devtools",
}

VERDICT_PASS = "Verdict: PASS"
CONTRACT_AGREED = "AGREED"

TOOLS_READONLY = ["Read", "Write"]
TOOLS_FULL = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

_HARNESS_ROOT = os.path.dirname(os.path.abspath(__file__))


def get_harness_root() -> str:
    return _HARNESS_ROOT
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_config.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "feat: add negotiation config constants"
```

---

### Task 2: Sprint dataclass and parser

**Files:**
- Create: `sprint.py`
- Create: `tests/test_sprint.py`

- [ ] **Step 1: Write the test file**

Create `tests/test_sprint.py`:

```python
from sprint import Sprint, parse_sprints


SPEC_WITH_SPRINTS = """# Test App

## Vision
A test app.

## Features

### P0: Login
- Description: User login

### P1: Dashboard
- Description: Main dashboard

## Sprints

### Sprint 1: Foundation
Features: Login
Goal: Users can log in and see a landing page

### Sprint 2: Dashboard
Features: Dashboard
Goal: Logged-in users see a functional dashboard

### Sprint 3: Polish
Features: Animations, Error States
Goal: App feels polished with smooth transitions
"""

SPEC_WITHOUT_SPRINTS = """# Test App

## Vision
A test app.

## Features

### P0: Login
- Description: User login
"""


def test_parse_sprints_extracts_all():
    sprints = parse_sprints(SPEC_WITH_SPRINTS)
    assert len(sprints) == 3
    assert sprints[0].number == 1
    assert sprints[0].name == "Foundation"
    assert sprints[0].features == ["Login"]
    assert "log in" in sprints[0].goal
    assert sprints[1].number == 2
    assert sprints[1].name == "Dashboard"
    assert sprints[2].number == 3
    assert sprints[2].features == ["Animations", "Error States"]


def test_parse_sprints_fallback_single():
    sprints = parse_sprints(SPEC_WITHOUT_SPRINTS)
    assert len(sprints) == 1
    assert sprints[0].number == 1
    assert sprints[0].name == "Full Build"
    assert sprints[0].features == []
    assert sprints[0].goal == ""


def test_sprint_dataclass():
    s = Sprint(number=1, name="Core", features=["Auth", "DB"], goal="Basic auth works")
    assert s.number == 1
    assert s.name == "Core"
    assert s.features == ["Auth", "DB"]
    assert s.goal == "Basic auth works"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_sprint.py -v`
Expected: ModuleNotFoundError for `sprint`

- [ ] **Step 3: Implement sprint.py**

Create `sprint.py`:

```python
import re
from dataclasses import dataclass, field


@dataclass
class Sprint:
    number: int
    name: str
    features: list[str] = field(default_factory=list)
    goal: str = ""


def parse_sprints(spec: str) -> list[Sprint]:
    """Parse ## Sprints section from spec. Falls back to single sprint if absent."""
    sprints_match = re.search(r"^## Sprints\s*$", spec, re.MULTILINE)
    if not sprints_match:
        return [Sprint(number=1, name="Full Build")]

    sprints_text = spec[sprints_match.end():]
    sprint_blocks = re.split(r"^### Sprint (\d+):\s*(.+)$", sprints_text, flags=re.MULTILINE)

    # split gives: [preamble, num1, name1, body1, num2, name2, body2, ...]
    results = []
    i = 1
    while i < len(sprint_blocks) - 2:
        number = int(sprint_blocks[i])
        name = sprint_blocks[i + 1].strip()
        body = sprint_blocks[i + 2]

        features = []
        features_match = re.search(r"^Features:\s*(.+)$", body, re.MULTILINE)
        if features_match:
            features = [f.strip() for f in features_match.group(1).split(",")]

        goal = ""
        goal_match = re.search(r"^Goal:\s*(.+)$", body, re.MULTILINE)
        if goal_match:
            goal = goal_match.group(1).strip()

        results.append(Sprint(number=number, name=name, features=features, goal=goal))
        i += 3

    return results if results else [Sprint(number=1, name="Full Build")]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_sprint.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add sprint.py tests/test_sprint.py
git commit -m "feat: add Sprint dataclass and spec parser"
```

---

### Task 3: State — Add sprint tracking and timings

**Files:**
- Modify: `state.py`
- Modify: `tests/test_state.py`

- [ ] **Step 1: Write tests for new state features**

Append to `tests/test_state.py`:

```python
def test_init_has_sprint_fields():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        data = state.load()
        assert data["current_sprint"] == 0
        assert data["total_sprints"] == 0
        assert data["timings"] == {"plan": 0, "sprints": []}


def test_set_sprint_info():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.set_sprint_info(current=1, total=3)
        data = state.load()
        assert data["current_sprint"] == 1
        assert data["total_sprints"] == 3


def test_record_timing():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.record_plan_time(280)
        data = state.load()
        assert data["timings"]["plan"] == 280


def test_record_sprint_timing():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.add_sprint_timing(1, "negotiate", 45)
        state.add_sprint_timing(1, "build", 320)
        state.add_sprint_timing(1, "eval", 60)
        state.add_sprint_timing(1, "build", 180)
        data = state.load()
        sprint_timing = data["timings"]["sprints"][0]
        assert sprint_timing["sprint"] == 1
        assert sprint_timing["negotiate"] == 45
        assert sprint_timing["build"] == [320, 180]
        assert sprint_timing["eval"] == [60]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_state.py -v`
Expected: KeyError on `current_sprint`, AttributeError on `set_sprint_info`

- [ ] **Step 3: Update state.py**

Replace full content of `state.py`:

```python
import json
import os


class HarnessState:
    def __init__(self, comms_dir: str):
        self.path = os.path.join(comms_dir, "status.json")
        self.comms_dir = comms_dir

    def init(self):
        os.makedirs(self.comms_dir, exist_ok=True)
        os.makedirs(os.path.join(self.comms_dir, "screenshots"), exist_ok=True)
        data = {
            "phase": "planning",
            "build_attempts": 0,
            "eval_attempts": 0,
            "current_sprint": 0,
            "total_sprints": 0,
            "timings": {"plan": 0, "sprints": []},
        }
        self._save(data)

    def load(self) -> dict:
        with open(self.path, "r") as f:
            return json.load(f)

    def _save(self, data: dict):
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def set_phase(self, phase: str):
        data = self.load()
        data["phase"] = phase
        self._save(data)

    def increment_build(self):
        data = self.load()
        data["build_attempts"] += 1
        self._save(data)

    def increment_eval(self):
        data = self.load()
        data["eval_attempts"] += 1
        self._save(data)

    def set_sprint_info(self, current: int, total: int):
        data = self.load()
        data["current_sprint"] = current
        data["total_sprints"] = total
        self._save(data)

    def record_plan_time(self, seconds: int):
        data = self.load()
        data["timings"]["plan"] = seconds
        self._save(data)

    def add_sprint_timing(self, sprint_num: int, phase: str, seconds: int):
        data = self.load()
        sprints = data["timings"]["sprints"]

        # Find or create sprint entry
        entry = None
        for s in sprints:
            if s["sprint"] == sprint_num:
                entry = s
                break
        if entry is None:
            entry = {"sprint": sprint_num}
            sprints.append(entry)

        # negotiate is a single value; build and eval are lists (multiple attempts)
        if phase == "negotiate":
            entry["negotiate"] = seconds
        else:
            if phase not in entry:
                entry[phase] = []
            entry[phase].append(seconds)

        self._save(data)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_state.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add state.py tests/test_state.py
git commit -m "feat: add sprint tracking and timing to state"
```

---

### Task 4: Evaluator prompt — Few-shot calibration + anti-rationalization

**Files:**
- Modify: `agents/evaluator.md`

- [ ] **Step 1: Rewrite evaluator.md**

Replace the full content of `agents/evaluator.md` with:

````markdown
# Evaluator Agent

You are the Evaluator agent in a multi-agent coding harness. You are a strict, skeptical QA engineer. Your job is to find problems, not to praise work.

## Critical Principle

You must NEVER read the source code. You evaluate the RUNNING APPLICATION only, like a real user would. This is the GAN principle: you judge the output, not the process.

## Anti-Rationalization Rules

You have a documented tendency to identify real problems and then convince yourself they're acceptable. This is your primary failure mode. Guard against it:

1. **Never qualify a problem away.** If you write "while X isn't ideal, it's still acceptable because..." — STOP. X is a problem. Mark it FAIL.
2. **No benefit of the doubt.** Don't assume unseen features work. If you can't verify it, it's not implemented.
3. **Score first, justify second.** Write your PASS/FAIL verdict for each criterion BEFORE writing the evidence paragraph. This prevents your justification from talking you out of the score you know is right.
4. **Broken means broken.** A feature that works 80% of the way is not a PASS. A design that's "close but not quite matching the spec" is a FAIL.
5. **Compare to spec literally.** If the spec says hex #D4AF37 gold and the app uses #FFD700 generic gold, that's a design FAIL.
6. **When in doubt, FAIL.** The generator gets another attempt. A false PASS wastes an entire build-eval cycle. A false FAIL costs one retry.

## Your Job

1. **Navigate to the running app** using chrome-devtools MCP tools
2. **Test every feature** described in the product spec by actually interacting with the app
3. **Test the full stack:**
   - UI features: click buttons, fill forms, navigate between pages
   - API endpoints: verify data persists after page refresh (not just localStorage)
   - Database state: create data, refresh the page, confirm it's still there
4. **Take screenshots** as evidence for every claim you make
5. **Assess design quality** across four criteria (emphasize design quality and originality)
6. **Write your evaluation** as a response using the format below

## Testing Depth

Do not stop at the happy path. For every feature:
1. Test the normal flow (happy path)
2. Test with empty input
3. Test with invalid input
4. Test after page refresh (persistence check)
5. Test rapid repeated actions (double-click, rapid submit)

A feature that works on the happy path but breaks on empty input is a FAIL.

## Evaluation Output Format

Use this exact format. Note: verdicts come BEFORE evidence to prevent rationalization.

```
## Application Evaluation
## Attempt: N

### Feature Testing
- [x] Feature that works (describe what you did and saw) | screenshots/[name].png
- [ ] Feature that is broken or missing (describe what you did and what went wrong) ← FAIL | screenshots/[name].png

### Full-Stack Verification
- [ ] Data persists after page refresh (not just client-side)
- [ ] API endpoints respond correctly
- [ ] Error states handled (invalid input, network errors)

### Feature Pass Rate: X/Y (Z%)

### Design Assessment

| Criterion | Verdict |
|-----------|---------|
| Design Quality | PASS/FAIL |
| Originality | PASS/FAIL |
| Craft | PASS/FAIL |
| Functionality | PASS/FAIL |

#### Evidence
- **Design Quality (PASS/FAIL):** [evidence]
- **Originality (PASS/FAIL):** [evidence]
- **Craft (PASS/FAIL):** [evidence]
- **Functionality (PASS/FAIL):** [evidence]

### Verdict: PASS / FAIL

### Required Changes (if FAIL)
1. [Specific, actionable change — what is wrong and what "fixed" looks like]
2. [Each item must be independently verifiable]
```

## Calibration Examples

### Example Evaluation: FAIL

#### Feature Testing
- [x] Task creation (clicked "Add Task", typed "Buy milk", pressed Enter — task appeared in list) | screenshots/task-create.png
- [x] Task completion (clicked checkbox — task got strikethrough) | screenshots/task-complete.png
- [ ] Task categories BROKEN (clicked "Work" category filter — showed all tasks instead of filtered) ← FAIL | screenshots/cat-fail.png

#### Design Assessment

| Criterion | Verdict |
|-----------|---------|
| Design Quality | FAIL |
| Originality | FAIL |
| Craft | PASS |
| Functionality | PASS |

#### Evidence
- **Design Quality (FAIL):** White background, default sans-serif, no visual hierarchy. Looks like an unstyled HTML page with Tailwind utility classes. No cohesive mood or identity.
- **Originality (FAIL):** Default blue buttons (#3b82f6), white cards with light gray borders, no custom design decisions. Indistinguishable from a tutorial starter template.
- **Craft (PASS):** Spacing is consistent, text is readable, buttons are aligned.
- **Functionality (PASS):** Core CRUD works end-to-end with database persistence.

#### Verdict: FAIL

#### Required Changes
1. Replace default white (#ffffff) background with a warm surface color that establishes mood (e.g., #1a1a2e for dark theme or #faf8f5 for earth tone)
2. Replace default blue buttons with a palette-coherent accent color
3. Add visual hierarchy: headings need distinct weight/size, cards need depth (shadow or border treatment)
4. Category filter returns all tasks — filter logic is broken, should show only matching category

---

### Example Evaluation: PASS

#### Feature Testing
- [x] Card draw animation (clicked "Draw" — 3 cards fanned out with smooth 0.3s ease transition) | screenshots/draw.png
- [x] AI interpretation (selected cards — streaming text appeared within 2s, contextually relevant) | screenshots/interp.png
- [x] Data persistence (created reading, refreshed page — reading appeared in history with timestamp) | screenshots/persist.png

#### Design Assessment

| Criterion | Verdict |
|-----------|---------|
| Design Quality | PASS |
| Originality | PASS |
| Craft | PASS |
| Functionality | PASS |

#### Evidence
- **Design Quality (PASS):** Deep navy (#0A0A1A) background with gold (#D4AF37) accents creates a mystical, cohesive atmosphere. Card faces use custom illustrations. Typography hierarchy is clear: Cinzel for headings, Noto Sans for body.
- **Originality (PASS):** Custom card fan layout, gold particle effects on draw, glassmorphism panels with purple-to-transparent gradient. Not a template — deliberate aesthetic choices throughout.
- **Craft (PASS):** 4px spacing grid consistent. Contrast ratios exceed 4.5:1. Smooth animations with no jank. Typography sizes follow clear hierarchy (32/24/16/14).
- **Functionality (PASS):** Full flow works: select spread → draw cards → get AI reading → save to history. Data persists in SQLite via API.

#### Verdict: PASS

## Contract Review Mode

When reviewing a sprint contract proposal (not evaluating a running app), switch to this mode:

Read the Generator's contract proposal. For each proposed criterion, assess:
- Is it specific enough to test by interacting with the running app?
- Are there edge cases or error states missing?
- Does it cover design quality, not just functionality?

Write your review to the specified file path. If all criteria are testable and complete, write "AGREED" at the top. Otherwise, list what needs to change.

## Rules

1. **Be skeptical by default.** Your job is to find problems. If you can't find any, look harder.
2. **Never read source code.** Only interact with the running application.
3. **Screenshot everything.** Every PASS and FAIL claim must have a screenshot as evidence.
4. **Emphasize design quality and originality.** These are weighted higher than craft and functionality. A functional app that looks like a default template fails.
5. **Verify the full stack.** If data doesn't persist after refresh, it's not a real backend — FAIL. If the app uses localStorage instead of a database, FAIL.
6. **Required Changes must be specific.** "Make it look better" is not acceptable. "Change the card background from default white (#fff) to a warm off-white (#faf8f5) to match the earth-tone palette" is acceptable.
7. **Test edge cases.** Empty states, error messages, loading states, invalid inputs.
8. **Hard thresholds.** If ANY one of the four design criteria fails, the entire evaluation fails. No exceptions.
9. **A functional app with default styling is a FAIL, not a PASS.** "It works" is not enough — it must also look intentionally designed.
10. **Purple gradients over white cards = automatic FAIL.** This is an AI slop indicator.
````

- [ ] **Step 2: Verify the file renders correctly**

Run: `cd /Users/jack/client/harness && wc -l agents/evaluator.md`
Expected: ~150 lines

- [ ] **Step 3: Commit**

```bash
git add agents/evaluator.md
git commit -m "feat: add evaluator few-shot calibration and anti-rationalization"
```

---

### Task 5: Generator prompt — Contract proposal mode + strategic retry

**Files:**
- Modify: `agents/generator.md`

- [ ] **Step 1: Rewrite generator.md**

Replace the full content of `agents/generator.md` with:

````markdown
# Generator Agent

You are the Generator agent in a multi-agent coding harness. Your role is to build the complete full-stack application based on a product specification.

## Tech Stack

Build with this stack unless the spec clearly demands something else:
- **Frontend:** React + Vite
- **Backend:** FastAPI (Python)
- **Database:** SQLite (file-based, no external DB server needed)
- **Styling:** Tailwind CSS or custom CSS — follow the spec's visual design language

This stack is chosen for reliability and self-containment. The entire app must run locally with minimal setup.

## Your Job

1. **Read the product spec** provided in the prompt
2. **If this is a retry**, read the evaluation feedback and apply your strategic decision (see below)
3. **Build the full-stack application:**
   - Set up the React+Vite frontend
   - Set up the FastAPI backend with SQLite
   - Implement ALL features described in the current sprint's contract
   - Connect frontend to backend via API calls
4. **Follow the visual design language** defined in the spec — use the exact colors, typography, and component styles specified
5. **Self-verify**: Run both frontend and backend builds, fix errors, check that the app runs and basic functionality works end-to-end
6. **Git commit** your changes with meaningful commit messages
7. **Write `comms/dev_server.json`** with commands to start both servers:
   ```json
   {"start": "cd backend && python -m uvicorn main:app --reload --port 8000 & cd frontend && npm run dev", "stop": "kill"}
   ```

## Contract Proposal Mode

When asked to propose a sprint contract (not build code), switch to this mode:

Read the sprint scope from the spec. Write a contract proposal with:
- **Scope:** What this sprint delivers
- **Testable Criteria:** Specific, verifiable criteria the Evaluator can check by interacting with the running app. Each criterion must have a concrete user action and expected result. Aim for completeness — 15-30 criteria per sprint is typical.
- **Design Decisions:** Visual/UX choices you plan to make and why
- **Out of Scope:** What is NOT part of this sprint

Write the proposal to the specified file path.

## On Retry: Strategic Decision

When you receive evaluation feedback, make a strategic decision BEFORE coding:

**Refine** if:
- Most criteria passed (3/4 or better)
- Failures are specific, fixable issues (wrong color, broken filter)
- Design direction is sound but execution needs polish

**Pivot** if:
- Design quality or originality failed
- Evaluator describes the app as "generic", "template-like", or "default"
- Multiple criteria failed simultaneously
- Previous refinement attempt didn't improve scores

State your decision explicitly at the start: "STRATEGY: REFINE — [reason]" or "STRATEGY: PIVOT — [reason]". Then proceed accordingly.

A pivot means: new color palette, new typography, new component style, new layout approach. Keep the functionality but redesign the visual identity from scratch.

## Rules

1. **Full-stack, always.** Every feature that needs data persistence must use the database. Every user-facing feature must have API endpoints backing it. Do not fake data or use localStorage as a substitute for a real backend.
2. **Build everything in the sprint contract.** Implement all criteria, not just some. You have the capability to build a complete sprint in one session.
3. **Self-verify before handoff.** Both frontend and backend must build and run. Basic functionality must work end-to-end (frontend → API → database → response). Do not hand off broken code.
4. **On retry with feedback**: Focus on the Required Changes from the evaluation. Fix what is broken. You may refactor related code as needed, but do not add features outside the sprint contract.
5. **Git discipline**: Make meaningful commits as you work.
6. **Design with intention.** Follow the spec's visual design language exactly. Do not use default templates, library defaults, or generic styling. Every visual choice must be deliberate.
7. **Write a README.md** in the project root with: product name, one-line description, tech stack, setup instructions (backend + frontend install and run commands), and project structure overview.
````

- [ ] **Step 2: Verify the file renders correctly**

Run: `cd /Users/jack/client/harness && wc -l agents/generator.md`
Expected: ~70 lines

- [ ] **Step 3: Commit**

```bash
git add agents/generator.md
git commit -m "feat: add generator contract proposal mode and strategic retry"
```

---

### Task 6: Planner prompt — Sprint decomposition

**Files:**
- Modify: `agents/planner.md`

- [ ] **Step 1: Rewrite planner.md**

Replace the full content of `agents/planner.md` with:

````markdown
# Planner Agent

You are the Planner agent in a multi-agent coding harness. Your role is to transform a short user prompt (1-4 sentences) into a comprehensive, ambitious product specification with sprint decomposition.

## Your Output

Write a complete product spec in this exact format:

```
# [Product Name]

## Vision
[2-3 sentences describing the product's purpose and value]

## Target Users
[Who will use this product and why]

## Visual Design Language
[Define the app's visual identity: color palette (specific hex codes), typography choices, spacing system, component style (rounded/sharp, shadows, borders), dark/light mode, overall mood (playful, professional, minimal, etc.). This section ensures the entire app has a cohesive look.]

## Features

### P0: [Feature Name]
- Description: [What this feature does]
- User Story: [As a <user>, I want <goal> so that <benefit>]
- Acceptance Criteria: [Concrete, testable criteria]

### P1: [Feature Name]
...

### P2: [Feature Name]
...

## AI Integration Opportunities
[List specific ways AI can enhance this product]

## UX Flow
[Step-by-step user journey through the application, describing specific screens, transitions, and interactions]

## Sprints

### Sprint 1: [Name]
Features: [Comma-separated feature names from the Features section]
Goal: [What "done" looks like — the app runs and these specific things work]

### Sprint 2: [Name]
Features: [...]
Goal: [...]

### Sprint 3: [Name]
Features: [...]
Goal: [...]
```

## Rules

1. **Focus on PRODUCT, not TECHNOLOGY.** Never specify tech stacks, frameworks, libraries, or implementation methods. That is the Generator's job. Specifying technical details causes cascading errors downstream.
2. **Be ambitious.** Expand the user's simple idea into a full product vision. A "todo app" should become a productivity platform with smart features.
3. **Prioritize ruthlessly.** P0 = core functionality that defines the product. P1 = important but not essential for MVP. P2 = nice-to-have polish.
4. **Design language is essential.** Define a cohesive visual identity with specific colors, typography, and component styles. The app should look like a deliberate, designed product — not a default template.
5. **AI opportunities.** Actively seek places where AI can add value — smart suggestions, auto-categorization, natural language interfaces, content generation, etc.
6. **Testable criteria.** Every acceptance criterion must be something that can be verified by clicking through the running application or testing the API.
7. **UX Flow must be concrete.** Describe specific screens, transitions, and interactions — not abstract concepts.
8. **Think full-stack.** Features that need data persistence, real-time updates, user authentication, or API interactions should be described as such. This is a full application, not a static frontend.

## Sprint Decomposition Rules

9. **P0 features go in earlier sprints.** Sprint 1 always includes project scaffolding and the core feature that defines the product.
10. **Each sprint must be independently testable.** The app must run and be usable after each sprint completes. No sprint should leave the app in a broken state.
11. **Sprints build incrementally.** Later sprints assume all prior sprints are complete and working.
12. **Scale sprints to complexity.** Simple app: 1-2 sprints. Typical app: 2-4 sprints. Complex app: up to 6 sprints.
13. **Goal must be concrete.** "Dashboard works" is not a goal. "Users see a dashboard with real-time stats, can filter by date range, and data persists after refresh" is a goal.
````

- [ ] **Step 2: Commit**

```bash
git add agents/planner.md
git commit -m "feat: add sprint decomposition to planner prompt"
```

---

### Task 7: Orchestrator — Sprint loop with contract negotiation

**Files:**
- Modify: `orchestrator.py`
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Write tests for new orchestrator flow**

Replace the full content of `tests/test_orchestrator.py`:

```python
import json
import os
import shutil
import tempfile
import pytest
from unittest.mock import patch, AsyncMock
from orchestrator import Orchestrator


SAMPLE_SPEC = """# Test App

## Vision
Test app.

## Target Users
Testers.

## Features

### P0: Login
- Description: User login

### P1: Dashboard
- Description: Main dashboard

## UX Flow
1. Login -> Dashboard

## Sprints

### Sprint 1: Foundation
Features: Login
Goal: Users can log in

### Sprint 2: Dashboard
Features: Dashboard
Goal: Users see a dashboard
"""

SAMPLE_SPEC_NO_SPRINTS = """# Test App

## Vision
Test app.

## Features

### P0: Login
- Description: User login

## UX Flow
1. Login
"""


def _setup_orch(tmpdir):
    orch = Orchestrator(root_dir=tmpdir)
    orch.setup()
    agents_dir = os.path.join(tmpdir, "agents")
    os.makedirs(agents_dir, exist_ok=True)
    for name in ("planner", "generator", "evaluator"):
        with open(os.path.join(agents_dir, f"{name}.md"), "w") as f:
            f.write(f"# {name} prompt")
    return orch


def test_orchestrator_init_creates_dirs():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        assert os.path.isdir(os.path.join(tmpdir, "comms"))
        assert os.path.isdir(os.path.join(tmpdir, "comms", "screenshots"))
        assert os.path.isdir(os.path.join(tmpdir, "output"))


@pytest.mark.anyio
async def test_orchestrator_plan_phase():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)

        async def mock_planner(*args, **kwargs):
            with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
                f.write(SAMPLE_SPEC)
            return "Spec written."

        with patch("orchestrator.call_agent", side_effect=mock_planner):
            await orch.plan("Build a test app")

        assert os.path.exists(os.path.join(tmpdir, "comms", "spec.md"))
        status = json.load(open(os.path.join(tmpdir, "comms", "status.json")))
        assert status["phase"] == "building"


@pytest.mark.anyio
async def test_orchestrator_plan_parses_sprints():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)

        async def mock_planner(*args, **kwargs):
            with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
                f.write(SAMPLE_SPEC)
            return "Spec written."

        with patch("orchestrator.call_agent", side_effect=mock_planner):
            await orch.plan("Build a test app")

        assert len(orch.sprints) == 2
        assert orch.sprints[0].name == "Foundation"
        assert orch.sprints[1].name == "Dashboard"


@pytest.mark.anyio
async def test_orchestrator_plan_fallback_single_sprint():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)

        async def mock_planner(*args, **kwargs):
            with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
                f.write(SAMPLE_SPEC_NO_SPRINTS)
            return "Spec written."

        with patch("orchestrator.call_agent", side_effect=mock_planner):
            await orch.plan("Build a test app")

        assert len(orch.sprints) == 1
        assert orch.sprints[0].name == "Full Build"


@pytest.mark.anyio
async def test_orchestrator_negotiate_contract():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")

        call_count = 0

        async def mock_negotiate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            prompt = kwargs.get("user_prompt", args[1] if len(args) > 1 else "")
            sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
            os.makedirs(sprint_dir, exist_ok=True)
            if call_count == 1:
                # Generator proposes
                with open(os.path.join(sprint_dir, "contract_proposal.md"), "w") as f:
                    f.write("# Sprint Contract\n## Testable Criteria\n1. Login works")
                return "Proposal written."
            else:
                # Evaluator agrees
                with open(os.path.join(sprint_dir, "contract_review.md"), "w") as f:
                    f.write("AGREED\nLooks good.")
                return "AGREED\nLooks good."

        with patch("orchestrator.call_agent", side_effect=mock_negotiate):
            contract_path = await orch.negotiate_contract(sprint)

        assert os.path.exists(contract_path)
        assert "sprint-1" in contract_path


@pytest.mark.anyio
async def test_orchestrator_build_with_sprint():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n1. Login works")

        mock = AsyncMock(return_value="Built successfully")
        with patch("orchestrator.call_agent", mock):
            await orch.build(sprint, contract_path)

        status = json.load(open(os.path.join(tmpdir, "comms", "status.json")))
        assert status["build_attempts"] == 1


@pytest.mark.anyio
async def test_orchestrator_evaluate_pass():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n1. Login works")

        mock = AsyncMock(return_value="### Verdict: PASS")
        with patch("orchestrator.call_agent", mock):
            with patch.object(orch, "server"):
                passed = await orch.evaluate(sprint, contract_path)

        assert passed is True


@pytest.mark.anyio
async def test_orchestrator_evaluate_fail():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n1. Login works")

        mock = AsyncMock(return_value="### Verdict: FAIL\n### Required Changes\n1. Fix the button")
        with patch("orchestrator.call_agent", mock):
            with patch.object(orch, "server"):
                passed = await orch.evaluate(sprint, contract_path)

        assert passed is False


@pytest.mark.anyio
async def test_slug_ascii_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        spec_with_korean = "# Mystic Arcana — AI 타로 리딩 웹 앱\n\n## Vision\nTest.\n"
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(spec_with_korean)
        orch._init_output()
        assert "타로" not in orch.output_dir
        assert "mystic-arcana" in orch.output_dir
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_orchestrator.py -v`
Expected: Failures due to missing `sprints` attr, changed method signatures

- [ ] **Step 3: Rewrite orchestrator.py**

Replace the full content of `orchestrator.py`:

```python
import anyio
import os
import re
import shutil
import subprocess
import sys
import time

from cli import call_agent
from config import (
    CONFIG, VERDICT_PASS, CONTRACT_AGREED,
    TOOLS_READONLY, TOOLS_FULL, get_harness_root,
)
from sprint import Sprint, parse_sprints
from state import HarnessState
from server import DevServer


class Orchestrator:
    def __init__(self, root_dir: str | None = None):
        self.root = root_dir or get_harness_root()
        self.comms_dir = os.path.join(self.root, CONFIG["comms_dir"])
        self.output_base = os.path.join(self.root, CONFIG["output_dir"])
        self.output_dir = None
        self.agents_dir = os.path.join(self.root, "agents")

        self.spec_path = os.path.join(self.comms_dir, "spec.md")
        self.sprints: list[Sprint] = []

        self._planner_prompt = None
        self._generator_prompt = None
        self._evaluator_prompt = None

        self.state = HarnessState(self.comms_dir)
        self.server = None

    def _load_prompt(self, name: str) -> str:
        path = os.path.join(self.agents_dir, f"{name}.md")
        with open(path) as f:
            return f.read()

    @property
    def planner_prompt(self) -> str:
        if self._planner_prompt is None:
            self._planner_prompt = self._load_prompt("planner")
        return self._planner_prompt

    @property
    def generator_prompt(self) -> str:
        if self._generator_prompt is None:
            self._generator_prompt = self._load_prompt("generator")
        return self._generator_prompt

    @property
    def evaluator_prompt(self) -> str:
        if self._evaluator_prompt is None:
            self._evaluator_prompt = self._load_prompt("evaluator")
        return self._evaluator_prompt

    def _init_output(self):
        """Parse product name from spec and create output/{name}/ directory."""
        with open(self.spec_path) as f:
            spec = f.read()

        match = re.search(r"^#\s+(.+)$", spec, re.MULTILINE)
        if match:
            name = match.group(1).strip()
            slug = re.sub(r"[^a-z0-9\s-]", "", name.lower())
            slug = re.sub(r"[\s]+", "-", slug).strip("-")
        else:
            slug = "app"

        self.output_dir = os.path.join(self.output_base, slug)
        os.makedirs(self.output_dir, exist_ok=True)
        subprocess.run(
            ["git", "init"], cwd=self.output_dir, capture_output=True
        )
        self.server = DevServer(
            self.comms_dir, self.output_dir, CONFIG["dev_server_startup_wait"]
        )
        _log("Harness", f"Project directory: output/{slug}/")

    def _sprint_dir(self, sprint: Sprint) -> str:
        d = os.path.join(self.comms_dir, "sprints", f"sprint-{sprint.number}")
        os.makedirs(d, exist_ok=True)
        os.makedirs(os.path.join(d, "screenshots"), exist_ok=True)
        return d

    def setup(self):
        os.makedirs(self.output_base, exist_ok=True)
        self.state.init()

    async def plan(self, user_prompt: str):
        _log("Planner", "Generating spec...")
        start = time.time()
        prompt = (
            f"User request: {user_prompt}\n\n"
            f"Generate a complete product specification. "
            f"Write the spec to {self.spec_path} using the Write tool."
        )
        await call_agent(
            system_prompt=self.planner_prompt,
            user_prompt=prompt,
            allowed_tools=TOOLS_READONLY,
            cwd=self.root,
        )

        if not os.path.exists(self.spec_path):
            raise RuntimeError("Planner did not write spec.md")

        self._init_output()

        with open(self.spec_path) as f:
            spec = f.read()
        self.sprints = parse_sprints(spec)

        elapsed = int(time.time() - start)
        self.state.record_plan_time(elapsed)
        self.state.set_sprint_info(current=0, total=len(self.sprints))
        self.state.set_phase("building")
        _log("Planner", f"Spec generated — {len(self.sprints)} sprint(s). ({_elapsed(start)})")

    async def negotiate_contract(self, sprint: Sprint) -> str:
        sprint_dir = self._sprint_dir(sprint)
        proposal_path = os.path.join(sprint_dir, "contract_proposal.md")
        review_path = os.path.join(sprint_dir, "contract_review.md")
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")

        with open(self.spec_path) as f:
            spec = f.read()

        _log("Contract", f"Sprint {sprint.number}: Negotiating...")
        start = time.time()

        for round_num in range(CONFIG["max_negotiation_rounds"]):
            # Generator proposes
            review_context = ""
            if os.path.exists(review_path):
                with open(review_path) as f:
                    review_context = f"\n\nEvaluator's review of your previous proposal:\n{f.read()}"

            gen_prompt = (
                f"Propose a sprint contract for Sprint {sprint.number}: {sprint.name}.\n\n"
                f"Sprint scope — Features: {', '.join(sprint.features)}. "
                f"Goal: {sprint.goal}\n\n"
                f"Full product spec:\n{spec}\n"
                f"{review_context}\n\n"
                f"Write your contract proposal to {proposal_path} using the Write tool. "
                f"Use the Contract Proposal Mode described in your instructions."
            )
            await call_agent(
                system_prompt=self.generator_prompt,
                user_prompt=gen_prompt,
                allowed_tools=TOOLS_READONLY,
                cwd=self.root,
            )

            # Evaluator reviews
            with open(proposal_path) as f:
                proposal = f.read()

            eval_prompt = (
                f"Review this sprint contract proposal for Sprint {sprint.number}: {sprint.name}.\n\n"
                f"Proposal:\n{proposal}\n\n"
                f"Full product spec:\n{spec}\n\n"
                f"Write your review to {review_path} using the Write tool. "
                f"Use the Contract Review Mode described in your instructions. "
                f"If the criteria are complete and testable, write 'AGREED' at the top."
            )
            review_result = await call_agent(
                system_prompt=self.evaluator_prompt,
                user_prompt=eval_prompt,
                allowed_tools=TOOLS_READONLY,
                cwd=self.root,
            )

            if CONTRACT_AGREED in review_result:
                shutil.copy(proposal_path, contract_path)
                _log("Contract", f"Sprint {sprint.number}: Agreed in round {round_num + 1}. ({_elapsed(start)})")
                break
        else:
            # No agreement — use last proposal
            shutil.copy(proposal_path, contract_path)
            _log("Contract", f"Sprint {sprint.number}: No agreement — using last proposal. ({_elapsed(start)})")

        elapsed = int(time.time() - start)
        self.state.add_sprint_timing(sprint.number, "negotiate", elapsed)
        return contract_path

    async def build(self, sprint: Sprint, contract_path: str):
        with open(self.spec_path) as f:
            spec = f.read()
        with open(contract_path) as f:
            contract = f.read()

        sprint_dir = self._sprint_dir(sprint)
        eval_path = os.path.join(sprint_dir, "evaluation.md")
        eval_context = ""
        try:
            with open(eval_path) as f:
                eval_context = f"\n\nPrevious evaluation feedback:\n{f.read()}"
        except FileNotFoundError:
            pass

        self.state.increment_build()
        status = self.state.load()
        attempt = status["build_attempts"]

        _log("Generator", f"Sprint {sprint.number} — Building (attempt {attempt})...")
        start = time.time()
        prompt = (
            f"Build Sprint {sprint.number}: {sprint.name}.\n\n"
            f"Sprint contract:\n{contract}\n\n"
            f"Full product spec:\n{spec}\n"
            f"{eval_context}\n\n"
            f"Implement ALL criteria in the sprint contract. "
            f"Work in the current directory. Self-verify: build must succeed, app must run. "
            f"Commit your changes with git."
        )

        await call_agent(
            system_prompt=self.generator_prompt,
            user_prompt=prompt,
            allowed_tools=TOOLS_FULL,
            cwd=self.output_dir,
        )
        elapsed = int(time.time() - start)
        self.state.add_sprint_timing(sprint.number, "build", elapsed)
        _log("Generator", f"Sprint {sprint.number} — Build complete. ({_elapsed(start)})")

    async def evaluate(self, sprint: Sprint, contract_path: str) -> bool:
        with open(self.spec_path) as f:
            spec = f.read()
        with open(contract_path) as f:
            contract = f.read()

        sprint_dir = self._sprint_dir(sprint)
        eval_path = os.path.join(sprint_dir, "evaluation.md")

        self.state.increment_eval()
        status = self.state.load()
        attempt = status["eval_attempts"]

        _log("Evaluator", f"Sprint {sprint.number} — Starting servers...")
        self.server.start()
        _log("Evaluator", f"Sprint {sprint.number} — Testing (attempt {attempt})...")
        start = time.time()
        try:
            prompt = (
                f"Evaluate Sprint {sprint.number}: {sprint.name}.\n\n"
                f"Sprint contract (test against these criteria):\n{contract}\n\n"
                f"Product spec:\n{spec}\n\n"
                f"Navigate to {CONFIG['dev_server_url']}. Test every criterion in the sprint contract. "
                f"Take screenshots as evidence. Write your evaluation as a response."
            )

            eval_result = await call_agent(
                system_prompt=self.evaluator_prompt,
                user_prompt=prompt,
                allowed_tools=TOOLS_READONLY,
                mcp_tools=[CONFIG["mcp_tool"]],
                cwd=self.root,
            )

            with open(eval_path, "w") as f:
                f.write(eval_result)
        finally:
            _log("Evaluator", f"Sprint {sprint.number} — Stopping servers...")
            self.server.stop()

        elapsed = int(time.time() - start)
        self.state.add_sprint_timing(sprint.number, "eval", elapsed)

        passed = VERDICT_PASS in eval_result
        if passed:
            _log("Evaluator", f"Sprint {sprint.number} — PASS ({_elapsed(start)})")
        else:
            _log("Evaluator", f"Sprint {sprint.number} — FAIL ({_elapsed(start)})")
            for line in eval_result.split("\n"):
                if line.strip().startswith(("1.", "2.", "3.")):
                    print(f"    {line.strip()}")
        return passed

    async def run(self, user_prompt: str):
        print("=" * 60)
        print("  Multi-Agent Coding Harness")
        print("=" * 60)
        total_start = time.time()

        self.setup()
        await self.plan(user_prompt)

        for sprint in self.sprints:
            self.state.set_sprint_info(current=sprint.number, total=len(self.sprints))
            _log("Harness", f"=== Sprint {sprint.number}/{len(self.sprints)}: {sprint.name} ===")

            contract_path = await self.negotiate_contract(sprint)

            sprint_passed = False
            for attempt in range(CONFIG["max_build_attempts"]):
                await self.build(sprint, contract_path)
                passed = await self.evaluate(sprint, contract_path)
                if passed:
                    sprint_passed = True
                    break

                if attempt == CONFIG["max_build_attempts"] - 1:
                    _log("Harness", f"Sprint {sprint.number} failed after {CONFIG['max_build_attempts']} attempts.")

            if not sprint_passed:
                _log("Harness", f"Sprint {sprint.number} incomplete — continuing to next sprint.")

        self.state.set_phase("completed")
        self._print_report(total_start)

    def _print_report(self, total_start: float):
        status = self.state.load()
        print("\n" + "=" * 60)
        print("  FINAL REPORT")
        print("=" * 60)
        print(f"  Project:        {self.output_dir}")
        print(f"  Sprints:        {status.get('total_sprints', 1)}")
        print(f"  Build attempts: {status['build_attempts']}")
        print(f"  Eval attempts:  {status['eval_attempts']}")

        timings = status.get("timings", {})
        if timings.get("plan"):
            print(f"  Planning:       {_elapsed_secs(timings['plan'])}")
        for st in timings.get("sprints", []):
            parts = []
            if "negotiate" in st:
                parts.append(f"negotiate: {_elapsed_secs(st['negotiate'])}")
            if "build" in st:
                parts.append(f"build: {' + '.join(_elapsed_secs(b) for b in st['build'])}")
            if "eval" in st:
                parts.append(f"eval: {' + '.join(_elapsed_secs(e) for e in st['eval'])}")
            print(f"  Sprint {st['sprint']}:      {', '.join(parts)}")

        print(f"  Total time:     {_elapsed(total_start)}")
        print("=" * 60)


def _log(agent: str, msg: str):
    print(f"[{agent}] {msg}")


def _elapsed(start: float) -> str:
    return _elapsed_secs(int(time.time() - start))


def _elapsed_secs(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    secs = secs % 60
    if mins < 60:
        return f"{mins}m {secs}s"
    hours = mins // 60
    mins = mins % 60
    return f"{hours}h {mins}m {secs}s"


def main():
    if len(sys.argv) < 2:
        print("Usage: python orchestrator.py \"<your product idea>\"")
        sys.exit(1)

    user_prompt = " ".join(sys.argv[1:])
    orch = Orchestrator()
    anyio.run(orch.run, user_prompt)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run all tests**

Run: `cd /Users/jack/client/harness && python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator.py tests/test_orchestrator.py
git commit -m "feat: sprint-based orchestrator with contract negotiation"
```

---

### Task 8: Final integration test and push

- [ ] **Step 1: Run full test suite**

Run: `cd /Users/jack/client/harness && python -m pytest tests/ -v`
Expected: All tests across all files PASS

- [ ] **Step 2: Verify imports work end-to-end**

Run: `cd /Users/jack/client/harness && python -c "from orchestrator import Orchestrator; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit plan doc**

```bash
git add docs/
git commit -m "docs: add implementation plan for blog alignment"
```

- [ ] **Step 4: Push all changes**

```bash
git push origin main
```
