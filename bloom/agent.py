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
from . import llm
from . import manifest
from . import session
from . import tools
from .mcp_client import mcp_client
from .runner import run_turn
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
    parser.add_argument("--serve", action="store_true", help="run headless HTTP & SSE API server")
    parser.add_argument("--host", default="127.0.0.1", help="host to bind API server (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=4096, help="port to bind API server (default: 4096)")
    cli_args = parser.parse_args()

    if cli_args.serve:
        from . import server
        initial_agent = cli_args.agent or "assistant"
        await server.run_server(host=cli_args.host, port=cli_args.port, initial_agent=initial_agent)
        return

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

        async def cli_call_llm(msgs):
            with ui.working():
                return await llm.call_llm(msgs)

        async def cli_on_event(ev: dict):
            t = ev.get("type")
            if t == "delta":
                ui.agent(ev["text"])
            elif t == "message" and cli_args.debug:
                ui.debug(ev["message"])
            elif t == "usage":
                ui.usage(ev.get("raw_usage"))
            elif t == "tool_result":
                ui.tool(ev.get("raw_tool") or ev["tool"], ev.get("raw_args") or ev["arguments"], ev["output"])
            elif t == "error":
                ui.agent(ev["error"])

        try:
            messages, usage = await run_turn(
                messages,
                user_input,
                max_tool_rounds=max_tool_rounds,
                on_event=cli_on_event,
                call_llm_fn=cli_call_llm,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            ui.agent("(Cancelled)")

    ui.summary()


def cli():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
