import os

CONFIG = {
    "max_build_attempts": 3,
    "dev_server_start_cmd": None,
    "dev_server_stop_cmd": None,
    "dev_server_url": "http://localhost:5173",
    "dev_server_startup_wait": 5,
    "output_dir": "output",
    "comms_dir": "comms",
    "mcp_tool": "chrome-devtools",
    "max_negotiation_rounds": 3,
}

VERDICT_PASS = "Verdict: PASS"
CONTRACT_AGREED = "AGREED"

TOOLS_READONLY = ["Read", "Write"]
TOOLS_FULL = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

_HARNESS_ROOT = os.path.dirname(os.path.abspath(__file__))


def get_harness_root() -> str:
    return _HARNESS_ROOT
