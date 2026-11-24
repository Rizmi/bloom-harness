import argparse
import asyncio
import os
from pathlib import Path
import re


def load_env():
    project_env = Path(__file__).resolve().parent.parent / ".env"
    if project_env.is_file():
        try:
            for line in project_env.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))
        except OSError:
            pass

    cwd_env = Path(".env")
    if cwd_env.is_file() and cwd_env.resolve() != project_env.resolve():
        try:
            for line in cwd_env.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    key = k.strip()
                    if key.startswith(("BLOOM_", "NEURALCODE_")):
                        continue
                    os.environ.setdefault(key, v.strip().strip("'\""))
        except OSError:
            pass


load_env()

from . import commands
from . import compact
from . import llm
from . import manifest
from . import session
from . import tools
from .mcp_client import mcp_client
from .tools import execute
from .ui import ui


def refresh_system_prompt(messages: list, prompt: str) -> list:
    """Preserve existing compacted <summary> handoff when refreshing system prompt."""
    if not messages:
        return [{"role": "system", "content": prompt}]
    stored = messages[0].get("content") or ""
    m = re.search(r"<summary>.*?</summary>", stored, re.DOTALL)
    messages[0] = {
        "role": "system",
        "content": prompt + ("\n\n" + m.group(0) if m else ""),
    }
    return messages


async def main():
    load_env()
    parser = argparse.ArgumentParser(prog="bloom")
    parser.add_argument(
        "--agent",
        help="agent manifest name (e.g. assistant) or path to JSON file",
    )
    parser.add_argument("--resume", action="store_true", help="continue the last session")
    parser.add_argument("--debug", action="store_true", help="show the raw model response")
    cli_args = parser.parse_args()

    if not cli_args.agent:
        parser.error("--agent is required to run the harness (e.g. --agent assistant or --agent path/to/agent.json)")

    try:
        agent_name, agent_cfg = manifest.resolve(cli_args.agent)
        session.set_agent(agent_name)
        mcp_client.reload_servers()
        from . import skills
        auth_skill_paths, auth_skills = skills.validate_agent_skills(agent_cfg, agent_name)
        tools.setup(agent_cfg)
        mcp_client.configure_local_servers(
            agent_cfg, skill_paths=auth_skill_paths, allowed_skills=auth_skills
        )
        llm.setup(agent_cfg, skill_paths=auth_skill_paths, allowed_skills=auth_skills)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        parser.error(str(e))

    ui.banner()

    messages = [{"role": "system", "content": llm.SYSTEM_PROMPT}]
    if cli_args.resume:
        try:
            saved = session.all_sessions()
            if saved:
                messages = session.open_session(saved[0]["id"])
                if messages:
                    refresh_system_prompt(messages, llm.SYSTEM_PROMPT)
                ui.resumed(messages)
                ui.replay(messages)
        except Exception as e:
            ui.note(f"Failed to resume session ({e}); starting fresh.")
            messages = [{"role": "system", "content": llm.SYSTEM_PROMPT}]

    try:
        max_tool_rounds = max(1, int(os.environ.get("BLOOM_MAX_TOOL_ROUNDS", "50")))
    except ValueError:
        max_tool_rounds = 50

    while True:
        user_input = await ui.ask()
        if user_input is None:
            break
        user_input = user_input.strip()
        if not user_input:
            continue

        first_token = user_input.split()[0].lower()
        if first_token in commands.COMMANDS:
            messages = await commands.handle(first_token, messages)
            session.save(messages)
            continue

        messages.append({"role": "user", "content": user_input})
        session.save(messages)

        usage = None
        rounds = 0
        while True:
            if compact.needed_before(messages):
                new_messages = await commands.compact(messages)
                if new_messages is messages and compact.needed_before(new_messages):
                    ui.agent("Warning: Input exceeds context window budget and cannot be compacted further.")
                    break
                messages = new_messages

            try:
                with ui.working():
                    message, usage = await llm.call_llm(messages)
            except KeyboardInterrupt:
                ui.agent("(Cancelled)")
                break
            except Exception as e:
                ui.agent(f"API Error: {e}")
                break

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
            ui.usage(usage)

            if cli_args.debug:
                ui.debug(msg_dict)

            if message.content:
                ui.agent(message.content)

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
                ui.agent("Tool execution stopped: exceeded maximum tool rounds limit.")
                break

            for tool_call in message.tool_calls:
                args, result = await execute(tool_call)
                fn_name = getattr(getattr(tool_call, "function", None), "name", None) or (
                    tool_call.get("function", {}).get("name") if isinstance(tool_call, dict) else ""
                )
                tc_id = getattr(tool_call, "id", None) or (
                    tool_call.get("id") if isinstance(tool_call, dict) else ""
                )
                ui.tool(fn_name, args, result)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result,
                })
                session.save(messages)

        if usage and compact.needed(usage):
            messages = await commands.compact(messages)

    ui.summary()


def cli():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
