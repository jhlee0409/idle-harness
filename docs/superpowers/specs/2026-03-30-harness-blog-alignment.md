# Harness Blog Alignment: Complete Redesign Spec

> Align the multi-agent coding harness with all patterns described in
> [Anthropic's harness design article](https://www.anthropic.com/engineering/harness-design-long-running-apps).

## Scope

**Full implementation (blog-faithful):**
1. Sprint Decomposition
2. Sprint Contracts (Generator ↔ Evaluator negotiation)
3. Evaluator Few-Shot Calibration
4. Evaluator Anti-Rationalization

**Lightweight implementation:**
5. Sprint Decomposition for complex apps (Planner decides sprint count)
6. Cost/token tracking in final report

---

## 1. Sprint Decomposition

### Current State

Single loop: `plan → (build → eval) × max_attempts`. Generator builds the entire app in one shot. No concept of sprints.

### Target State

Planner decomposes the spec into ordered sprints. Each sprint is an independent build-eval cycle with its own contract.

### Planner Output Change

Planner's spec gains a `## Sprints` section at the end:

```markdown
## Sprints

### Sprint 1: [Name]
Features: P0-Feature-A, P0-Feature-B
Goal: [What "done" looks like for this sprint]

### Sprint 2: [Name]
Features: P0-Feature-C, P1-Feature-A
Goal: [...]

### Sprint 3: [Name]
Features: P1-Feature-B, P2-Feature-A
Goal: [...]
```

**Rules for Planner sprint design:**
- P0 features go in earlier sprints
- Each sprint should be independently testable (the app runs after each sprint)
- Sprint 1 always includes project scaffolding + core feature
- Sprints build incrementally; later sprints assume prior sprints are done
- Typical app: 2-4 sprints. Simple app: 1 sprint. Complex app: up to 6

### Orchestrator Flow Change

```python
async def run(user_prompt):
    setup()
    await plan(user_prompt)
    sprints = parse_sprints(spec)  # Extract sprint list from spec

    for sprint in sprints:
        contract = await negotiate_contract(sprint)

        for attempt in range(max_build_attempts):
            await build(sprint, contract)
            passed = await evaluate(sprint, contract)
            if passed:
                break
            if attempt == max_build_attempts - 1:
                log(f"Sprint {sprint.name} failed after {max_build_attempts} attempts")
                # Continue to next sprint anyway - partial progress > nothing

    set_phase("completed")
    print_report()
```

### Sprint Parsing

Parse the `## Sprints` section from spec.md. Each sprint becomes a dataclass:

```python
@dataclass
class Sprint:
    number: int
    name: str
    features: list[str]
    goal: str
```

If no `## Sprints` section exists (backward compat), treat the entire spec as a single sprint.

---

## 2. Sprint Contracts (Generator ↔ Evaluator Negotiation)

### Current State

No negotiation. Generator builds from spec directly. Evaluator tests with its own interpretation of the spec.

### Target State

Before each sprint, Generator and Evaluator negotiate testable criteria via file exchange. Blog: "The generator proposed what it would build and how success would be verified, and the evaluator reviewed that proposal... The two iterated until they agreed."

### Negotiation Flow

```
1. Generator writes comms/sprints/sprint-N/contract_proposal.md
   - What it will build
   - Specific testable criteria ("user can click X and see Y")
   - Design decisions it plans to make

2. Evaluator reads proposal, writes comms/sprints/sprint-N/contract_review.md
   - Criteria it considers insufficient
   - Additional criteria it wants
   - Concerns about testability

3. Generator reads review, writes updated contract_proposal.md
   - Addresses evaluator's concerns
   - Adds/modifies criteria

4. Repeat until Evaluator writes "AGREED" in review
   - Max 3 negotiation rounds to prevent infinite loops
   - If no agreement after 3 rounds, use Generator's latest proposal
```

### Contract Format

```markdown
# Sprint Contract: Sprint N - [Name]

## Scope
[What this sprint delivers]

## Testable Criteria
1. [ ] [Criterion] — [How to verify]
2. [ ] [Criterion] — [How to verify]
...

## Design Decisions
- [Decision and rationale]

## Out of Scope
- [What is NOT part of this sprint]
```

### File Structure

```
comms/
  spec.md
  sprints/
    sprint-1/
      contract_proposal.md    # Generator writes
      contract_review.md      # Evaluator writes
      sprint_contract.md      # Final agreed contract (copy of last proposal after AGREED)
      evaluation.md           # Evaluation result
      screenshots/            # Evidence for this sprint
    sprint-2/
      ...
  status.json                 # Updated with sprint tracking
```

### Agent Prompts for Negotiation

**Generator (contract proposal mode):**
System prompt addition for contract phase:
```
You are proposing a sprint contract. Read the sprint scope from the spec.
Write specific, testable criteria that the Evaluator can verify by interacting
with the running application. Each criterion must be independently verifiable
with a concrete user action and expected result. Aim for completeness —
the blog reference had 27 criteria for a single sprint's level editor.
```

**Evaluator (contract review mode):**
System prompt addition for review phase:
```
You are reviewing a sprint contract proposal. Your job is to ensure the
criteria are specific enough to test. Reject vague criteria. Add criteria
the Generator missed. Think about edge cases, error states, and design
quality. When satisfied, write "AGREED" at the top of your review.
```

### Orchestrator Implementation

```python
async def negotiate_contract(self, sprint: Sprint) -> str:
    sprint_dir = os.path.join(self.comms_dir, "sprints", f"sprint-{sprint.number}")
    os.makedirs(sprint_dir, exist_ok=True)
    proposal_path = os.path.join(sprint_dir, "contract_proposal.md")
    review_path = os.path.join(sprint_dir, "contract_review.md")
    contract_path = os.path.join(sprint_dir, "sprint_contract.md")

    for round in range(max_negotiation_rounds):  # max 3
        # Generator proposes
        await call_agent(generator_prompt_contract, ...)
        # Evaluator reviews
        review = await call_agent(evaluator_prompt_review, ...)
        if "AGREED" in review:
            # Copy final proposal as contract
            shutil.copy(proposal_path, contract_path)
            break

    return contract_path
```

---

## 3. Evaluator Few-Shot Calibration

### Current State

Evaluator prompt has rules and output format, but no concrete examples. No calibration for what PASS vs FAIL looks like in practice.

### Target State

Blog: "I calibrated the evaluator using few-shot examples with detailed score breakdowns. This ensured the evaluator's judgment aligned with my preferences, and reduced score drift across iterations."

### Implementation

Add a `## Calibration Examples` section to `evaluator.md` with 2 concrete examples: one PASS and one FAIL.

**FAIL Example (generic template):**
```markdown
### Example Evaluation: FAIL

#### Feature Testing
- [x] Task creation (clicked "Add Task", typed "Buy milk", pressed Enter — task appeared in list)
- [x] Task completion (clicked checkbox — task got strikethrough)
- [ ] Task categories BROKEN (clicked "Work" category filter — showed all tasks instead of filtered) | screenshots/cat-fail.png

#### Design Assessment
| Criterion | Verdict | Evidence |
|-----------|---------|----------|
| Design Quality | FAIL | White background, default sans-serif, no visual hierarchy. Looks like an unstyled HTML page with Tailwind utility classes. No cohesive mood or identity. |
| Originality | FAIL | Default blue buttons (#3b82f6), white cards with light gray borders, no custom design decisions. Indistinguishable from a tutorial starter template. |
| Craft | PASS | Spacing is consistent, text is readable, buttons are aligned. |
| Functionality | PASS | Core CRUD works end-to-end with database persistence. |

#### Verdict: FAIL

#### Required Changes
1. Replace default white (#ffffff) background with a warm surface color that establishes mood (e.g., #1a1a2e for dark theme or #faf8f5 for earth tone)
2. Replace default blue buttons with a palette-coherent accent color
3. Add visual hierarchy: headings need distinct weight/size, cards need depth (shadow or border treatment)
4. Category filter returns all tasks — filter logic is broken, should show only matching category
```

**PASS Example (well-designed app):**
```markdown
### Example Evaluation: PASS

#### Feature Testing
- [x] Card draw animation (clicked "Draw" — 3 cards fanned out with smooth 0.3s ease transition) | screenshots/draw.png
- [x] AI interpretation (selected cards — streaming text appeared within 2s, contextually relevant) | screenshots/interp.png
- [x] Data persistence (created reading, refreshed page — reading appeared in history with timestamp) | screenshots/persist.png

#### Design Assessment
| Criterion | Verdict | Evidence |
|-----------|---------|----------|
| Design Quality | PASS | Deep navy (#0A0A1A) background with gold (#D4AF37) accents creates a mystical, cohesive atmosphere. Card faces use custom illustrations. Typography hierarchy is clear: Cinzel for headings, Noto Sans for body. |
| Originality | PASS | Custom card fan layout, gold particle effects on draw, glassmorphism panels with purple-to-transparent gradient. Not a template — deliberate aesthetic choices throughout. |
| Craft | PASS | 4px spacing grid consistent. Contrast ratios exceed 4.5:1. Smooth animations with no jank. Typography sizes follow clear hierarchy (32/24/16/14). |
| Functionality | PASS | Full flow works: select spread → draw cards → get AI reading → save to history. Data persists in SQLite via API. |

#### Verdict: PASS
```

### Key Calibration Principles (added to prompt)

```
- A functional app with default styling is a FAIL, not a PASS
- "It works" is not enough — it must also look intentionally designed
- Purple gradients over white cards = automatic FAIL (AI slop indicator)
- If you find yourself writing "while not perfect, it's acceptable" — that means FAIL
- When in doubt, FAIL. The generator gets another attempt.
```

---

## 4. Evaluator Anti-Rationalization

### Current State

Prompt says "Be skeptical by default" and "Your job is to find problems." Insufficient — blog says Claude identifies issues then rationalizes them away.

### Target State

Blog: "I watched it identify legitimate issues, then talk itself into deciding they weren't a big deal and approve the work anyway." Solution: explicit anti-rationalization rules and structural changes to prevent this.

### Prompt Additions to evaluator.md

```markdown
## Anti-Rationalization Rules

You have a documented tendency to identify real problems and then convince yourself
they're acceptable. This is your primary failure mode. Guard against it:

1. **Never qualify a problem away.** If you write "while X isn't ideal, it's still
   acceptable because..." — STOP. X is a problem. Mark it FAIL.
2. **No benefit of the doubt.** Don't assume unseen features work. If you can't
   verify it, it's not implemented.
3. **Score first, justify second.** Write your PASS/FAIL verdict for each criterion
   BEFORE writing the evidence paragraph. This prevents your justification from
   talking you out of the score you know is right.
4. **Broken means broken.** A feature that works 80% of the way is not a PASS.
   A design that's "close but not quite matching the spec" is a FAIL.
5. **Compare to spec literally.** If the spec says hex #D4AF37 gold and the app
   uses #FFD700 generic gold, that's a design FAIL.
6. **When in doubt, FAIL.** The generator gets another attempt. A false PASS
   wastes an entire build-eval cycle. A false FAIL costs one retry.
```

### Structural Change: Score-First Format

Change the evaluation output format so verdicts come before evidence:

```markdown
### Design Assessment

| Criterion | Verdict |
|-----------|---------|
| Design Quality | FAIL |
| Originality | FAIL |
| Craft | PASS |
| Functionality | PASS |

#### Evidence
- **Design Quality (FAIL):** [evidence here]
- **Originality (FAIL):** [evidence here]
- **Craft (PASS):** [evidence here]
- **Functionality (PASS):** [evidence here]
```

This forces the Evaluator to commit to scores before it can rationalize.

### Testing Depth

Blog: "It also tended to test superficially, rather than probing edge cases."

Add to evaluator prompt:
```markdown
## Testing Depth

Do not stop at the happy path. For every feature:
1. Test the normal flow (happy path)
2. Test with empty input
3. Test with invalid input
4. Test after page refresh (persistence check)
5. Test rapid repeated actions (double-click, rapid submit)

A feature that works on the happy path but breaks on empty input is a FAIL.
```

---

## 5. Generator Strategic Decision After Evaluation

### Current State

On retry, Generator just reads evaluation feedback and fixes issues. No strategic thinking.

### Target State

Blog: "I instructed the generator to make a strategic decision after each evaluation: refine the current direction if scores were trending well, or pivot to an entirely different aesthetic if the approach wasn't working."

### Prompt Addition to generator.md

```markdown
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

State your decision explicitly at the start: "STRATEGY: REFINE — [reason]" or
"STRATEGY: PIVOT — [reason]". Then proceed accordingly.

A pivot means: new color palette, new typography, new component style, new layout
approach. Keep the functionality but redesign the visual identity from scratch.
```

---

## 6. Cost/Time Tracking (Lightweight)

### Current State

Only time tracking via `_elapsed()`. No cost or token tracking.

### Target State

Track per-phase timing in status.json. No token tracking (Agent SDK doesn't expose it easily).

### status.json Change

```json
{
  "phase": "building",
  "build_attempts": 2,
  "eval_attempts": 2,
  "current_sprint": 2,
  "total_sprints": 3,
  "timings": {
    "plan": 280,
    "sprints": [
      {
        "sprint": 1,
        "negotiate": 45,
        "build": [320, 180],
        "eval": [60, 55]
      }
    ]
  }
}
```

### Final Report Change

```
============================================================
  FINAL REPORT
============================================================
  Project:        output/mystic-arcana
  Sprints:        3/3 completed
  Total attempts: 5 builds, 5 evals
  Timing:
    Planning:     4m 40s
    Sprint 1:     8m 20s (negotiate: 45s, build: 5m 20s + 3m, eval: 1m + 55s)
    Sprint 2:     6m 10s (negotiate: 30s, build: 4m 40s, eval: 1m)
    Sprint 3:     5m 30s (negotiate: 35s, build: 4m, eval: 55s)
  Total time:     24m 40s
============================================================
```

---

## 7. Slug Fix (Already Identified)

### Change

`orchestrator.py` line 63: `[^\w\s-]` → `[^a-z0-9\s-]` to prevent non-ASCII characters in directory names.

---

## 8. Config Changes

```python
CONFIG = {
    "max_build_attempts": 3,
    "max_negotiation_rounds": 3,        # NEW: contract negotiation rounds
    "dev_server_start_cmd": None,
    "dev_server_stop_cmd": None,
    "dev_server_url": "http://localhost:5173",
    "dev_server_startup_wait": 5,
    "output_dir": "output",
    "comms_dir": "comms",
    "mcp_tool": "chrome-devtools",
}

CONTRACT_AGREED = "AGREED"               # NEW: contract agreement marker
```

---

## 9. File Changes Summary

| File | Change Type | Description |
|------|-------------|-------------|
| `orchestrator.py` | Major rewrite | Sprint loop, contract negotiation, per-sprint build/eval, timing |
| `agents/planner.md` | Edit | Add Sprint decomposition rules and output format |
| `agents/generator.md` | Edit | Add contract proposal mode, strategic decision on retry |
| `agents/evaluator.md` | Major edit | Few-shot examples, anti-rationalization rules, score-first format, testing depth, contract review mode |
| `config.py` | Edit | Add max_negotiation_rounds, CONTRACT_AGREED |
| `state.py` | Edit | Add sprint tracking, timing data |
| `cli.py` | No change | — |
| `server.py` | No change | — |
| `tests/` | Update | Reflect new sprint-based orchestrator flow |

---

## 10. Backward Compatibility

- If spec has no `## Sprints` section, treat entire spec as single sprint (no negotiation, behaves like current code)
- All existing config keys unchanged
- comms/ file structure is additive (sprints/ is new, spec.md and evaluation.md still exist at root for single-sprint mode)
