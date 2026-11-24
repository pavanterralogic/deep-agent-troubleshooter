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

The project uses three layers that build on each other:

```
Deep Agents (deepagents)   -> the agent "recipe": planning, virtual files, subagents
   built on LangChain       -> models, tools, middleware (retry, fallback, todo list)
   running on LangGraph     -> the graph that executes the agent loop, holds state, streams steps
```

### Deep Agents - `deepagents`
| What | Where |
|---|---|
| `create_deep_agent(...)` builds the supervisor with its subagents | [agent.py](agent.py) - `build_agent()` (line ~134) |
| Subagent definitions (name, description, prompt, tools, middleware) | [agent.py](agent.py) - `subagents = [...]` (line ~137) |
| Built-in `task` tool (supervisor delegates to a subagent) | provided by Deep Agents; how to use it is in [prompts.py](prompts.py) `SUPERVISOR_PROMPT` |
| Built-in virtual filesystem (`write_file`, `read_file`, `edit_file`, `ls`) used for `/findings/*` and `/reports/*` | provided by Deep Agents; files are copied to disk by `save_outputs()` in [agent.py](agent.py) |
| Prompts for supervisor and subagents | [prompts.py](prompts.py) |

### LangChain - `langchain`, `langchain_core`, `langchain_google_genai`, `langchain_openai`, `langchain_groq`
| What | Where |
|---|---|
| `@tool` - turns Python functions into tools the model can call (logs, metrics, processes, services, disk, changes, network) | [tools.py](tools.py) - `make_tools()` |
| Chat models: `ChatGoogleGenerativeAI` (Gemini, main), `ChatGroq` (optional), `ChatOpenAI` (backup, also used for OpenRouter) | [agent.py](agent.py) - `build_model()`, `build_backup_model()` |
| `InMemoryRateLimiter` - keeps calls under the free-tier requests-per-minute limit | [agent.py](agent.py) - `build_model()` |
| `ModelRetryMiddleware` - retries rate limits / temporary errors, prints a line when waiting | [agent.py](agent.py) - `build_retry()`, `_is_retryable()` |
| `ModelFallbackMiddleware` - switches to the backup model if the main one still fails | [agent.py](agent.py) - `build_middleware()` |
| `TodoListMiddleware` - gives the supervisor the `write_todos` planning tool (the "Plan" panel) | [agent.py](agent.py) - `build_agent()` |

### LangGraph - `langgraph` (used through Deep Agents, not imported directly)
| What | Where |
|---|---|
| The agent returned by `create_deep_agent` is a LangGraph `CompiledStateGraph` (model node -> tools node -> model ... until done) | [agent.py](agent.py) - `build_agent()` |
| `agent.stream(..., stream_mode="values")` - runs the graph and yields the full state after every step | [agent.py](agent.py) - `stream_investigation()` |
| `recursion_limit` - LangGraph's cap on the number of steps in one run | [agent.py](agent.py) - `stream_investigation()` |
| Graph state: `messages` (conversation + tool calls), `todos` (plan), `files` (virtual files) | read in [run_cli.py](run_cli.py) and [app.py](app.py) while streaming; files saved by `save_outputs()` |
| Each subagent runs as its own smaller graph inside the supervisor's `task` tool call | built by Deep Agents from the `subagents` list |

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
