from dataclasses import dataclass

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    ResultMessage,
    AssistantMessage,
)


@dataclass
class AgentResult:
    result: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0


class AgentError(Exception):
    """Raised when a sub-agent fails or returns no result."""


async def call_agent(
    system_prompt: str,
    user_prompt: str,
    allowed_tools: list[str],
    cwd: str,
    mcp_tools: list[str] | None = None,
    max_turns: int | None = None,
) -> AgentResult:
    tools = list(allowed_tools)
    if mcp_tools:
        for mcp in mcp_tools:
            tools.append(f"mcp__{mcp}__*")

    options = ClaudeAgentOptions(
        cwd=cwd,
        allowed_tools=tools,
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
        model="claude-opus-4-6",
    )

    if max_turns:
        options.max_turns = max_turns

    turn_count = 0
    result_text = ""
    input_tokens = 0
    output_tokens = 0

    try:
        async for message in query(prompt=user_prompt, options=options):
            if isinstance(message, AssistantMessage):
                turn_count += 1
                if turn_count % 5 == 0:
                    print(f"    ...진행 중 ({turn_count} turns)")
                usage = getattr(message, "usage", None)
                if usage:
                    input_tokens += getattr(usage, "input_tokens", 0)
                    output_tokens += getattr(usage, "output_tokens", 0)
            if isinstance(message, ResultMessage):
                result_text = message.result
    except Exception as exc:
        raise AgentError(f"Agent execution failed: {exc}") from exc

    if not result_text:
        raise AgentError(
            f"Agent returned empty result after {turn_count} turns. "
            "The agent may have crashed or hit a turn limit."
        )

    return AgentResult(
        result=result_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        turns=turn_count,
    )
