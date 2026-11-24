"""
Builds the supervisor Deep Agent with five specialist subagents, for one chosen scenario.

Deep Agents concepts demonstrated here:
  * Planning        - built-in `write_todos` tool (supervisor keeps a todo list)
  * Filesystem      - built-in virtual files (/findings/*.md, /reports/*.md) shared by all agents
  * Subagents       - `subagents=[...]`, each with its own prompt, tools and isolated context
  * Tools           - custom tools from tools.py, bound to the scenario's data folder
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
    SECURITY_ANALYST_PROMPT,
    SERVER_DIAGNOSTICIAN_PROMPT,
    SOLUTION_ADVISOR_PROMPT,
    SUPERVISOR_PROMPT,
)
from scenarios import DEFAULT_SCENARIO, SCENARIOS, scenario_dir
from tools import make_tools

load_dotenv()

ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "output"
DEFAULT_TASK = SCENARIOS[DEFAULT_SCENARIO]["task"]


PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()
DEFAULT_MODELS = {"ollama": "qwen3:8b", "gemini": "gemini-3.5-flash-lite", "groq": "openai/gpt-oss-120b"}
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")


def model_name() -> str:
    legacy = os.getenv("GROQ_MODEL") if PROVIDER == "groq" else None
    return os.getenv("LLM_MODEL") or legacy or DEFAULT_MODELS.get(PROVIDER, "")


def api_key_var() -> str:
    """Env var holding the API key; empty for Ollama (local, no key needed)."""
    return {"gemini": "GOOGLE_API_KEY", "groq": "GROQ_API_KEY"}.get(PROVIDER, "")


def build_model():
    if PROVIDER == "ollama":
        from langchain_ollama import ChatOllama

        # Local model served by Ollama: no API key, no rate limit.
        # num_ctx: Ollama's default context window is too small for the agent's prompts + tool schemas.
        # reasoning=False: skip the "thinking" phase of models like qwen3, much faster on a laptop CPU.
        return ChatOllama(
            model=model_name(),
            base_url=OLLAMA_URL,
            temperature=0,
            num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "16384")),
            reasoning=os.getenv("OLLAMA_THINKING", "false").lower() == "true",
            keep_alive="30m",
        )
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
    raise RuntimeError(f"Unknown LLM_PROVIDER '{PROVIDER}'. Use 'ollama', 'gemini' or 'groq'.")


RETRYABLE_MARKERS = (
    "429", "rate_limit", "resource_exhausted", "quota",      # rate limits (per-minute quota)
    "500", "503", "unavailable", "overloaded", "timeout",     # temporary server problems
    "connecterror", "connection refused",                     # Ollama still starting up
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


def backup_enabled() -> bool:
    """Cloud backup is off by default with Ollama (everything stays local); opt in with ENABLE_CLOUD_BACKUP=true."""
    if not os.getenv("OPENAI_API_KEY"):
        return False
    default = "false" if PROVIDER == "ollama" else "true"
    return os.getenv("ENABLE_CLOUD_BACKUP", default).lower() == "true"


def build_backup_model():
    """Optional backup: used only when enabled and the main model fails."""
    if not backup_enabled():
        return None
    key, base_url, model, _ = _backup_settings()
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=model, base_url=base_url, max_retries=2, timeout=120)


def backup_label() -> str:
    if not backup_enabled():
        return "none"
    _, _, model, is_openrouter = _backup_settings()
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


def build_agent(scenario: str = DEFAULT_SCENARIO):
    """Build the supervisor + subagents with tools that read sample_data/<scenario>/."""
    t = make_tools(scenario_dir(scenario))
    subagents = [
        {
            "name": "log-analyzer",
            "description": "Analyzes application, syslog, nginx and auth logs: error patterns, timeline, affected components, noise.",
            "system_prompt": LOG_ANALYZER_PROMPT,
            "tools": t.LOG_TOOLS,
        },
        {
            "name": "server-diagnostician",
            "description": "Analyzes CPU, memory, load, swap, DB connections, processes, services, disk usage and recent changes/deployments.",
            "system_prompt": SERVER_DIAGNOSTICIAN_PROMPT,
            "tools": t.SERVER_TOOLS,
        },
        {
            "name": "security-analyst",
            "description": "Checks for attacks/compromise: SSH brute force, suspicious logins, unknown processes, unusual outbound connections, unauthorized crontab changes. Use only when findings show suspicious signs.",
            "system_prompt": SECURITY_ANALYST_PROMPT,
            "tools": t.SECURITY_TOOLS,
        },
        {
            "name": "root-cause-analyst",
            "description": "Reads all findings files, correlates them and determines the root cause. Run after the analysts.",
            "system_prompt": ROOT_CAUSE_PROMPT,
            "tools": t.ROOT_CAUSE_TOOLS,
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


def save_outputs(state: dict, scenario: str = "") -> list[Path]:
    """Write the agent virtual files (report + findings) to ./output/<scenario>/ on real disk."""
    saved = []
    base = OUTPUT_DIR / scenario if scenario else OUTPUT_DIR
    for path, entry in (state.get("files") or {}).items():
        target = base / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file_text(entry), encoding="utf-8")
        saved.append(target)
    return saved
