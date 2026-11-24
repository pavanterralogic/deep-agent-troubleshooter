"""The simulated incidents the agent can investigate. Data lives in sample_data/<key>/."""
from pathlib import Path

SAMPLE_ROOT = Path(__file__).parent / "sample_data"

SCENARIOS = {
    "runaway_job": {
        "title": "Runaway background job (high CPU, services failing)",
        "task": (
            "My Linux server is not responding. CPU usage is high, some services are failing, and I am "
            "seeing errors in the application logs. Investigate the issue and provide possible solutions."
        ),
    },
    "bad_deployment": {
        "title": "Bad deployment (errors right after a release)",
        "task": (
            "Everything broke right after this morning's release. API requests are timing out and users "
            "are getting errors, but the server does not look overloaded. Investigate and provide solutions."
        ),
    },
    "security_incident": {
        "title": "Security incident (CPU at 100% at night, strange logins)",
        "task": (
            "CPU is stuck near 100% since the middle of the night, the application is very slow, and I "
            "noticed some strange login activity. Investigate what is happening and how to fix it."
        ),
    },
}
DEFAULT_SCENARIO = "runaway_job"


def scenario_dir(key: str) -> Path:
    if key not in SCENARIOS:
        raise ValueError(f"Unknown scenario '{key}'. Choose one of: {', '.join(SCENARIOS)}")
    return SAMPLE_ROOT / key
