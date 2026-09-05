"""One-off test for get_state's include_digest_runs parameter.

Runs against a throwaway NEWS_ROOT (set by the caller BEFORE import) so the
real state.json is never touched. Exercises the raw functions behind the
FastMCP tool objects.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="newday_test_"))
os.environ["NEWS_ROOT"] = str(tmp)

sys.path.insert(0, r"D:\newDay")
import mcp_server  # noqa: E402  (must come after NEWS_ROOT is set)

assert mcp_server.ROOT == tmp, f"NEWS_ROOT not honored: {mcp_server.ROOT}"

get_state = getattr(mcp_server.get_state, "fn", mcp_server.get_state)
update_state = getattr(mcp_server.update_state, "fn", mcp_server.update_state)

now = datetime.now(timezone.utc)
iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")

seed = {
    "alerted": [{"topic_key": "t1", "ts": iso(now - timedelta(hours=3))}],
    "queued_for_digest": [{"id": "art_q1"}],
    "last_watchman_run": iso(now - timedelta(hours=1)),
    "last_digest_run": iso(now - timedelta(days=1)),
    "alerts_today": {"date": now.date().isoformat(), "count": 2},
    "digest_runs": [
        {"ts": iso(now - timedelta(days=2)), "topics": [{"slug": "recent"}]},
    ],
}
(tmp / "state.json").write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)


# 1. Default (the hourly watchman call): digest_runs omitted, the rest intact.
res = get_state()
check("default: digest_runs omitted", "digest_runs" not in res)
check("default: alerted intact", res["alerted"] == seed["alerted"])
check("default: queued intact", res["queued_for_digest"] == seed["queued_for_digest"])
check("default: alerts_today intact", res["alerts_today"] == seed["alerts_today"])
check("default: last_watchman_run intact", res["last_watchman_run"] == seed["last_watchman_run"])

# 2. The digest ШАГ 1 call: include_digest_runs=true returns it verbatim.
res = get_state(include_digest_runs=True)
check("include: digest_runs present", res.get("digest_runs") == seed["digest_runs"])
check("include: rest intact too", res["queued_for_digest"] == seed["queued_for_digest"])

# 3. Omission is read-side only — disk still has digest_runs after a default read.
on_disk = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
check("disk: digest_runs untouched by default read", on_disk["digest_runs"] == seed["digest_runs"])

# 4. A watchman-style update_state must not lose digest_runs on disk
#    (guards against someone moving the filtering into _load_state).
update_state({"last_watchman_run": iso(now), "increment_alerts_today": 1})
on_disk = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
check("update_state: digest_runs survives write", on_disk["digest_runs"] == seed["digest_runs"])

# 5. Missing state.json → fresh default state, no crash with either flag.
(tmp / "state.json").unlink()
check("fresh: default has no digest_runs", "digest_runs" not in get_state())
fresh = get_state(include_digest_runs=True)
check("fresh: include=true works without the key", "digest_runs" not in fresh and "alerted" in fresh)

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILURE(S): {failures}")
sys.exit(1 if failures else 0)
