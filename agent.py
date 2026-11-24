"""
Builds the supervisor Deep Agent with four specialist subagents.

Deep Agents concepts demonstrated here:
  * Planning        - built-in `write_todos` tool (supervisor keeps a todo list)
  * Filesystem      - built-in virtual files (/findings/*.md, /reports/*.md) shared by all agents
  * Subagents       - `subagents=[...]`, each with its own prompt, tools and isolated context
  * Tools           - custom tools from tools.py
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from deepagents import create_deep_agent
from langchain.agents.middleware import ModelFallbackMiddleware, ModelRetryMiddleware, TodoListMiddleware
from langchain_core.rate_limiters import InMemoryRateLimiter

from prompts import (
    LOG_ANALYZER_PROMPT,
    ROOT_CAUSE_PROMPT,
    SERVER_DIAGNOSTICIAN_PROMPT,
    SOLUTION_ADVISOR_PROMPT,
    SUPERVISOR_PROMPT,
)
from tools import LOG_TOOLS, SERVER_TOOLS, get_system_metrics, search_logs

load_dotenv()

ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "output"
DEFAULT_TASK = (
    "My Linux server is not responding. CPU usage is high, some services are failing, and I am "
    "seeing errors in the application logs. Investigate the issue and provide possible solutions."
)


PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
DEFAULT_MODELS = {"gemini": "gemini-3.5-flash-lite","groq": "openai/gpt-oss-120b"}


def model_name() -> str:
    legacy = os.getenv("GROQ_MODEL") if PROVIDER == "groq" else None
    return os.getenv("LLM_MODEL") or legacy or DEFAULT_MODELS.get(PROVIDER, "")


def api_key_var() -> str:
    return "GOOGLE_API_KEY" if PROVIDER == "gemini" else "GROQ_API_KEY"


def build_model():
    if not os.getenv(api_key_var()):
        raise RuntimeError(f"{api_key_var()} is not set. Copy .env.example to .env and add your key.")
    # Free tiers cap requests per minute; the limiter spaces out calls so we stay under it.
    rpm = float(os.getenv("LLM_REQUESTS_PER_MINUTE", "8"))
    limiter = InMemoryRateLimiter(requests_per_second=rpm / 60, check_every_n_seconds=0.5, max_bucket_size=1)
    if PROVIDER == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        # max_retries=0 + timeout: let ModelRetryMiddleware retry visibly instead of the SDK retrying silently.
        return ChatGoogleGenerativeAI(
            model=model_name(), temperature=0, rate_limiter=limiter, max_retries=0, timeout=120
        )
    if PROVIDER == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(model=model_name(), temperature=0, rate_limiter=limiter)
    raise RuntimeError(f"Unknown LLM_PROVIDER '{PROVIDER}'. Use 'gemini' or 'groq'.")


RETRYABLE_MARKERS = (
    "429", "rate_limit", "resource_exhausted", "quota",      # rate limits (per-minute quota)
    "500", "503", "unavailable", "overloaded", "timeout",     # temporary server problems
    "output_parse_failed", "tool_use_failed",                 # Groq: malformed model output
)


def _is_retryable(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    if "per day" in text or "perday" in text:  # daily quota used up: waiting a minute won't help
        return False
    retry = any(marker in text for marker in RETRYABLE_MARKERS)
    if retry:
        print(f"  ... model busy/rate-limited ({type(exc).__name__}), waiting and retrying", flush=True)
    return retry


OPENROUTER_URL = "https://openrouter.ai/api/v1"


def _backup_settings():
    """Backup model settings. OpenRouter keys (sk-or-...) use OpenRouter's OpenAI-compatible endpoint."""
    key = os.getenv("OPENAI_API_KEY", "")
    is_openrouter = key.startswith("sk-or-")
    base_url = os.getenv("OPENAI_BASE_URL") or (OPENROUTER_URL if is_openrouter else None)
    model = os.getenv("OPENAI_MODEL") or "gpt-5-mini"
    if is_openrouter and "/" not in model:  # OpenRouter names models as "<provider>/<model>"
        model = f"openai/{model}"
    return key, base_url, model, is_openrouter


def build_backup_model():
    """Optional backup: used only when OPENAI_API_KEY is set and the main model fails."""
    key, base_url, model, _ = _backup_settings()
    if not key:
        return None
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=model, base_url=base_url, max_retries=2, timeout=120)


def backup_label() -> str:
    key, _, model, is_openrouter = _backup_settings()
    if not key:
        return "none"
    return f"{'openrouter' if is_openrouter else 'openai'} / {model}"


def build_middleware():
    """Retry the model a few times; if it still fails (e.g. free daily quota used up), switch to the backup."""
    stack = [build_retry()]
    backup = build_backup_model()
    if backup is not None:
        # Outer layer: the fallback catches whatever the (retried) main model could not answer.
        stack.insert(0, ModelFallbackMiddleware(backup))
    return stack


def build_retry():
    # Gemini free-tier 429s ask you to wait up to ~60s, so back off generously.
    return ModelRetryMiddleware(
        max_retries=4, retry_on=_is_retryable, on_failure="error", initial_delay=15.0, max_delay=65.0
    )


def build_agent():
    subagents = [
        {
            "name": "log-analyzer",
            "description": "Analyzes application, syslog and nginx logs: error patterns, timeline, affected components, noise.",
            "system_prompt": LOG_ANALYZER_PROMPT,
            "tools": LOG_TOOLS,
        },
        {
            "name": "server-diagnostician",
            "description": "Analyzes CPU, memory, load, swap, DB connections, processes, services and disk usage.",
            "system_prompt": SERVER_DIAGNOSTICIAN_PROMPT,
            "tools": SERVER_TOOLS,
        },
        {
            "name": "root-cause-analyst",
            "description": "Reads the log and server findings, correlates them and determines the root cause. Run after the two analysts.",
            "system_prompt": ROOT_CAUSE_PROMPT,
            "tools": [search_logs, get_system_metrics],
        },
        {
            "name": "solution-advisor",
            "description": "Reads the root cause and proposes immediate, short-term and preventive remediation. Run last.",
            "system_prompt": SOLUTION_ADVISOR_PROMPT,
            "tools": [],
        },
    ]
    for sub in subagents:
        sub["middleware"] = build_middleware()
    # The supervisor gets no domain tools on purpose: it must delegate.
    return create_deep_agent(
        model=build_model(),
        tools=[],
        system_prompt=SUPERVISOR_PROMPT,
        subagents=subagents,
        middleware=[TodoListMiddleware(), *build_middleware()],
    )


def stream_investigation(agent, task: str):
    """Yield the full agent state after every step (state has messages, todos, files)."""
    payload = {"messages": [{"role": "user", "content": task}]}
    yield from agent.stream(payload, stream_mode="values", config={"recursion_limit": 150})


# ----------------------------------------------------------------- helpers
def file_text(entry) -> str:
    """Virtual-file entries differ between deepagents versions (str, dict with list/str content)."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        content = entry.get("content", "")
        return "\n".join(content) if isinstance(content, list) else str(content)
    return str(entry)


def message_text(msg) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        return " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content)


def save_outputs(state: dict) -> list[Path]:
    """Write the agent virtual files (report + findings) to ./output on real disk."""
    saved = []
    for path, entry in (state.get("files") or {}).items():
        target = OUTPUT_DIR / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file_text(entry), encoding="utf-8")
        saved.append(target)
    return saved
