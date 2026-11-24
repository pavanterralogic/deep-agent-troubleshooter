# Deep Agent - IT Infrastructure Troubleshooter (prototype)

A supervisor Deep Agent that investigates a simulated Linux server incident by delegating to
specialist subagents, then writes a troubleshooting report. Uses simulated logs and metrics only.
Three incidents (scenarios) are included; pick one in the app or with `--scenario`.

```
User request -> Supervisor (plan + review)
                 |-> log-analyzer          -> /findings/log_analysis.md
                 |-> server-diagnostician  -> /findings/server_diagnostics.md
                 |-> security-analyst      -> /findings/security_analysis.md   (only if suspicious)
                 |-> root-cause-analyst    -> /findings/root_cause.md
                 |-> solution-advisor      -> /findings/solutions.md
               -> /reports/troubleshooting_report.md
```

## Setup
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -U -r requirements.txt
cp .env.example .env                                   # add your free GOOGLE_API_KEY (aistudio.google.com/apikey)
python generate_sample_data.py                         # optional: sample_data is already included
```

## Run
```bash
python run_cli.py --list                          # show scenarios
python run_cli.py                                 # default scenario (runaway_job)
python run_cli.py --scenario bad_deployment
python run_cli.py --scenario security_incident
streamlit run app.py                              # dashboard with a scenario picker
```
Files the agents wrote are saved to `./output/<scenario>/` (report in `reports/`, findings in `findings/`).

## How LangChain, LangGraph and Deep Agents are used

Three layers build on each other: **Deep Agents** (planning, virtual files, subagents) is built on
**LangChain** (models, tools, middleware), which runs on **LangGraph** (the graph that executes the
agent loop and holds its state). Below are the key lines of each.

### Deep Agents

**1. Create the supervisor agent** - [agent.py](agent.py) `build_agent()`
```python
from deepagents import create_deep_agent

return create_deep_agent(
    model=build_model(),
    tools=[],                          # supervisor has no domain tools: it must delegate
    system_prompt=SUPERVISOR_PROMPT,
    subagents=subagents,
    middleware=[TodoListMiddleware(), *build_middleware()],
)
```

**2. Subagents** - each has its own prompt, tools and isolated context - [agent.py](agent.py)
```python
subagents = [
    {
        "name": "log-analyzer",
        "description": "Analyzes application, syslog, nginx and auth logs: ...",
        "system_prompt": LOG_ANALYZER_PROMPT,
        "tools": t.LOG_TOOLS,
    },
    # server-diagnostician, security-analyst, root-cause-analyst, solution-advisor ...
]
```

**3. Delegation with the built-in `task` tool, and shared virtual files** - [prompts.py](prompts.py) `SUPERVISOR_PROMPT`
```text
2. DELEGATE (one subagent per `task` call, give it a clear, self-contained instruction):
   a. `log-analyzer`          -> writes /findings/log_analysis.md
   b. `server-diagnostician`  -> writes /findings/server_diagnostics.md
   c. `security-analyst`      -> writes /findings/security_analysis.md. Delegate to it ONLY IF ...
```
The agents write findings with the built-in `write_file` tool; the supervisor reads them with `read_file`.

**4. Virtual files -> real disk** - [agent.py](agent.py) `save_outputs()`
```python
for path, entry in (state.get("files") or {}).items():   # e.g. "/reports/troubleshooting_report.md"
    target = base / path.lstrip("/")
    target.write_text(file_text(entry), encoding="utf-8")
```

### LangChain

**1. Tools** - a Python function becomes a tool the model can call; the docstring tells the model how to use it - [tools.py](tools.py)
```python
from langchain_core.tools import tool

@tool
def search_logs(log_name: str, pattern: str, max_matches: int = 25) -> str:
    """Search a log with a case-insensitive regex and return matching lines with line numbers."""
```

**2. Chat models** - main model (Gemini) and optional backup (OpenAI / OpenRouter) - [agent.py](agent.py) `build_model()`, `build_backup_model()`
```python
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

ChatGoogleGenerativeAI(model=model_name(), temperature=0, rate_limiter=limiter, max_retries=0, timeout=120)
ChatOpenAI(model=model, base_url=base_url, max_retries=2, timeout=120)
```

**3. Rate limiter** - stays under the free-tier requests-per-minute limit - [agent.py](agent.py) `build_model()`
```python
from langchain_core.rate_limiters import InMemoryRateLimiter

limiter = InMemoryRateLimiter(requests_per_second=rpm / 60, check_every_n_seconds=0.5, max_bucket_size=1)
```

**4. Middleware** - planning tool, retries, fallback to the backup model - [agent.py](agent.py)
```python
from langchain.agents.middleware import ModelFallbackMiddleware, ModelRetryMiddleware, TodoListMiddleware

TodoListMiddleware()                       # gives the supervisor `write_todos` (the "Plan" panel)
ModelRetryMiddleware(max_retries=4, retry_on=_is_retryable, initial_delay=15.0, max_delay=65.0)
ModelFallbackMiddleware(backup)            # if Gemini still fails, use the backup model
```

### LangGraph

LangGraph is not imported directly: `create_deep_agent` returns a LangGraph `CompiledStateGraph`.

**1. Run the graph and stream every step** - [agent.py](agent.py) `stream_investigation()`
```python
payload = {"messages": [{"role": "user", "content": task}]}
yield from agent.stream(payload, stream_mode="values", config={"recursion_limit": 150})
```

**2. Read the graph state while it runs** (`todos`, `messages`, `files`) - [run_cli.py](run_cli.py) (same in [app.py](app.py))
```python
for state in stream_investigation(agent, task):
    todos = state.get("todos")             # the supervisor's plan
    msgs = state.get("messages", [])       # AIMessage tool calls (e.g. `task log-analyzer`) and ToolMessage results
```

### Other files
| File | Role |
|---|---|
| [scenarios.py](scenarios.py) | The incidents you can pick (title + problem description) |
| [generate_sample_data.py](generate_sample_data.py) | Creates the simulated logs/metrics in `sample_data/<scenario>/` |
| [run_cli.py](run_cli.py) | Terminal runner (`--scenario`, `--list`) |
| [app.py](app.py) | Streamlit dashboard (scenario picker, live plan, activity, report download) |

## The planted incidents (what a good report should find)
**runaway_job** - Crontab edited at 08:57 -> `report_export.py --full` ran at 09:00 with an unbounded query -> CPU/DB pool
saturation (20/20) -> "Database connection timeout" + HTTP 500s from 09:05 -> memory exhaustion and OOM kills
at 09:13 -> `app.service` restart loop, nginx 502/504. Noise to ignore: TLS expiry warning, pydantic
deprecation, /var/log at 91%, port-scanner lines in nginx.

**bad_deployment** - Release v2.8.0 at 10:00 renamed the pool setting and shipped `DB_POOL_SIZE=2` (was 20).
The server is *not* overloaded (CPU ~20%): requests queue for 2 DB connections and time out -> HTTP 500/504.
Evidence: "Database pool initialised (size=2...)" at 10:00:47 and the diff in `get_recent_changes`. Red
herring: migration 0042 ran fine. Fix: correct the setting or roll back to v2.7.3.

**security_incident** - SSH brute force from 198.51.100.23 (02:00-02:11), password for `deploy` guessed at
02:11:37, cryptominer `/tmp/.X11-unix/.k/kdevtmpfsi` downloaded and started at 02:13 with an @reboot crontab
(no change ticket), mining to 203.0.113.50:3333 -> CPU ~100%, gunicorn worker timeouts. Red herring: the
internal `ops` login at 02:31. Fix: contain first (block IP, kill process, remove crontab, rotate credentials,
disable SSH password login), then restore.

To add your own scenario: add it to `scenarios.py`, write a generator function in `generate_sample_data.py`
that produces the same files, and run `python generate_sample_data.py`.

## Troubleshooting
- **Rate limits on free tiers:** Gemini free tier allows ~10 requests/min and a daily cap; lower `LLM_REQUESTS_PER_MINUTE` if you see 429s. Groq free tier (`LLM_PROVIDER=groq`) is too small for Deep Agents prompts (413 errors).
- **Agent skips delegation or loops:** use a stronger tool-calling model; keep `temperature=0`.
- **TypeError on `create_deep_agent` / subagent keys:** the library changes fast; run `pip install -U deepagents` and compare with the docs.

## Ideas to extend
Human-in-the-loop approval before "risky" fixes, real log tools over SSH, memory across incidents, evaluation against the planted root cause.
