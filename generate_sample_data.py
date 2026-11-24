"""
Generates a coherent, simulated incident for the prototype.

Story (the agent has to discover this on its own):
  * 08:57  someone edits the crontab -> the heavy `report_export.py --full` job
           now runs at 09:00 instead of 02:00.
  * 09:00  the job runs an unbounded query (no LIMIT) on ~2.4M rows.
  * 09:02+ CPU saturates, the DB connection pool (size 20) fills up.
  * 09:05+ "Database connection timeout" errors -> HTTP 500 responses.
  * 09:13  memory is exhausted, the kernel OOM-killer kills gunicorn workers.
  * 09:14+ app.service crashes and restart-loops, nginx returns 502/504.
  * Noise: unrelated warnings (TLS expiry, deprecation, /var/log nearly full,
           port scanner) so the agent has to separate signal from noise.

Run:  python generate_sample_data.py
"""
import json
import random
from pathlib import Path

OUT = Path(__file__).parent / "sample_data"
OUT.mkdir(exist_ok=True)
rnd = random.Random(42)
DAY = "2026-11-23"

logs = {"application": [], "syslog": [], "nginx": []}


def add(log, hms, level, src, msg):
    logs[log].append((hms, f"{DAY} {hms} {level:<8} [{src}] {msg}"))


def hms(minute_total, sec):
    return f"{minute_total // 60:02d}:{minute_total % 60:02d}:{sec:02d}"


def m(h, mi):
    return h * 60 + mi


START, END = m(8, 50), m(9, 30)
ENDPOINTS = ["/api/orders", "/api/products", "/api/customers", "/api/checkout", "/api/health"]

# ---------------------------------------------------------------- normal traffic
for t in range(START, END + 1):
    if t >= m(9, 15):  # app is down after this point
        continue
    n = rnd.randint(2, 4) if t < m(9, 5) else rnd.randint(1, 2)
    for _ in range(n):
        ep = rnd.choice(ENDPOINTS)
        if t < m(9, 5):
            ms = rnd.randint(25, 140) + (0 if t < m(9, 2) else rnd.randint(100, 900))
            add("application", hms(t, rnd.randint(0, 59)), "INFO", "http", f"GET {ep} 200 {ms}ms")

# ---------------------------------------------------------------- noise (unrelated)
add("application", "08:51:12", "WARNING", "app", "DeprecationWarning: pydantic v1 style validators will be removed in a future release")
add("application", "08:55:40", "WARNING", "tls", "TLS certificate for api.internal.example expires in 29 days")
add("application", "08:58:05", "WARNING", "cache", "Redis cache miss ratio 34% for key prefix 'product:'")
add("syslog", "08:52:00", "INFO", "systemd", "Started Daily apt download activities.")
add("syslog", "08:53:31", "WARNING", "rsyslog", "Disk usage on /var/log at 91% (consider log rotation)")
add("nginx", "08:54:10", "WARNING", "nginx", 'client sent invalid method while reading client request line, client: 203.0.113.9, request: "\\x16\\x03\\x01"')
add("nginx", "08:56:44", "WARNING", "nginx", 'client sent invalid method while reading client request line, client: 203.0.113.9, request: "\\x16\\x03\\x01"')

# ---------------------------------------------------------------- the trigger
add("syslog", "08:57:41", "INFO", "crontab", "(deploy) REPLACE (deploy)")
add("syslog", "08:57:42", "INFO", "crontab", "(deploy) END EDIT (deploy)")
add("syslog", "09:00:01", "INFO", "CRON", "(deploy) CMD (/opt/app/jobs/report_export.py --full)")
add("application", "09:00:03", "INFO", "scheduler", "Starting job report-export (pid 4821) triggered by cron")
add("application", "09:00:09", "INFO", "report-export",
    "Executing: SELECT * FROM orders JOIN order_items USING (order_id) JOIN customers USING (customer_id) -- no LIMIT, estimated rows=2400000")

# ---------------------------------------------------------------- degradation
pool_usage = {2: 11, 3: 14, 4: 17, 5: 19, 6: 20, 7: 20, 8: 20}
for t, used in pool_usage.items():
    ts = m(9, t)
    add("application", hms(ts, rnd.randint(1, 20)), "WARNING", "db.pool", f"Connection pool usage {used}/20")
    add("application", hms(ts, rnd.randint(21, 50)), "WARNING", "db.slow",
        f"Slow query detected ({rnd.randint(6, 28)}.{rnd.randint(0, 9)}s): SELECT * FROM orders WHERE customer_id = ?")

for t in range(m(9, 5), m(9, 15)):
    for _ in range(rnd.randint(2, 4) if t < m(9, 12) else rnd.randint(3, 6)):
        s = rnd.randint(0, 59)
        add("application", hms(t, s), "ERROR", "db.pool",
            "Database connection timeout after 30s (QueuePool limit of size 20 overflow 0 reached)")
        ep = rnd.choice(["/api/orders", "/api/checkout", "/api/customers"])
        add("application", hms(t, min(59, s + 1)), "ERROR", "http", f"{'GET' if ep != '/api/checkout' else 'POST'} {ep} 500 30012ms")
    if t in (m(9, 9), m(9, 11)):
        add("application", hms(t, 30), "WARNING", "db.pool", "Retry limit exceeded (3/3) for request, giving up")

for t in range(m(9, 6), m(9, 15)):
    for _ in range(rnd.randint(1, 3)):
        add("nginx", hms(t, rnd.randint(0, 59)), "ERROR", "nginx",
            f'upstream timed out (110: Connection timed out) while reading response header from upstream, client: 10.0.4.{rnd.randint(10, 60)}, request: "GET /api/orders HTTP/1.1"')

# ---------------------------------------------------------------- memory exhaustion + crash
add("syslog", "09:12:40", "WARNING", "kernel", "Memory cgroup stats: usage 15.6GB of 16GB, swap 6.4GB of 8GB in use")
add("syslog", "09:13:58", "CRITICAL", "kernel", "Out of memory: Killed process 4902 (gunicorn) total-vm:3412880kB, anon-rss:1790220kB")
add("syslog", "09:14:19", "CRITICAL", "kernel", "Out of memory: Killed process 5011 (gunicorn) total-vm:3398112kB, anon-rss:1765004kB")
add("application", "09:13:59", "CRITICAL", "worker", "Worker pid 4902 exited unexpectedly (signal 9)")
add("application", "09:14:20", "CRITICAL", "worker", "Worker pid 5011 exited unexpectedly (signal 9)")
add("syslog", "09:14:21", "ERROR", "systemd", "app.service: Main process exited, code=killed, status=9/KILL")
add("syslog", "09:14:21", "ERROR", "systemd", "app.service: Failed with result 'signal'.")

for i, (mi, sec) in enumerate([(14, 31), (14, 58), (15, 24), (15, 50), (16, 17)], start=1):
    add("syslog", hms(m(9, mi), sec), "INFO", "systemd", f"app.service: Scheduled restart job, restart counter is at {i}.")
    add("application", hms(m(9, mi), sec + 3 if sec < 56 else 59), "ERROR", "app",
        "Failed to start application: [Errno 12] Cannot allocate memory" if i % 2 else "Failed to start application: database unavailable (connection pool could not be initialised)")
add("syslog", "09:16:40", "ERROR", "systemd", "app.service: Start request repeated too quickly.")
add("syslog", "09:16:40", "ERROR", "systemd", "Failed to start Application Service.")

for t in range(m(9, 15), m(9, 22)):
    add("application", hms(t, rnd.randint(0, 59)), "ERROR", "healthcheck", "Health check failed: /api/health unreachable")
    for _ in range(rnd.randint(1, 3)):
        add("nginx", hms(t, rnd.randint(0, 59)), "ERROR", "nginx",
            f'connect() failed (111: Connection refused) while connecting to upstream, client: 10.0.4.{rnd.randint(10, 60)}, request: "GET /api/orders HTTP/1.1"')
add("application", "09:21:30", "INFO", "report-export", "Job report-export (pid 4821) still running: 1.9M of 2.4M rows fetched")

# ---------------------------------------------------------------- write logs
for name, fname in [("application", "application.log"), ("syslog", "syslog.log"), ("nginx", "nginx_error.log")]:
    lines = sorted(logs[name], key=lambda x: x[0])
    (OUT / fname).write_text("\n".join(l for _, l in lines) + "\n")
    print(f"{fname}: {len(lines)} lines")

# ---------------------------------------------------------------- metrics (1/min)
metrics = []
for t in range(START, END + 1):
    since = t - m(9, 0)
    if since < 0:
        cpu, mem, load, swap, conns = rnd.uniform(20, 30), rnd.uniform(50, 54), rnd.uniform(0.6, 1.1), rnd.uniform(1, 3), rnd.randint(6, 10)
    else:
        cpu = min(98, 30 + since * 22) if since < 3 else rnd.uniform(88, 98)
        mem = min(97.5, 52 + since * 3.4) if since <= 13 else rnd.uniform(70, 78)
        load = min(14.0, 1 + since * 1.1) if since < 12 else rnd.uniform(11, 14)
        swap = min(82, 3 + since * 6) if since <= 13 else rnd.uniform(70, 80)
        conns = min(20, 8 + since * 2) if since < 15 else rnd.randint(2, 6)
    metrics.append({"time": f"{t // 60:02d}:{t % 60:02d}", "cpu_percent": round(cpu, 1), "memory_percent": round(mem, 1),
                    "load_1m": round(load, 2), "swap_percent": round(swap, 1), "db_connections": int(conns)})
(OUT / "metrics.json").write_text(json.dumps(metrics, indent=2))

(OUT / "processes.json").write_text(json.dumps({
    "snapshot_time": "09:20", "cpu_cores": 8, "total_memory_gb": 16,
    "processes": [
        {"pid": 4821, "user": "deploy", "command": "python /opt/app/jobs/report_export.py --full", "cpu_percent": 94.2, "memory_gb": 9.8, "runtime": "20m"},
        {"pid": 1377, "user": "postgres", "command": "postgres: app_db app_user [local] SELECT", "cpu_percent": 71.5, "memory_gb": 2.1, "runtime": "3d"},
        {"pid": 1380, "user": "postgres", "command": "postgres: app_db app_user [local] SELECT", "cpu_percent": 63.0, "memory_gb": 1.9, "runtime": "3d"},
        {"pid": 912, "user": "redis", "command": "redis-server *:6379", "cpu_percent": 1.2, "memory_gb": 0.4, "runtime": "3d"},
        {"pid": 880, "user": "www-data", "command": "nginx: worker process", "cpu_percent": 0.8, "memory_gb": 0.1, "runtime": "3d"},
    ]}, indent=2))

(OUT / "services.json").write_text(json.dumps({
    "snapshot_time": "09:20",
    "services": [
        {"name": "app.service", "state": "failed", "detail": "restart loop, 5 restarts, start request repeated too quickly"},
        {"name": "nginx.service", "state": "active", "detail": "running, upstream (app) unreachable"},
        {"name": "postgresql.service", "state": "active", "detail": "running, 20/20 connections used, long-running SELECTs"},
        {"name": "redis-server.service", "state": "active", "detail": "running normally"},
        {"name": "cron.service", "state": "active", "detail": "running"},
    ]}, indent=2))

(OUT / "disk.json").write_text(json.dumps({
    "filesystems": [
        {"mount": "/", "used_percent": 62, "size_gb": 100},
        {"mount": "/var/log", "used_percent": 91, "size_gb": 20},
        {"mount": "/var/lib/postgresql", "used_percent": 55, "size_gb": 200},
    ]}, indent=2))
print("sample_data generated in", OUT)
