import sys
import time
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
    conversation_id: str = ""


class AgentError(Exception):
    """Raised when a sub-agent fails or returns no result."""


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_elapsed(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    return f"{m}m{s:02d}s"


async def call_agent(
    system_prompt: str,
    user_prompt: str,
    allowed_tools: list[str],
    cwd: str,
    mcp_tools: list[str] | None = None,
    max_turns: int | None = None,
    resume: str | None = None,
) -> AgentResult:
    """Call a Claude agent. Pass resume=conversation_id to continue a previous session."""
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
    if resume:
        options.resume = resume

    turn_count = 0
    result_text = ""
    input_tokens = 0
    output_tokens = 0
    conversation_id = ""
    start = time.time()

    try:
        async for message in query(prompt=user_prompt, options=options):
            if isinstance(message, AssistantMessage):
                turn_count += 1
                usage = getattr(message, "usage", None)
                if usage:
                    input_tokens += getattr(usage, "input_tokens", 0)
                    output_tokens += getattr(usage, "output_tokens", 0)
                # Single-line overwrite progress
                elapsed = _fmt_elapsed(int(time.time() - start))
                total_tok = _fmt_tokens(input_tokens + output_tokens)
                status = f"    ↳ {elapsed} | {turn_count} turns | {total_tok} tokens"
                sys.stdout.write(f"\r{status}    ")
                sys.stdout.flush()
            if isinstance(message, ResultMessage):
                result_text = message.result
                conversation_id = getattr(message, "conversation_id", "") or ""
    except Exception as exc:
        sys.stdout.write("\r" + " " * 60 + "\r")  # clear progress line
        raise AgentError(f"Agent execution failed: {exc}") from exc

    # Clear progress line
    sys.stdout.write("\r" + " " * 60 + "\r")
    sys.stdout.flush()

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
        conversation_id=conversation_id,
    )
