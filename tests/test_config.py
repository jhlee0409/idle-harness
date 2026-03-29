from config import CONFIG, VERDICT_PASS, TOOLS_READONLY, TOOLS_FULL


def test_config_has_required_keys():
    required = [
        "max_build_attempts",
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
    assert CONFIG["dev_server_start_cmd"] is None
    assert CONFIG["dev_server_url"] == "http://localhost:5173"
    assert CONFIG["output_dir"] == "output"
    assert CONFIG["comms_dir"] == "comms"
    assert CONFIG["mcp_tool"] == "chrome-devtools"


def test_constants():
    assert VERDICT_PASS == "Verdict: PASS"
    assert "Read" in TOOLS_READONLY
    assert "Bash" in TOOLS_FULL
