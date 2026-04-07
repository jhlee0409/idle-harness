import os
from urllib.parse import urlparse

CONFIG = {
    # --- Harness mode ---
    # "auto": select based on model (opus/sonnet → simple, haiku → full)
    # "simple": single build + single evaluation (high-capability models, large context)
    # "full": sprint decomposition + contract negotiation (smaller models, limited context)
    "mode": "auto",

    # --- Model ---
    # Default model (used when per-agent model is not set)
    "model": "claude-opus-4-6",
    # Per-agent models: None = use default "model" above
    "model_planner": "claude-sonnet-4-6",
    "model_evaluator": None,  # Opus — Sonnet was slower (2x turns) and ignored Evidence Rules
    "model_generator": None,  # None → falls back to "model" (opus)

    # --- Build / eval limits ---
    "max_build_attempts": 10,
    "max_design_iterations": 10,       # per article: 5-15 iterations for frontend design
    "max_negotiation_rounds": 3,
    "generator_max_turns": 200,

    # --- Agent timeouts (seconds, 0 = no limit) ---
    "agent_timeout_planner": 600,       # 10 min
    "agent_timeout_criteria": 600,      # 10 min — testable criteria generation from spec
    "agent_timeout_negotiate": 300,     # 5 min per round
    "agent_timeout_build": 9000,        # 2.5 hr — article's DAW Build Round 1 was 2hr 7min
    "agent_timeout_eval": 0,            # no limit — Evaluator must test ALL criteria, not rush
    "agent_timeout_integration": 0,     # no limit

    # --- Dev server ---
    "dev_server_start_cmd": None,
    "dev_server_url": "http://localhost:8005",
    "backend_url": "http://localhost:8006",
    "dev_server_startup_wait": 5,
    "dev_server_health_timeout": 60,

    # --- Directories ---
    "output_dir": "output",
    "comms_dir": "comms",

    # --- Verifier ---
    "verifier_enabled": True,
    "verifier_timeout": 300,            # total 5 min for all checks
    "verifier_check_timeout": 30,       # per-check timeout
    "verifier_retry_inconclusive": 1,   # retry count for INCONCLUSIVE results
    "automation_limited_allowlist": [
        "file_upload",      # OS native file picker
        "oauth_redirect",   # third-party OAuth
        "email_verify",     # email delivery
        "sms_verify",       # SMS delivery
        "payment",          # payment provider
        "maps_embed",       # third-party maps
    ],

    # --- MCP / tools ---
    "mcp_tool": "playwright",
    "mcp_servers": {
        "playwright": {
            "command": "npx",
            "args": ["@anthropic-ai/mcp-server-playwright"],
        },
    },
}

CONTRACT_AGREED = "AGREED"

TOOLS_READ_WRITE = ["Read", "Write"]
TOOLS_FULL = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

# Evaluator: Write only — no Read prevents source code access (GAN principle).
# Contracts and specs are passed directly in the prompt, so Read is unnecessary.
# Note: MCP tools (Playwright) are added automatically by the SDK when mcp_servers
# is passed to call_agent(). They bypass the allowed_tools filter.
TOOLS_EVALUATOR = ["Write"]

_HARNESS_ROOT = os.path.dirname(os.path.abspath(__file__))


def get_harness_root() -> str:
    return _HARNESS_ROOT


def resolve_mode() -> str:
    """Resolve harness mode. 'auto' selects based on model capability."""
    mode = CONFIG["mode"]
    if mode != "auto":
        return mode
    model = CONFIG.get("model", "")
    # Opus: 1M context, high capability → single-pass build
    if "opus" in model:
        return "simple"
    # Haiku: smaller context, lower capability → sprint decomposition
    if "haiku" in model:
        return "full"
    # Sonnet and others: default to simple (adequate for most apps)
    return "simple"


def resolve_agent_model(role: str) -> str:
    """Resolve the model for a given agent role (planner/generator/evaluator).

    Per-agent model keys override the default. None or missing → default model.
    """
    per_agent = CONFIG.get(f"model_{role}")
    if per_agent:
        return per_agent
    return CONFIG.get("model", "claude-opus-4-6")


def get_server_ports() -> list[int]:
    """Parse ports from configured URLs."""
    ports = []
    for key in ("dev_server_url", "backend_url"):
        url = CONFIG.get(key, "")
        if url:
            parsed = urlparse(url)
            if parsed.port:
                ports.append(parsed.port)
    return sorted(set(ports))
