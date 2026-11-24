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

## Where each Deep Agents concept lives
| Concept | Where |
|---|---|
| Planning (`write_todos`) | built in; prompt in `prompts.py` (SUPERVISOR_PROMPT) |
| Virtual filesystem | built in; agents write `/findings/*` and `/reports/*`; saved by `save_outputs()` in `agent.py` |
| Subagents | `subagents=[...]` in `agent.py`, each with own prompt + tools; the security analyst is used only when needed |
| Custom tools | `tools.py`: `make_tools(folder)` builds tools bound to one scenario (logs, metrics, processes, services, disk, recent changes, network) |
| Scenarios | `scenarios.py` (title + problem text) and `generate_sample_data.py` (the data) |

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
