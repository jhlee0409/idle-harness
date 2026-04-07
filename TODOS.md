# TODOS

## Deferred Work

### Parallel Evaluator
**What:** Split subjective criteria across 3-5 parallel evaluator agents with separate MCP Playwright instances.
**Why:** Sequential eval of ~50 subjective criteria takes ~15min. Parallel = ~5min. Codex flagged sequential as "throughput suicide for iterative loop."
**Pros:** 3-5x speed improvement on eval phase.
**Cons:** Requires multiple MCP server instances, result merge logic, resource management.
**Context:** Deferred from verifier PR (quality first, speed second). Once verifier proves the criteria split (deterministic vs subjective) works, this becomes straightforward.
**Depends on:** verifier.py shipping and proving the typed criteria classification works.

### Gold Set Calibration
**What:** Build a set of known-good and known-bad apps to calibrate verifier false-positive and false-negative rates.
**Why:** Codex pointed out 18 unit tests don't measure real-world accuracy. Need to measure: does the verifier catch real defects without blocking good apps?
**Depends on:** 5+ harness runs with verifier enabled to collect calibration data.

### Deploy Pipeline
**What:** Auto-deploy PASS apps to Vercel/Fly/Railway with domain setup and SSL.
**Why:** "Full product factory" vision. One prompt to live URL.
**Depends on:** Trustworthy PASS verdict (verifier + strict evaluator).

### Learning Loop
**What:** After each run, save what worked/failed to a knowledge base. Next run starts smarter.
**Why:** Compound improvement across runs.
**Depends on:** Reliable eval data (no false PASSes polluting the knowledge base).
