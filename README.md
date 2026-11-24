# Deep Agent - IT Infrastructure Troubleshooter (prototype)

A supervisor Deep Agent that investigates a simulated Linux server incident by delegating to four
subagents, then writes a troubleshooting report. Uses simulated logs and metrics only.

```
User request -> Supervisor (plan + review)
                 |-> log-analyzer          -> /findings/log_analysis.md
                 |-> server-diagnostician  -> /findings/server_diagnostics.md
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
python run_cli.py          # Stage 1-4: watch plan, delegation and tool calls in the terminal
streamlit run app.py       # Stage 5: dashboard
```
Files the agents wrote are saved to `./output/` (report in `output/reports/`, findings in `output/findings/`).

## Where each Deep Agents concept lives
| Concept | Where |
|---|---|
| Planning (`write_todos`) | built in; prompt in `prompts.py` (SUPERVISOR_PROMPT) |
| Virtual filesystem | built in; agents write `/findings/*` and `/reports/*`; saved by `save_outputs()` in `agent.py` |
| Subagents | `subagents=[...]` in `agent.py`, each with own prompt + tools |
| Custom tools | `tools.py` (read_log, search_logs, summarize_log_levels, get_system_metrics, ...) |

## The planted incident (what a good report should find)
Crontab edited at 08:57 -> `report_export.py --full` ran at 09:00 with an unbounded query -> CPU/DB pool
saturation (20/20) -> "Database connection timeout" + HTTP 500s from 09:05 -> memory exhaustion and OOM kills
at 09:13 -> `app.service` restart loop, nginx 502/504. Noise to ignore: TLS expiry warning, pydantic
deprecation, /var/log at 91%, port-scanner lines in nginx.

## Troubleshooting
- **Rate limits on free tiers:** Gemini free tier allows ~10 requests/min and a daily cap; lower `LLM_REQUESTS_PER_MINUTE` if you see 429s. Groq free tier (`LLM_PROVIDER=groq`) is too small for Deep Agents prompts (413 errors).
- **Agent skips delegation or loops:** use a stronger tool-calling model; keep `temperature=0`.
- **TypeError on `create_deep_agent` / subagent keys:** the library changes fast; run `pip install -U deepagents` and compare with the docs.

## Ideas to extend
Human-in-the-loop approval before "risky" fixes, real log tools over SSH, memory across incidents, evaluation against the planted root cause.
