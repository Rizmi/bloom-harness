import argparse
import asyncio
import json
import os
from pathlib import Path
import re

from . import commands
from . import compact
from . import history
from . import llm
from . import manifest
from . import session
from . import tools
from .mcp_client import mcp_client
from .tools import execute
from .ui import ui


def load_env():
    project_env = Path(__file__).resolve().parent.parent / ".env"
    if project_env.is_file():
        for line in project_env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

    cwd_env = Path(".env")
    if cwd_env.is_file() and cwd_env.resolve() != project_env.resolve():
        for line in cwd_env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                key = k.strip()
                if key.startswith(("BLOOM_", "NEURALCODE_")):
                    continue
                os.environ.setdefault(key, v.strip().strip("'\""))


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
        tools.setup(agent_cfg)
        mcp_client.configure_local_servers(agent_cfg)
        llm.setup(agent_cfg)
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

    while True:
        user_input = await ui.ask()
        if user_input is None:
            break
        user_input = user_input.strip()
        if not user_input:
            continue

        if user_input.startswith("/"):
            messages = await commands.handle(user_input, messages)
            session.save(messages)
            continue

        messages.append({"role": "user", "content": user_input})
        session.save(messages)

        usage = None
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

            msg_dict = message.model_dump(exclude_none=True)
            if "content" not in msg_dict or msg_dict["content"] is None:
                msg_dict["content"] = ""
            messages.append(msg_dict)
            session.save(messages)
            ui.usage(usage)

            if cli_args.debug:
                ui.debug(msg_dict)

            if message.content:
                ui.agent(message.content)

            if not message.tool_calls:
                break

            for tool_call in message.tool_calls:
                args, result = await execute(tool_call)
                ui.tool(tool_call.function.name, args, result)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
                session.save(messages)

        history.sweep()

        if usage and compact.needed(usage):
            messages = await commands.compact(messages)

    ui.summary()


def cli():
    asyncio.run(main())


if __name__ == "__main__":
    cli()
