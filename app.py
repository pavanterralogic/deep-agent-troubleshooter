"""Streamlit dashboard: task -> live plan -> agent activity -> findings -> final report."""
import os

import streamlit as st

# On Streamlit Community Cloud the keys live in the app's Secrets settings, not in .env.
# Copy them into environment variables before agent.py reads its configuration.
try:
    for _key, _value in st.secrets.items():
        if isinstance(_value, (str, int, float)):
            os.environ.setdefault(_key, str(_value))
except Exception:  # no secrets file locally: .env is used instead
    pass

from agent import (
    OLLAMA_URL,
    PROVIDER,
    api_key_var,
    backup_label,
    model_name,
    build_agent,
    file_text,
    message_text,
    save_outputs,
    stream_investigation,
)
from scenarios import DEFAULT_SCENARIO, SCENARIOS

st.set_page_config(page_title="Deep Agent - IT Troubleshooter", page_icon="🛠️", layout="wide")

ICON = {"pending": "⏳", "in_progress": "🔄", "completed": "✅"}
REPORT_PATH = "/reports/troubleshooting_report.md"
FINDINGS = [
    ("Log analysis", "/findings/log_analysis.md"),
    ("Server diagnostics", "/findings/server_diagnostics.md"),
    ("Security analysis", "/findings/security_analysis.md"),
    ("Root cause", "/findings/root_cause.md"),
    ("Solutions", "/findings/solutions.md"),
]


@st.cache_resource(show_spinner=False)
def get_agent(scenario: str):
    return build_agent(scenario)


def render_plan(box, todos):
    if not todos:
        box.info("Waiting for the supervisor to create a plan...")
        return
    box.markdown("\n".join(f"{ICON.get(t.get('status'), '⏳')} {t.get('content')}" for t in todos))


def render_activity(box, events):
    box.markdown("\n\n".join(events[-18:]) if events else "_No activity yet_")


def render_results(files):
    report = files.get(REPORT_PATH)
    tabs = st.tabs(["📄 Final report", "🔎 Subagent findings", "🗂 All files"])
    with tabs[0]:
        if report is None:
            st.warning("The agent did not produce a final report. Check the activity log or try again.")
        else:
            text = file_text(report)
            st.markdown(text)
            st.download_button("Download report (.md)", text, file_name="troubleshooting_report.md")
    with tabs[1]:
        for title, path in FINDINGS:
            if path in files:
                with st.expander(title, expanded=False):
                    st.markdown(file_text(files[path]))
            else:
                st.caption(f"{title}: not produced")
    with tabs[2]:
        st.write(sorted(files.keys()))


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Deep Agent prototype")
    st.caption("Supervisor + 5 subagents on simulated logs and metrics. Nothing touches a real server.")
    st.markdown(f"**Model:** `{PROVIDER}` / `{model_name()}`")
    if api_key_var():
        st.markdown(f"**{api_key_var()}:** " + ("✅ set" if os.getenv(api_key_var()) else "❌ missing (.env or Streamlit secrets)"))
    else:
        st.markdown(f"**Ollama server:** `{OLLAMA_URL}` (local, no API key)")
    st.markdown(f"**Backup model:** `{backup_label()}`")
    st.divider()
    st.markdown(
        "**Agents**\n- 🧭 Supervisor (plans, delegates, reviews)\n- 📜 Log Analyzer\n- 🖥 Server Diagnostician\n"
        "- 🛡 Security Analyst (only when needed)\n- 🔗 Root Cause Analyst\n- 🩹 Solution Advisor"
    )

# ------------------------------------------------------------------ main
st.title("🛠️ Deep Agent - IT Infrastructure Troubleshooter")
scenario = st.selectbox(
    "Simulated incident",
    list(SCENARIOS),
    index=list(SCENARIOS).index(DEFAULT_SCENARIO),
    format_func=lambda k: SCENARIOS[k]["title"],
)
# one text box per scenario, so switching scenario shows that scenario's problem description
task = st.text_area("Describe the problem", value=SCENARIOS[scenario]["task"], height=100, key=f"task_{scenario}")
run = st.button("Run investigation", type="primary")

if run:
    st.session_state.pop("files", None)
    left, right = st.columns([1, 1.4])
    with left:
        st.subheader("Plan")
        plan_box = st.empty()
    with right:
        st.subheader("Agent activity")
        activity_box = st.empty()
    render_plan(plan_box, None)
    render_activity(activity_box, [])

    events, seen, state = [], 0, {}
    try:
        agent = get_agent(scenario)
        with st.status("Investigating...", expanded=False) as status:
            for state in stream_investigation(agent, task):
                render_plan(plan_box, state.get("todos"))
                msgs = state.get("messages", [])
                for msg in msgs[seen:]:
                    kind = type(msg).__name__
                    if kind == "AIMessage":
                        for call in getattr(msg, "tool_calls", []) or []:
                            args = call.get("args", {})
                            if call["name"] == "task":
                                events.append(f"🧭 **Delegating** to `{args.get('subagent_type')}`")
                            elif call["name"] == "write_todos":
                                events.append("📝 Updating plan")
                            else:
                                events.append(f"🔧 `{call['name']}` {args.get('file_path', '')}")
                    elif kind == "ToolMessage":
                        events.append(f"&nbsp;&nbsp;↳ {message_text(msg)[:110].strip()!r}")
                seen = len(msgs)
                render_activity(activity_box, events)
            status.update(label="Investigation complete", state="complete")
        st.session_state["files"] = state.get("files") or {}
        save_outputs(state, scenario)
    except Exception as exc:  # show API/rate-limit/config errors in the UI
        st.error(f"Run failed: {exc}")

if st.session_state.get("files"):
    st.divider()
    render_results(st.session_state["files"])
