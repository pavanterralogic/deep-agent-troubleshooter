"""Run the investigation from the terminal (do this BEFORE using the Streamlit UI).

    python run_cli.py                                   # default scenario (runaway_job)
    python run_cli.py --scenario bad_deployment
    python run_cli.py --scenario security_incident
    python run_cli.py --scenario bad_deployment "Why does the API return HTTP 500 errors?"
    python run_cli.py --list                            # show available scenarios
"""
import argparse

from agent import build_agent, message_text, save_outputs, stream_investigation
from scenarios import DEFAULT_SCENARIO, SCENARIOS

ICON = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def main():
    parser = argparse.ArgumentParser(description="Run the Deep Agent troubleshooter on a simulated incident.")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO, choices=list(SCENARIOS))
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("task", nargs="*", help="problem description (default: the scenario's own)")
    args = parser.parse_args()
    if args.list:
        for key, sc in SCENARIOS.items():
            print(f"{key:<18} {sc['title']}")
        return

    task = " ".join(args.task) or SCENARIOS[args.scenario]["task"]
    print(f"SCENARIO: {args.scenario} - {SCENARIOS[args.scenario]['title']}")
    print(f"TASK: {task}\n")
    agent = build_agent(args.scenario)
    seen, last_todos, state = 0, None, {}

    for state in stream_investigation(agent, task):
        todos = state.get("todos")
        if todos and todos != last_todos:
            print("\n--- PLAN ---")
            for t in todos:
                print(f"{ICON.get(t.get('status'), '[ ]')} {t.get('content')}")
            print("------------")
            last_todos = todos

        msgs = state.get("messages", [])
        for msg in msgs[seen:]:
            kind = type(msg).__name__
            if kind == "AIMessage":
                for call in getattr(msg, "tool_calls", []) or []:
                    call_args = call.get("args", {})
                    detail = call_args.get("subagent_type") or call_args.get("file_path") or ""
                    print(f"> tool call: {call['name']} {detail}")
            elif kind == "ToolMessage":
                print(f"  result: {message_text(msg)[:120].strip()!r}")
        seen = len(msgs)

    final = state.get("messages", [])[-1] if state.get("messages") else None
    if final is not None:
        print("\n=== SUPERVISOR SUMMARY ===\n" + message_text(final))

    saved = save_outputs(state, args.scenario)
    print("\nSaved files:")
    for p in saved:
        print(" -", p)


if __name__ == "__main__":
    main()
