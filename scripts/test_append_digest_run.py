"""One-off test for update_state's append_digest_run + unknown-key guard.

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

update_state = getattr(mcp_server.update_state, "fn", mcp_server.update_state)

now = datetime.now(timezone.utc)
iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")

# Seed: one run inside the 10-day window, one far outside it.
seed = {
    "alerted": [],
    "queued_for_digest": [{"id": "art_old"}],
    "alerts_today": {"date": "", "count": 0},
    "digest_runs": [
        {"ts": iso(now - timedelta(days=12)), "topics": [{"slug": "ancient"}]},
        {"ts": iso(now - timedelta(days=2)), "topics": [{"slug": "recent"}]},
    ],
}
(tmp / "state.json").write_text(json.dumps(seed), encoding="utf-8")

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)


# 1. The real ШАГ 9.3 call: clear_queued + last_digest_run + append_digest_run.
#    digest_runs is filtered out of the return (callers never need it back) —
#    the appended/trimmed array is verified by reading state.json from disk.
new_run = {"ts": iso(now), "run": "digest", "topics": [{"slug": "fresh"}]}
res = update_state({
    "clear_queued": True,
    "last_digest_run": iso(now),
    "append_digest_run": new_run,
})
check("append: digest_runs filtered from return", "digest_runs" not in res)
check("append: digest_runs_count returned", res.get("digest_runs_count") == 2)
runs = json.loads((tmp / "state.json").read_text(encoding="utf-8"))["digest_runs"]
check("append: new run persisted to disk", any(r.get("run") == "digest" for r in runs))
check("append: 12-day-old run trimmed", all(r["topics"][0]["slug"] != "ancient" for r in runs))
check("append: 2-day-old run kept", any(r["topics"][0]["slug"] == "recent" for r in runs))
check("append: queue cleared", res["queued_for_digest"] == [])
check("append: last_digest_run merged", res["last_digest_run"] == iso(now))

# 2. Unknown append-style keys are rejected and nothing is written.
before = (tmp / "state.json").read_text(encoding="utf-8")
for bad_key in ("append_digest_runs", "digest_runs_append", "notes_append",
                "append_notes", "clear_alerted", "increment_counter"):
    res = update_state({bad_key: ["x"], "last_digest_run": "POISON"})
    ok = "error" in res and (tmp / "state.json").read_text(encoding="utf-8") == before
    check(f"guard: {bad_key} rejected without write", ok)

# 3. Watchman-style patch still works end to end.
res = update_state({
    "append_alerted": [{"topic_key": "t1", "ts": iso(now)}],
    "append_queued": [{"id": "art_q1"}],
    "increment_alerts_today": 1,
    "last_watchman_run": iso(now),
})
check("watchman: alerted appended", len(res["alerted"]) == 1)
check("watchman: queued appended", res["queued_for_digest"] == [{"id": "art_q1"}])
check("watchman: counter", res["alerts_today"]["count"] == 1)
check("watchman: digest_runs filtered from return", "digest_runs" not in res)

# 4. A run record without ts gets stamped server-side (not silently dropped).
update_state({"append_digest_run": {"run": "digest", "topics": []}})
on_disk_runs = json.loads((tmp / "state.json").read_text(encoding="utf-8"))["digest_runs"]
stamped = [r for r in on_disk_runs if r.get("run") == "digest" and not r.get("topics")]
check("append: missing ts stamped", len(stamped) == 1 and stamped[0].get("ts", "") >= iso(now - timedelta(minutes=1)))

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILURE(S): {failures}")
sys.exit(1 if failures else 0)
