from config import CONFIG, CONTRACT_AGREED, TOOLS_READ_WRITE, TOOLS_FULL, TOOLS_EVALUATOR, get_server_ports


def test_config_has_required_keys():
    required = [
        "mode",
        "max_build_attempts",
        "max_negotiation_rounds",
        "generator_max_turns",
        "agent_timeout_criteria",
        "dev_server_start_cmd",
        "dev_server_url",
        "backend_url",
        "dev_server_startup_wait",
        "dev_server_health_timeout",
        "output_dir",
        "comms_dir",
        "mcp_tool",
    ]
    for key in required:
        assert key in CONFIG, f"Missing config key: {key}"


def test_config_defaults():
    assert CONFIG["mode"] == "simple"
    assert CONFIG["max_build_attempts"] == 3
    assert CONFIG["max_negotiation_rounds"] == 3
    assert CONFIG["generator_max_turns"] == 200
    assert CONFIG["dev_server_start_cmd"] is None
    assert CONFIG["dev_server_url"] == "http://localhost:5173"
    assert CONFIG["backend_url"] == "http://localhost:8000"
    assert CONFIG["dev_server_health_timeout"] == 60
    assert CONFIG["output_dir"] == "output"
    assert CONFIG["comms_dir"] == "comms"
    assert CONFIG["mcp_tool"] == "playwright"


def test_constants():
    assert "Read" in TOOLS_READ_WRITE
    assert "Bash" in TOOLS_FULL


def test_contract_agreed_constant():
    assert CONTRACT_AGREED == "AGREED"


def test_tools_evaluator_no_read():
    """Evaluator must NOT have Read tool — GAN principle prevents source code access."""
    assert "Read" not in TOOLS_EVALUATOR
    assert "Write" in TOOLS_EVALUATOR


def test_get_server_ports():
    ports = get_server_ports()
    assert 5173 in ports
    assert 8000 in ports
    assert ports == sorted(set(ports))
