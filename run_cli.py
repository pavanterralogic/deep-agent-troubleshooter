"""Run the investigation from the terminal (do this BEFORE using the Streamlit UI).

    python run_cli.py
    python run_cli.py "Investigate why my application returns HTTP 500 errors."
"""
import sys

from agent import DEFAULT_TASK, build_agent, message_text, save_outputs, stream_investigation

ICON = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def main():
    task = " ".join(sys.argv[1:]) or DEFAULT_TASK
    print(f"TASK: {task}\n")
    agent = build_agent()
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
                    args = call.get("args", {})
                    detail = args.get("subagent_type") or args.get("file_path") or ""
                    print(f"> tool call: {call['name']} {detail}")
            elif kind == "ToolMessage":
                print(f"  result: {message_text(msg)[:120].strip()!r}")
        seen = len(msgs)

    final = state.get("messages", [])[-1] if state.get("messages") else None
    if final is not None:
        print("\n=== SUPERVISOR SUMMARY ===\n" + message_text(final))

    saved = save_outputs(state)
    print("\nSaved files:")
    for p in saved:
        print(" -", p)


if __name__ == "__main__":
    main()
