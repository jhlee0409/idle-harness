import asyncio
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
    cost_usd: float = 0.0
    duration_ms: int = 0


class AgentError(Exception):
    """Raised when a sub-agent fails or returns no result."""


class InfraError(AgentError):
    """Raised for infrastructure errors that won't be fixed by rebuilding."""


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
    mcp_servers: dict | None = None,
    max_turns: int | None = None,
    resume: str | None = None,
    disallowed_tools: list[str] | None = None,
) -> AgentResult:
    """Call a Claude agent. Pass resume=session_id to continue a previous session.

    mcp_servers: SDK-managed MCP servers (launched automatically, no user setup needed).
                 Example: {"playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]}}
    mcp_tools: Legacy — references MCP servers from user's Claude CLI config.
    """
    tools = list(allowed_tools)
    if mcp_tools:
        for mcp in mcp_tools:
            tools.append(f"mcp__{mcp}__*")

    stderr_lines = []

    def _capture_stderr(line: str):
        stderr_lines.append(line)
        if len(stderr_lines) > 50:
            stderr_lines.pop(0)

    options = ClaudeAgentOptions(
        cwd=cwd,
        allowed_tools=tools,
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
        model="claude-opus-4-6",
        max_buffer_size=10 * 1024 * 1024,  # 10MB — allows screenshot responses
        stderr=_capture_stderr,
    )

    if mcp_servers:
        options.mcp_servers = mcp_servers

    if disallowed_tools:
        options.disallowed_tools = disallowed_tools

    if max_turns:
        options.max_turns = max_turns
    if resume:
        options.resume = resume

    turn_count = 0
    result_text = ""
    input_tokens = 0
    output_tokens = 0
    conversation_id = ""
    cost_usd = 0.0
    duration_ms = 0
    start = time.time()

    def _print_status():
        elapsed = _fmt_elapsed(int(time.time() - start))
        total_tok = _fmt_tokens(input_tokens + output_tokens)
        status = f"    ↳ {elapsed} | {turn_count} turns | {total_tok} tokens"
        if cost_usd > 0:
            status += f" | ${cost_usd:.2f}"
        sys.stdout.write(f"\r{status}    ")
        sys.stdout.flush()

    async def _ticker():
        while True:
            await asyncio.sleep(1)
            _print_status()

    ticker_task = asyncio.create_task(_ticker())
    try:
        async for message in query(prompt=user_prompt, options=options):
            if isinstance(message, AssistantMessage):
                turn_count += 1
                usage = getattr(message, "usage", None)
                if usage:
                    input_tokens += getattr(usage, "input_tokens", 0)
                    output_tokens += getattr(usage, "output_tokens", 0)
                _print_status()
            if isinstance(message, ResultMessage):
                result_text = message.result
                conversation_id = getattr(message, "conversation_id", "") or ""
                # Extract final usage/cost from ResultMessage
                cost_usd = getattr(message, "total_cost_usd", 0.0) or 0.0
                duration_ms = getattr(message, "duration_ms", 0) or 0
                num_turns = getattr(message, "num_turns", 0) or 0
                if num_turns:
                    turn_count = num_turns
                ru = getattr(message, "usage", None)
                if ru:
                    ri = getattr(ru, "input_tokens", 0) or 0
                    ro = getattr(ru, "output_tokens", 0) or 0
                    if ri > input_tokens:
                        input_tokens = ri
                    if ro > output_tokens:
                        output_tokens = ro
    except Exception as exc:
        ticker_task.cancel()
        sys.stdout.write("\r" + " " * 80 + "\r")  # clear progress line
        msg = str(exc)
        # Append stderr for diagnosis
        stderr_tail = "\n".join(stderr_lines[-10:]) if stderr_lines else ""
        if stderr_tail:
            full_msg = f"Agent execution failed: {exc}\nstderr:\n{stderr_tail}"
        else:
            full_msg = f"Agent execution failed: {exc}"
        # Classify infrastructure errors that won't be fixed by rebuilding
        combined = msg + stderr_tail
        infra_patterns = [
            "maximum buffer size",
            "buffer size of 1048576",
            "Failed to decode JSON",
            "message reader",
        ]
        if any(p in combined for p in infra_patterns):
            raise InfraError(full_msg) from exc
        raise AgentError(full_msg) from exc

    ticker_task.cancel()
    # Clear progress line
    sys.stdout.write("\r" + " " * 80 + "\r")
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
        cost_usd=cost_usd,
        duration_ms=duration_ms,
    )
