import pytest
from unittest.mock import patch, MagicMock
from cli import call_agent, AgentResult, AgentError, InfraError
from claude_agent_sdk import ResultMessage


def _mock_result_message(text="Response"):
    """Create a properly configured ResultMessage mock."""
    m = MagicMock(spec=ResultMessage)
    m.result = text
    m.total_cost_usd = 0.0
    m.duration_ms = 0
    m.num_turns = 0
    m.usage = None
    m.conversation_id = ""
    return m


@pytest.mark.anyio
async def test_call_agent_returns_agent_result():
    mock_result = _mock_result_message("Agent response")

    async def mock_query(**kwargs):
        yield mock_result

    with patch("cli.query", side_effect=mock_query):
        result = await call_agent(
            system_prompt="You are a planner.",
            user_prompt="Build a todo app",
            allowed_tools=["Read", "Write"],
            cwd="/tmp/test",
        )

    assert isinstance(result, AgentResult)
    assert result.result == "Agent response"


@pytest.mark.anyio
async def test_call_agent_with_mcp_tools():
    mock_result = _mock_result_message("Response")

    captured_options = {}

    async def mock_query(**kwargs):
        captured_options.update(kwargs)
        yield mock_result

    with patch("cli.query", side_effect=mock_query):
        await call_agent(
            system_prompt="You are an evaluator.",
            user_prompt="Evaluate the app",
            allowed_tools=["Read", "Write"],
            mcp_tools=["playwright"],
            cwd="/tmp/test",
        )

    tools = captured_options["options"].allowed_tools
    assert any("playwright" in t for t in tools)


@pytest.mark.anyio
async def test_call_agent_raises_on_empty_result():
    async def mock_query(**kwargs):
        # No ResultMessage yielded — empty result
        return
        yield  # make it an async generator

    with patch("cli.query", side_effect=mock_query):
        with pytest.raises(AgentError, match="empty result"):
            await call_agent(
                system_prompt="test",
                user_prompt="test",
                allowed_tools=["Read"],
                cwd="/tmp/test",
            )


@pytest.mark.anyio
async def test_call_agent_raises_on_sdk_exception():
    async def mock_query(**kwargs):
        raise RuntimeError("SDK crash")
        yield  # make it an async generator

    with patch("cli.query", side_effect=mock_query):
        with pytest.raises(AgentError, match="Agent execution failed"):
            await call_agent(
                system_prompt="test",
                user_prompt="test",
                allowed_tools=["Read"],
                cwd="/tmp/test",
            )


@pytest.mark.anyio
async def test_call_agent_raises_infra_error_on_buffer_overflow():
    async def mock_query(**kwargs):
        raise RuntimeError(
            "Fatal error in message reader: Failed to decode JSON: "
            "JSON message exceeded maximum buffer size of 1048576 bytes"
        )
        yield

    with patch("cli.query", side_effect=mock_query):
        with pytest.raises(InfraError, match="Agent execution failed"):
            await call_agent(
                system_prompt="test",
                user_prompt="test",
                allowed_tools=["Read"],
                cwd="/tmp/test",
            )


@pytest.mark.anyio
async def test_infra_error_is_subclass_of_agent_error():
    """InfraError should be catchable as AgentError for backward compat."""
    assert issubclass(InfraError, AgentError)


@pytest.mark.anyio
async def test_call_agent_with_disallowed_tools():
    mock_result = _mock_result_message("Response")

    captured_options = {}

    async def mock_query(**kwargs):
        captured_options.update(kwargs)
        yield mock_result

    with patch("cli.query", side_effect=mock_query):
        await call_agent(
            system_prompt="You are an evaluator.",
            user_prompt="Evaluate the app",
            allowed_tools=["Read", "Write"],
            mcp_tools=["playwright"],
            disallowed_tools=["mcp__playwright__browser_take_screenshot"],
            cwd="/tmp/test",
        )

    opts = captured_options["options"]
    assert "mcp__playwright__browser_take_screenshot" in opts.disallowed_tools
