import os
from urllib.parse import urlparse

CONFIG = {
    # --- Harness mode ---
    # "full": sprint decomposition + contract negotiation + multi-eval
    # "simple": single build + single end-of-build evaluation (no sprints/contracts)
    "mode": "full",

    # --- Build / eval limits ---
    "max_build_attempts": 3,
    "max_negotiation_rounds": 3,
    "generator_max_turns": 200,

    # --- Agent timeouts (seconds, 0 = no limit) ---
    "agent_timeout_planner": 600,       # 10 min
    "agent_timeout_negotiate": 300,     # 5 min per round
    "agent_timeout_build": 1800,        # 30 min
    "agent_timeout_eval": 1800,         # 30 min
    "agent_timeout_integration": 1800,  # 30 min

    # --- Dev server ---
    "dev_server_start_cmd": None,
    "dev_server_stop_cmd": None,
    "dev_server_url": "http://localhost:5173",
    "backend_url": "http://localhost:8000",
    "dev_server_startup_wait": 5,
    "dev_server_health_timeout": 60,

    # --- Directories ---
    "output_dir": "output",
    "comms_dir": "comms",

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

TOOLS_READONLY = ["Read", "Write"]
TOOLS_FULL = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

# Evaluator: Write only — no Read prevents source code access (GAN principle).
# Contracts and specs are passed directly in the prompt, so Read is unnecessary.
TOOLS_EVALUATOR = ["Write"]

_HARNESS_ROOT = os.path.dirname(os.path.abspath(__file__))


def get_harness_root() -> str:
    return _HARNESS_ROOT


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
