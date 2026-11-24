"""
Custom tools for the troubleshooting agents.

All tools read the *simulated* data of one scenario in ./sample_data/<scenario>/, so nothing
touches a real server. `make_tools(data_dir)` builds a fresh set of tools bound to one
scenario folder, so different scenarios can run at the same time (e.g. two Streamlit users).

To go "real" later, replace the bodies (e.g. run `journalctl`, `top`, `systemctl status`,
`ss -tunp`, `last` over SSH) and keep the same names/signatures.
"""
import json
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from langchain_core.tools import tool

SAMPLE_ROOT = Path(__file__).parent / "sample_data"
LOG_FILES = {
    "application": "application.log",
    "syslog": "syslog.log",
    "nginx": "nginx_error.log",
    "auth": "auth.log",
}
LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\[([^\]]+)\]\s+(.*)$"
)
MAX_LINES = 80  # keep tool output small so the agent's context stays healthy
BASELINE_MINUTES = 10  # first N metric samples are treated as "normal" baseline


def make_tools(data_dir: Path) -> SimpleNamespace:
    """Build all tools bound to one scenario's data folder."""
    data_dir = Path(data_dir)

    def _read_lines(log_name: str):
        fname = LOG_FILES.get(log_name.lower())
        if not fname:
            return None, f"Unknown log '{log_name}'. Available logs: {', '.join(LOG_FILES)}"
        path = data_dir / fname
        if not path.exists():
            return None, f"Log file missing: {path.name}. Run `python generate_sample_data.py`."
        return path.read_text(encoding="utf-8").splitlines(), None

    def _read_json(name: str):
        path = data_dir / name
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ logs
    @tool
    def list_available_logs() -> str:
        """List the log files available for investigation, with line counts and time range."""
        out = []
        for name in LOG_FILES:
            lines, err = _read_lines(name)
            if err:
                out.append(f"- {name}: {err}")
                continue
            stamps = [LINE_RE.match(l) for l in lines]
            stamps = [s.group(2) for s in stamps if s]
            rng = f"{stamps[0]} to {stamps[-1]}" if stamps else "n/a"
            out.append(f"- {name}: {len(lines)} lines, time range {rng}")
        return "\n".join(out)

    @tool
    def read_log(log_name: str, from_time: str = "", to_time: str = "", max_lines: int = 60) -> str:
        """Read lines from a log, optionally limited to a time window.

        Args:
            log_name: one of 'application', 'syslog', 'nginx', 'auth'.
            from_time: optional start 'HH:MM' (inclusive), e.g. '09:00'.
            to_time: optional end 'HH:MM' (inclusive), e.g. '09:15'.
            max_lines: maximum lines to return (hard cap 80).
        """
        lines, err = _read_lines(log_name)
        if err:
            return err
        max_lines = max(1, min(int(max_lines), MAX_LINES))
        selected = []
        for line in lines:
            m = LINE_RE.match(line)
            hhmm = m.group(2)[:5] if m else ""
            if from_time and hhmm < from_time:
                continue
            if to_time and hhmm > to_time:
                continue
            selected.append(line)
        note = ""
        if len(selected) > max_lines:
            note = f"\n[truncated: showing first {max_lines} of {len(selected)} matching lines - narrow the time window]"
            selected = selected[:max_lines]
        return "\n".join(selected) + note if selected else "No log lines in that window."

    @tool
    def search_logs(log_name: str, pattern: str, max_matches: int = 25) -> str:
        """Search a log with a case-insensitive regex and return matching lines with line numbers.

        Args:
            log_name: one of 'application', 'syslog', 'nginx', 'auth'.
            pattern: regex, e.g. 'timeout|OOM|Out of memory|Failed password'.
            max_matches: maximum matches to return (hard cap 80).
        """
        lines, err = _read_lines(log_name)
        if err:
            return err
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"Invalid regex: {e}"
        hits = [(i + 1, l) for i, l in enumerate(lines) if rx.search(l)]
        cap = max(1, min(int(max_matches), MAX_LINES))
        body = "\n".join(f"{n}: {l}" for n, l in hits[:cap])
        return f"{len(hits)} match(es) for /{pattern}/ in {log_name}.\n{body}" if hits else f"No matches for /{pattern}/ in {log_name}."

    @tool
    def summarize_log_levels(log_name: str) -> str:
        """Count log levels and list the most frequent WARNING/ERROR/CRITICAL messages (numbers normalised)."""
        lines, err = _read_lines(log_name)
        if err:
            return err
        levels, groups, first, last = Counter(), Counter(), {}, {}
        for line in lines:
            m = LINE_RE.match(line)
            if not m:
                continue
            _, ts, level, src, msg = m.groups()
            levels[level] += 1
            if level in ("WARNING", "ERROR", "CRITICAL"):
                normalised = re.sub(r"\d+", "#", msg)
                key = f"{level} [{src}] {normalised}"
                groups[key] += 1
                first.setdefault(key, ts)
                last[key] = ts
        out = [f"Level counts for {log_name}: " + ", ".join(f"{k}={v}" for k, v in sorted(levels.items()))]
        out.append("Most frequent warning/error patterns (count | first seen - last seen):")
        for key, n in groups.most_common(10):
            out.append(f"- {n}x | {first[key]} - {last[key]} | {key}")
        return "\n".join(out)

    # ------------------------------------------------------------------ server
    @tool
    def get_system_metrics() -> str:
        """Return CPU, memory, load, swap and DB-connection metrics: baseline, peaks and a 5-minute timeline."""
        data = _read_json("metrics.json")
        if not data:
            return "metrics.json missing. Run `python generate_sample_data.py`."
        base = data[:BASELINE_MINUTES]
        avg = lambda rows, k: round(sum(r[k] for r in rows) / len(rows), 1)
        peak = lambda k: max(data, key=lambda r: r[k])
        out = [
            f"Metrics window {data[0]['time']} - {data[-1]['time']} (1 sample/min).",
            f"Baseline (first {BASELINE_MINUTES} min, {base[0]['time']}-{base[-1]['time']}): cpu={avg(base, 'cpu_percent')}%, "
            f"mem={avg(base, 'memory_percent')}%, load={avg(base, 'load_1m')}, swap={avg(base, 'swap_percent')}%, "
            f"db_conns={avg(base, 'db_connections')}",
            f"Peak CPU {peak('cpu_percent')['cpu_percent']}% at {peak('cpu_percent')['time']}; "
            f"peak memory {peak('memory_percent')['memory_percent']}% at {peak('memory_percent')['time']}; "
            f"peak load {peak('load_1m')['load_1m']} at {peak('load_1m')['time']}; "
            f"peak swap {peak('swap_percent')['swap_percent']}%; peak DB connections {peak('db_connections')['db_connections']}",
            "Timeline (every 5 min): time | cpu% | mem% | load | swap% | db_conns",
        ]
        for r in data[::5]:
            out.append(f"{r['time']} | {r['cpu_percent']} | {r['memory_percent']} | {r['load_1m']} | {r['swap_percent']} | {r['db_connections']}")
        for key, label, limit in (("cpu_percent", "CPU", 90), ("memory_percent", "Memory", 95)):
            crossed = next((r for r in data if r[key] >= limit), None)
            if crossed:
                out.append(f"{label} first reached >={limit}% at {crossed['time']}.")
        return "\n".join(out)

    @tool
    def get_top_processes() -> str:
        """Return the top resource-consuming processes (snapshot near the end of the incident)."""
        data = _read_json("processes.json")
        if not data:
            return "processes.json missing."
        out = [f"Snapshot {data['snapshot_time']} | {data['cpu_cores']} cores, {data['total_memory_gb']}GB RAM"]
        for p in data["processes"]:
            out.append(f"pid {p['pid']} user={p['user']} cpu={p['cpu_percent']}% mem={p['memory_gb']}GB runtime={p['runtime']} cmd={p['command']}")
        return "\n".join(out)

    @tool
    def get_service_status() -> str:
        """Return the state of key services (app, nginx, postgresql, redis, cron, sshd)."""
        data = _read_json("services.json")
        if not data:
            return "services.json missing."
        return f"Snapshot {data['snapshot_time']}\n" + "\n".join(
            f"- {s['name']}: {s['state']} ({s['detail']})" for s in data["services"]
        )

    @tool
    def get_disk_usage() -> str:
        """Return disk usage per mount point."""
        data = _read_json("disk.json")
        if not data:
            return "disk.json missing."
        return "\n".join(f"{f['mount']}: {f['used_percent']}% of {f['size_gb']}GB" for f in data["filesystems"])

    @tool
    def get_recent_changes() -> str:
        """Return recent change records: deployments, config changes and scheduled-job edits (with diffs)."""
        data = _read_json("changes.json")
        if data is None:
            return "changes.json missing."
        if not data:
            return "No change records found for the last 7 days."
        out = []
        for c in data:
            out.append(f"- {c['time']} | {c['type']} | {c['summary']} | by {c['author']} | ticket {c.get('ticket') or 'none'}")
            for d in c.get("diff", []):
                out.append(f"    {d}")
        return "\n".join(out)

    @tool
    def get_network_connections() -> str:
        """Return established/listening network connections with the owning process (like `ss -tunp`)."""
        data = _read_json("network.json")
        if not data:
            return "network.json missing."
        out = [f"Snapshot {data['snapshot_time']}"]
        for c in data["connections"]:
            out.append(f"{c['state']:<11} {c['local']:<22} -> {c['remote']:<24} pid {c['pid']} ({c['process']})")
        return "\n".join(out)

    log_tools = [list_available_logs, read_log, search_logs, summarize_log_levels]
    return SimpleNamespace(
        LOG_TOOLS=log_tools,
        SERVER_TOOLS=[get_system_metrics, get_top_processes, get_service_status, get_disk_usage, get_recent_changes],
        ROOT_CAUSE_TOOLS=[search_logs, get_system_metrics, get_recent_changes],
        SECURITY_TOOLS=[read_log, search_logs, summarize_log_levels, get_top_processes,
                        get_network_connections, get_recent_changes],
    )
