"""Bloom Unified Turn Execution Engine.

Shared by both the interactive terminal loop (bloom/agent.py) and
the headless HTTP/SSE API server (bloom/server.py).
Executes LLM completions and tool execution rounds.
"""

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import commands
from . import compact
from . import llm
from . import session
from .tools import execute


def unwrap_tool_info(fn_name: str, raw_args: Any) -> Tuple[str, Any]:
    """Unwrap tool_run to present the clean qualified tool name and inner arguments."""
    parsed = raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except Exception:
            parsed = raw_args
    if fn_name == "tool_run" and isinstance(parsed, dict) and "tool_name" in parsed:
        return parsed["tool_name"], parsed.get("arguments", {})
    return fn_name, parsed


async def run_turn(
    messages: List[Dict[str, Any]],
    user_input: str,
    *,
    max_tool_rounds: int = 50,
    on_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
    call_llm_fn: Optional[Callable] = None,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Execute a single conversation turn through LLM completion and tool rounds.

    Emits on_event(event_dict) as each phase runs:
    - compacted (before, after)
    - thinking
    - delta (field="text"|"reasoning", text=...)
    - usage (promptTokens, cachedTokens, contextPercentage, etc.)
    - tool (state="running"|"completed", callId, tool, arguments, input, output)
    - error (error=...)
    - done (reason="stop"|"max_rounds"|"error")

    Returns:
        (updated_messages, final_usage)
    """
    async def emit(ev: Dict[str, Any]):
        if on_event is not None:
            if asyncio.iscoroutinefunction(on_event):
                await on_event(ev)
            else:
                on_event(ev)

    trimmed = user_input.strip()
    if trimmed:
        messages.append({"role": "user", "content": trimmed})
        session.save(messages)

    context_limit = getattr(llm, "CONTEXT_WINDOW", 64000) or 64000
    rounds = 0
    usage = None

    while True:
        if compact.needed_before(messages):
            before_len = len(messages)
            new_messages = await commands.compact(messages)
            if new_messages is not messages:
                messages = new_messages
                await emit({"type": "compacted", "before": before_len, "after": len(messages)})
            elif compact.needed_before(new_messages):
                await emit({"type": "error", "error": "Warning: Input exceeds context window budget and cannot be compacted further."})
                await emit({"type": "done", "reason": "error"})
                return messages, usage

        await emit({"type": "thinking"})
        try:
            if call_llm_fn is not None:
                message, usage = await call_llm_fn(messages)
            else:
                message, usage = await llm.call_llm(messages)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:
            await emit({"type": "error", "error": f"API Error: {e}"})
            await emit({"type": "done", "reason": "error"})
            return messages, usage

        msg_dict = {"role": "assistant", "content": message.content or ""}
        if message.tool_calls:
            tool_calls_list = []
            for tc in message.tool_calls:
                if hasattr(tc, "model_dump"):
                    tool_calls_list.append(tc.model_dump(exclude_none=True))
                elif isinstance(tc, dict):
                    tool_calls_list.append(tc)
                else:
                    tool_calls_list.append({
                        "id": getattr(tc, "id", ""),
                        "type": getattr(tc, "type", "function"),
                        "function": {
                            "name": getattr(getattr(tc, "function", None), "name", ""),
                            "arguments": getattr(getattr(tc, "function", None), "arguments", "{}"),
                        },
                    })
            msg_dict["tool_calls"] = tool_calls_list

        messages.append(msg_dict)
        session.save(messages)

        reasoning_text = getattr(message, "reasoning_content", None)
        if not isinstance(reasoning_text, str) and hasattr(message, "model_dump"):
            try:
                dumped = message.model_dump()
                if isinstance(dumped, dict):
                    reasoning_text = dumped.get("reasoning_content")
            except Exception:
                pass
        if isinstance(reasoning_text, str) and reasoning_text:
            await emit({"type": "delta", "field": "reasoning", "text": reasoning_text})

        if message.content:
            await emit({"type": "delta", "field": "text", "text": message.content})

        if usage:
            prompt_tokens = usage.get("prompt_tokens", 0) or 0
            completion_tokens = usage.get("completion_tokens", 0) or 0
            reasoning_tokens = usage.get("reasoning_tokens") or 0
            total_tokens = prompt_tokens + completion_tokens
            pct = (prompt_tokens / context_limit) * 100
            pct_str = f"{pct:.2f}%" if pct < 0.1 else f"{pct:.1f}%"
            await emit({
                "type": "usage",
                "promptTokens": prompt_tokens,
                "completionTokens": completion_tokens,
                "candidatesTokens": completion_tokens,
                "reasoningTokens": reasoning_tokens,
                "thoughtsTokens": reasoning_tokens,
                "totalTokens": total_tokens,
                "contextTokens": prompt_tokens,
                "cachedTokens": usage.get("cached_tokens", 0) or 0,
                "contextLimit": context_limit,
                "contextPercentage": pct_str,
                "raw_usage": usage,
            })

        if not message.tool_calls:
            break

        rounds += 1
        if rounds > max_tool_rounds:
            for idx, tool_call in enumerate(message.tool_calls):
                tc_id = getattr(tool_call, "id", None) or (tool_call.get("id") if isinstance(tool_call, dict) else "")
                if not tc_id:
                    tc_id = f"synthesized_call_{idx}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": "Error: tool round limit reached; call not executed.",
                })
            session.save(messages)
            await emit({"type": "error", "error": "Tool execution stopped: exceeded maximum tool rounds limit."})
            await emit({"type": "done", "reason": "max_rounds"})
            return messages, usage

        for tool_call in message.tool_calls:
            fn_name = getattr(getattr(tool_call, "function", None), "name", None) or (
                tool_call.get("function", {}).get("name") if isinstance(tool_call, dict) else ""
            )
            tc_id = getattr(tool_call, "id", None) or (
                tool_call.get("id") if isinstance(tool_call, dict) else ""
            )
            raw_args = getattr(getattr(tool_call, "function", None), "arguments", None) or (
                tool_call.get("function", {}).get("arguments") if isinstance(tool_call, dict) else "{}"
            )
            display_tool, display_args = unwrap_tool_info(fn_name, raw_args)

            await emit({
                "type": "tool",
                "callId": tc_id,
                "id": tc_id,
                "tool": display_tool,
                "name": display_tool,
                "title": display_tool,
                "arguments": display_args,
                "input": display_args,
                "status": "running",
                "state": "running",
            })

            args, result = await execute(tool_call)

            await emit({
                "type": "tool",
                "callId": tc_id,
                "id": tc_id,
                "tool": display_tool,
                "name": display_tool,
                "title": display_tool,
                "arguments": display_args,
                "input": display_args,
                "output": result,
                "status": "completed",
                "state": "completed",
            })

            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result,
            })
            session.save(messages)

    if usage and compact.needed(usage):
        messages = await commands.compact(messages)

    await emit({"type": "done", "reason": "stop"})
    return messages, usage
