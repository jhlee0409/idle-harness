from config import CONFIG, CONTRACT_AGREED, TOOLS_READ_WRITE, TOOLS_FULL, TOOLS_EVALUATOR, get_server_ports, resolve_mode, resolve_agent_model


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
    assert CONFIG["mode"] == "auto"
    assert CONFIG["max_build_attempts"] == 10
    assert CONFIG["max_negotiation_rounds"] == 3
    assert CONFIG["generator_max_turns"] == 200
    assert CONFIG["dev_server_start_cmd"] is None
    assert CONFIG["dev_server_url"] == "http://localhost:8005"
    assert CONFIG["backend_url"] == "http://localhost:8006"
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


def test_resolve_mode_auto_opus():
    from unittest.mock import patch
    with patch.dict("config.CONFIG", {"mode": "auto", "model": "claude-opus-4-6"}):
        assert resolve_mode() == "simple"


def test_resolve_mode_auto_haiku():
    from unittest.mock import patch
    with patch.dict("config.CONFIG", {"mode": "auto", "model": "claude-haiku-4-5-20251001"}):
        assert resolve_mode() == "full"


def test_resolve_mode_auto_sonnet():
    from unittest.mock import patch
    with patch.dict("config.CONFIG", {"mode": "auto", "model": "claude-sonnet-4-6"}):
        assert resolve_mode() == "simple"


def test_resolve_mode_explicit_overrides_auto():
    from unittest.mock import patch
    with patch.dict("config.CONFIG", {"mode": "full", "model": "claude-opus-4-6"}):
        assert resolve_mode() == "full"


def test_config_has_verifier_keys():
    for key in ("verifier_enabled", "verifier_timeout", "verifier_check_timeout",
                "verifier_retry_inconclusive", "automation_limited_allowlist"):
        assert key in CONFIG, f"Missing config key: {key}"


def test_automation_limited_allowlist_defaults():
    allowlist = CONFIG["automation_limited_allowlist"]
    assert "file_upload" in allowlist
    assert "oauth_redirect" in allowlist
    assert isinstance(allowlist, list)


def test_config_has_per_agent_model_keys():
    for key in ("model_planner", "model_evaluator", "model_generator"):
        assert key in CONFIG, f"Missing config key: {key}"


def test_resolve_agent_model_with_per_agent():
    from unittest.mock import patch
    with patch.dict("config.CONFIG", {
        "model": "claude-opus-4-6",
        "model_planner": "claude-sonnet-4-6",
        "model_evaluator": "claude-sonnet-4-6",
        "model_generator": None,
    }):
        assert resolve_agent_model("planner") == "claude-sonnet-4-6"
        assert resolve_agent_model("evaluator") == "claude-sonnet-4-6"
        assert resolve_agent_model("generator") == "claude-opus-4-6"  # None → fallback


def test_resolve_agent_model_all_default():
    from unittest.mock import patch
    with patch.dict("config.CONFIG", {
        "model": "claude-opus-4-6",
        "model_planner": None,
        "model_evaluator": None,
        "model_generator": None,
    }):
        assert resolve_agent_model("planner") == "claude-opus-4-6"
        assert resolve_agent_model("evaluator") == "claude-opus-4-6"
        assert resolve_agent_model("generator") == "claude-opus-4-6"


def test_resolve_agent_model_missing_key():
    """Unknown role falls back to default model."""
    from unittest.mock import patch
    with patch.dict("config.CONFIG", {"model": "claude-opus-4-6"}):
        assert resolve_agent_model("unknown_role") == "claude-opus-4-6"


def test_get_server_ports():
    ports = get_server_ports()
    assert 8005 in ports
    assert 8006 in ports
    assert ports == sorted(set(ports))
