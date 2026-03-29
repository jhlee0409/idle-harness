from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    ResultMessage,
    AssistantMessage,
)


async def call_agent(
    system_prompt: str,
    user_prompt: str,
    allowed_tools: list[str],
    cwd: str,
    mcp_tools: list[str] | None = None,
    max_turns: int | None = None,
) -> str:
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
    result = ""
    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, AssistantMessage):
            turn_count += 1
            if turn_count % 5 == 0:
                print(f"    ...진행 중 ({turn_count} turns)")
        if isinstance(message, ResultMessage):
            result = message.result
    return result
