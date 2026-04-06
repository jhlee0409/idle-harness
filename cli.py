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

from config import CONFIG


@dataclass
class AgentResult:
    result: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    session_id: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0


class AgentError(Exception):
    """Raised when a sub-agent fails or returns no result."""


class InfraError(AgentError):
    """Raised for infrastructure errors that won't be fixed by rebuilding."""


def fmt_tokens(n: int) -> str:
    """Format token count for display (e.g., 1.2M, 45.3k)."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def fmt_elapsed(secs: int) -> str:
    """Format seconds for display (e.g., 45s, 5m 23s, 1h 30m 5s)."""
    if secs < 60:
        return f"{secs}s"
    mins, s = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m {s}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m {s}s"


class AgentTimeout(AgentError):
    """Raised when a sub-agent exceeds its wall-clock timeout."""


async def call_agent(
    system_prompt: str,
    user_prompt: str,
    allowed_tools: list[str],
    cwd: str,
    mcp_servers: dict | None = None,
    max_turns: int | None = None,
    resume: str | None = None,
    disallowed_tools: list[str] | None = None,
    timeout: int = 0,
) -> AgentResult:
    """Call a Claude agent. Pass resume=session_id to continue a previous session.

    mcp_servers: SDK-managed MCP servers (launched automatically, no user setup needed).
                 Example: {"playwright": {"command": "npx", "args": ["@anthropic-ai/mcp-server-playwright"]}}
    timeout: Wall-clock timeout in seconds. 0 = no limit. Raises AgentTimeout on expiry.
    """
    tools = list(allowed_tools)

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
        model=CONFIG.get("model", "claude-opus-4-6"),
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
    session_id = ""
    cost_usd = 0.0
    duration_ms = 0
    start = time.time()

    def _print_status():
        elapsed = fmt_elapsed(int(time.time() - start))
        total_tok = fmt_tokens(input_tokens + output_tokens)
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
        async with asyncio.timeout(timeout if timeout > 0 else None):
            async for message in query(prompt=user_prompt, options=options):
                # Capture session_id from init message (per SDK docs)
                if hasattr(message, "subtype") and getattr(message, "subtype", "") == "init":
                    sid = getattr(message, "session_id", "")
                    if sid:
                        session_id = sid
                if isinstance(message, AssistantMessage):
                    turn_count += 1
                    usage = getattr(message, "usage", None)
                    if usage:
                        input_tokens += getattr(usage, "input_tokens", 0)
                        output_tokens += getattr(usage, "output_tokens", 0)
                    _print_status()
                if isinstance(message, ResultMessage):
                    result_text = message.result
                    # Fallback: some SDK versions put session info on ResultMessage
                    if not session_id:
                        session_id = getattr(message, "session_id", "") or getattr(message, "conversation_id", "") or ""
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
    except TimeoutError:
        ticker_task.cancel()
        sys.stdout.write("\r" + " " * 80 + "\r")
        elapsed = int(time.time() - start)
        raise AgentTimeout(
            f"Agent timed out after {fmt_elapsed(elapsed)} ({turn_count} turns, "
            f"{fmt_tokens(input_tokens + output_tokens)} tokens). "
            f"The agent may be stuck on a hanging command."
        )
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
        session_id=session_id,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
    )
