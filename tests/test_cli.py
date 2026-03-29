import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from cli import call_agent
from claude_agent_sdk import ResultMessage


@pytest.mark.anyio
async def test_call_agent_builds_options():
    mock_result = MagicMock(spec=ResultMessage)
    mock_result.result = "Agent response"

    async def mock_query(**kwargs):
        yield mock_result

    with patch("cli.query", side_effect=mock_query):
        result = await call_agent(
            system_prompt="You are a planner.",
            user_prompt="Build a todo app",
            allowed_tools=["Read", "Write"],
            cwd="/tmp/test",
        )

    assert result == "Agent response"


@pytest.mark.anyio
async def test_call_agent_with_mcp_tools():
    mock_result = MagicMock()
    mock_result.result = "Response"

    captured_options = {}

    async def mock_query(**kwargs):
        captured_options.update(kwargs)
        yield mock_result

    with patch("cli.query", side_effect=mock_query):
        await call_agent(
            system_prompt="You are an evaluator.",
            user_prompt="Evaluate the app",
            allowed_tools=["Read", "Write"],
            mcp_tools=["chrome-devtools"],
            cwd="/tmp/test",
        )

    tools = captured_options["options"].allowed_tools
    assert any("chrome-devtools" in t for t in tools)
