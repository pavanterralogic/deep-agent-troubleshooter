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
The model runs **locally with [Ollama](https://ollama.com)**: no API key, no cost, no rate limits, and
no data leaves your machine.

1. Install Ollama from https://ollama.com/download (Windows: run `OllamaSetup.exe`; Linux:
   `curl -fsSL https://ollama.com/install.sh | sh`). It starts a local server on `http://localhost:11434`.
2. Download a model that supports tool calling (~5 GB, needs ~8 GB free RAM):
   ```bash
   ollama pull qwen3:8b
   ```
3. Install the Python project:
   ```bash
   python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
   pip install -U -r requirements.txt
   cp .env.example .env                                   # defaults already point to Ollama + qwen3:8b
   python generate_sample_data.py                         # optional: sample_data is already included
   ```

**Choosing a model** (set `LLM_MODEL` in `.env`): the model **must support tool calling**.
`qwen3:8b` (default, good balance), `qwen2.5:7b` / `llama3.1:8b` (lighter), `qwen3:14b` (better
reasoning, needs ~16 GB RAM or a GPU). On a laptop CPU a full investigation takes several minutes;
with a GPU it is much faster. Gemini/Groq are still supported with `LLM_PROVIDER=gemini|groq`.

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

**What it does:** builds the **supervisor** - the "team lead" agent. It gets the model (local Ollama), its instructions (`SUPERVISOR_PROMPT`), the list of subagents it may delegate to, and middleware (planning, retry, fallback). It has no log or metrics tools on purpose, so it cannot investigate by itself: it must plan, delegate, review and write the final report. Deep Agents automatically adds the `task` tool (to call subagents) and the file tools.

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

**What it does:** defines the **specialists**. Each subagent is a separate model conversation with its own instructions and only the tools it needs (the log analyzer gets log tools, the server diagnostician gets metrics tools, and so on). The `description` is what the supervisor reads to decide whom to call. Each subagent starts with a clean, isolated context, so long log output does not fill up the supervisor's memory - it only gets back a short summary.

**3. Delegation with the built-in `task` tool, and shared virtual files** - [prompts.py](prompts.py) `SUPERVISOR_PROMPT`
```text
2. DELEGATE (one subagent per `task` call, give it a clear, self-contained instruction):
   a. `log-analyzer`          -> writes /findings/log_analysis.md
   b. `server-diagnostician`  -> writes /findings/server_diagnostics.md
   c. `security-analyst`      -> writes /findings/security_analysis.md. Delegate to it ONLY IF ...
```
The agents write findings with the built-in `write_file` tool; the supervisor reads them with `read_file`.


**What it does:** tells the supervisor the **order of work** and who writes which file. The `task` tool starts a subagent with an instruction and waits for its answer. Instead of passing long text back and forth, agents share results through **virtual files** (`/findings/*.md`): one agent writes, the next one reads. The rule for `security-analyst` makes the supervisor call it only when the findings look suspicious, which saves model calls.

**4. Virtual files -> real disk** - [agent.py](agent.py) `save_outputs()`
```python
for path, entry in (state.get("files") or {}).items():   # e.g. "/reports/troubleshooting_report.md"
    target = base / path.lstrip("/")
    target.write_text(file_text(entry), encoding="utf-8")
```

**What it does:** the virtual files exist only in the agent's memory (its state) while it runs. After the run, this copies them to real files in `output/<scenario>/` so you can open the report and findings afterwards.

### LangChain

**1. Tools** - a Python function becomes a tool the model can call; the docstring tells the model how to use it - [tools.py](tools.py)
```python
from langchain_core.tools import tool

@tool
def search_logs(log_name: str, pattern: str, max_matches: int = 25) -> str:
    """Search a log with a case-insensitive regex and return matching lines with line numbers."""
```

**What it does:** gives the agents **hands**. The model cannot read files by itself; it can only ask to call a tool. `@tool` turns this function into a tool: the function name, parameters and docstring are sent to the model so it knows when and how to use it. When the model asks for `search_logs("auth", "Failed password")`, the code runs and the result goes back to the model. All tools read the simulated data in `sample_data/<scenario>/`.

**2. Chat model** - local model served by Ollama - [agent.py](agent.py) `build_model()`
```python
from langchain_ollama import ChatOllama

ChatOllama(model=model_name(), base_url=OLLAMA_URL, temperature=0,
           num_ctx=16384, reasoning=False, keep_alive="30m")
```

**What it does:** the **brain**. `ChatOllama` sends the conversation and tool definitions to the Ollama server running on your own machine and gets back text or tool calls - the same interface as any cloud model, so the rest of the code does not change. `num_ctx=16384` raises Ollama's small default context window so the long agent prompts fit; `reasoning=False` skips the slow "thinking" phase of qwen3; `keep_alive` keeps the model loaded in memory between calls. (Gemini/Groq via `ChatGoogleGenerativeAI`/`ChatGroq` are still available as alternatives.)

**3. Rate limiter** - only for cloud free tiers - [agent.py](agent.py) `build_model()`
```python
from langchain_core.rate_limiters import InMemoryRateLimiter

limiter = InMemoryRateLimiter(requests_per_second=rpm / 60, check_every_n_seconds=0.5, max_bucket_size=1)
```

**What it does:** used only with Gemini/Groq, whose free tiers allow about 10 requests per minute. The limiter spaces out calls so they are not rejected. Ollama runs locally and has no limit, so it is not used there.

**4. Middleware** - planning tool, retries, fallback to the backup model - [agent.py](agent.py)
```python
from langchain.agents.middleware import ModelFallbackMiddleware, ModelRetryMiddleware, TodoListMiddleware

TodoListMiddleware()                       # gives the supervisor `write_todos` (the "Plan" panel)
ModelRetryMiddleware(max_retries=4, retry_on=_is_retryable, initial_delay=15.0, max_delay=65.0)
ModelFallbackMiddleware(backup)            # optional: if the main model fails, use a backup model
```

**What it does:** middleware wraps every model call, like a guard around it:
- `TodoListMiddleware` adds the `write_todos` tool, so the supervisor writes and updates a **plan** (shown in the CLI and in the app's Plan panel).
- `ModelRetryMiddleware` catches temporary errors (Ollama still starting, timeouts, rate limits) and **retries** after waiting, printing a line so you know it is waiting, not stuck.
- `ModelFallbackMiddleware` **switches to a backup model** if the main model still fails. Off by default with Ollama so nothing leaves the machine; enable with `ENABLE_CLOUD_BACKUP=true` and `OPENAI_API_KEY`.

### LangGraph

LangGraph is not imported directly: `create_deep_agent` returns a LangGraph `CompiledStateGraph`.

**1. Run the graph and stream every step** - [agent.py](agent.py) `stream_investigation()`
```python
payload = {"messages": [{"role": "user", "content": task}]}
yield from agent.stream(payload, stream_mode="values", config={"recursion_limit": 150})
```

**What it does:** **runs** the agent. LangGraph executes the loop *model -> tool call -> tool result -> model ...* until the supervisor gives its final answer. `stream` returns the full state after every step, so we can show progress live instead of waiting for the end. `recursion_limit` is a safety cap: the run stops after 150 steps so a confused model cannot loop forever.

**2. Read the graph state while it runs** (`todos`, `messages`, `files`) - [run_cli.py](run_cli.py) (same in [app.py](app.py))
```python
for state in stream_investigation(agent, task):
    todos = state.get("todos")             # the supervisor's plan
    msgs = state.get("messages", [])       # AIMessage tool calls (e.g. `task log-analyzer`) and ToolMessage results
```

**What it does:** the **state** is LangGraph's memory of the run: `messages` (every model reply and tool call), `todos` (the plan) and `files` (the virtual files). The CLI prints new tool calls and plan changes from it, and the Streamlit app uses it to update the Plan and Agent activity panels.

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
- **`Connection refused` / `ConnectError`:** Ollama is not running. Start the Ollama app (or `ollama serve`) and check `ollama list` shows your model.
- **`model "qwen3:8b" not found`:** run `ollama pull qwen3:8b` (or the model in `LLM_MODEL`).
- **`does not support tools`:** pick a model with tool calling (qwen3, qwen2.5, llama3.1). Models like gemma or phi without tool support will not work.
- **Very slow / out of memory:** use a smaller model (`qwen2.5:7b`), lower `OLLAMA_NUM_CTX` to `12288`, close other apps. A GPU makes a big difference.
- **Agent wanders or skips a step:** small local models are weaker than cloud models; try `qwen3:14b` or `OLLAMA_THINKING=true` if you have the RAM/time.
- **Hosting:** Streamlit Community Cloud cannot run Ollama. Host the app on a machine (or company server/VM) that runs Ollama, e.g. `streamlit run app.py --server.address 0.0.0.0`, or point `OLLAMA_BASE_URL` at a shared Ollama server on your network.
- **Rate limits on cloud free tiers:** Gemini free tier allows ~10 requests/min and a daily cap; lower `LLM_REQUESTS_PER_MINUTE` if you see 429s. Groq free tier (`LLM_PROVIDER=groq`) is too small for Deep Agents prompts (413 errors).
- **Agent skips delegation or loops:** use a stronger tool-calling model; keep `temperature=0`.
- **TypeError on `create_deep_agent` / subagent keys:** the library changes fast; run `pip install -U deepagents` and compare with the docs.

## Ideas to extend
Human-in-the-loop approval before "risky" fixes, real log tools over SSH, memory across incidents, evaluation against the planted root cause.
