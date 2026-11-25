"""Bloom Unified Turn Execution Engine.

Shared by both the interactive terminal loop (bloom/agent.py) and
the headless HTTP/SSE API server (bloom/server.py).
Executes LLM completions and tool execution rounds.
"""

import asyncio
import inspect
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import commands
from . import compact
from . import history
from . import llm
from . import session
from .tools import execute


def _normalize_tool_calls(raw_calls: Any) -> List[Dict[str, Any]]:
    """Normalize SDK objects or partial dicts into canonical tool_call dicts."""
    normalized: List[Dict[str, Any]] = []
    for tc in raw_calls or []:
        if hasattr(tc, "model_dump"):
            dumped = tc.model_dump(exclude_none=True)
            fn = dumped.get("function") if isinstance(dumped, dict) else {}
            fn = fn if isinstance(fn, dict) else {}
            normalized.append({
                "id": dumped.get("id") or "",
                "type": dumped.get("type") or "function",
                "function": {
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") if fn.get("arguments") is not None else "{}",
                },
            })
        elif isinstance(tc, dict):
            fn = tc.get("function")
            fn = fn if isinstance(fn, dict) else {}
            normalized.append({
                "id": tc.get("id") or "",
                "type": tc.get("type") or "function",
                "function": {
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") if fn.get("arguments") is not None else "{}",
                },
            })
        else:
            fn = getattr(tc, "function", None)
            normalized.append({
                "id": getattr(tc, "id", None) or "",
                "type": getattr(tc, "type", None) or "function",
                "function": {
                    "name": getattr(fn, "name", None) or "",
                    "arguments": getattr(fn, "arguments", None) if getattr(fn, "arguments", None) is not None else "{}",
                },
            })
    return normalized


def _accepts_on_delta(fn: Any) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return "on_delta" in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


async def _emit_usage(emit: Callable, usage: Dict[str, Any], context_limit: int):
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
    session_id: Optional[str] = None,
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
        session.save(messages, session_id=session_id)

    context_limit = getattr(llm, "CONTEXT_WINDOW", 64000) or 64000
    rounds = 0
    usage = None

    while True:
        if compact.needed_before(messages):
            before_len = len(messages)
            new_messages = await commands.compact(messages, session_id=session_id)
            if new_messages is not messages:
                messages = new_messages
                await emit({"type": "compacted", "before": before_len, "after": len(messages)})
            elif compact.needed_before(new_messages):
                await emit({"type": "error", "error": "Warning: Input exceeds context window budget and cannot be compacted further."})
                await emit({"type": "done", "reason": "error"})
                return messages, usage

        await emit({"type": "thinking"})
        streamed_fields: set[str] = set()

        async def _on_delta(field: str, chunk_text: str):
            if chunk_text:
                streamed_fields.add(field)
                await emit({"type": "delta", "field": field, "text": chunk_text})

        try:
            target_fn = call_llm_fn if call_llm_fn is not None else llm.call_llm
            if _accepts_on_delta(target_fn):
                message, usage = await target_fn(messages, on_delta=_on_delta)
            else:
                message, usage = await target_fn(messages)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:
            await emit({"type": "error", "error": f"API Error: {e}"})
            await emit({"type": "done", "reason": "error"})
            return messages, usage

        reasoning_text = getattr(message, "reasoning_content", None)
        if not isinstance(reasoning_text, str) and hasattr(message, "model_dump"):
            try:
                dumped = message.model_dump()
                if isinstance(dumped, dict):
                    reasoning_text = dumped.get("reasoning_content")
            except Exception:
                pass

        content_text = (message.get("content") if isinstance(message, dict) else getattr(message, "content", None)) or ""
        raw_tcs = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
        tool_calls_list = _normalize_tool_calls(raw_tcs)

        msg_dict = {"role": "assistant", "content": content_text}
        if isinstance(reasoning_text, str) and reasoning_text:
            msg_dict["reasoning_content"] = reasoning_text
        if tool_calls_list:
            msg_dict["tool_calls"] = tool_calls_list

        messages.append(msg_dict)
        session.save(messages, session_id=session_id)

        if isinstance(reasoning_text, str) and reasoning_text and "reasoning" not in streamed_fields:
            await emit({"type": "delta", "field": "reasoning", "text": reasoning_text})

        if content_text and "text" not in streamed_fields:
            await emit({"type": "delta", "field": "text", "text": content_text})

        if usage:
            await _emit_usage(emit, usage, context_limit)

        if not tool_calls_list:
            break

        rounds += 1
        if rounds > max_tool_rounds:
            for idx, tool_call in enumerate(tool_calls_list):
                tc_id = tool_call["id"] or f"synthesized_call_{idx}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": "Error: tool round limit reached; call not executed.",
                })
            session.save(messages, session_id=session_id)
            await emit({"type": "error", "error": "Tool execution stopped: exceeded maximum tool rounds limit."})
            await emit({"type": "done", "reason": "max_rounds"})
            return messages, usage

        for tool_call in tool_calls_list:
            fn_name = tool_call["function"]["name"]
            tc_id = tool_call["id"]
            raw_args = tool_call["function"]["arguments"]
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
            session.save(messages, session_id=session_id)

    if usage and compact.needed(usage):
        before_len = len(messages)
        new_messages = await commands.compact(messages, session_id=session_id)
        if new_messages is not messages:
            messages = new_messages
            await emit({"type": "compacted", "before": before_len, "after": len(messages)})
            est_prompt = history.estimate(messages)
            usage = {
                "prompt_tokens": est_prompt,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
            }
            await _emit_usage(emit, usage, context_limit)

    await emit({"type": "done", "reason": "stop"})
    return messages, usage
