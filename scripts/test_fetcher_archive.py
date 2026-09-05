"""fetcher.py: articles that fall past the 72h horizon must land in
archive.jsonl instead of silently vanishing (broken until 05.09.2026 —
the rollout ran on the already-filtered inbox and archived 0 every time).

Runs main() against a throwaway --root with a comment-only sources.txt, so
nothing is fetched from the network.

    D:\\newDay\\.venv\\Scripts\\python.exe scripts\\test_fetcher_archive.py
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, r"D:\newDay")
import fetcher  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="newday_fetcher_test_"))
now = datetime.now(timezone.utc)
iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")  # noqa: E731


def art(i, hours_ago):
    return {"id": f"art_{i:08x}", "source": "TASS", "title": f"Story {i}",
            "summary": f"Summary {i}", "url": f"https://example.org/{i}",
            "published": iso(now - timedelta(hours=hours_ago))}


fresh, stale, older = art(1, 1), art(2, 100), art(3, 200)
(tmp / "sources.txt").write_text("# no sources in the test\n", encoding="utf-8")
(tmp / "inbox.json").write_text(json.dumps({
    "articles": [fresh, stale, older, {"id": "broken", "title": "no fields"}]}),
    encoding="utf-8")

# unit: split_by_horizon
cutoff = now - timedelta(hours=fetcher.INBOX_HORIZON_HOURS)
kept, expired = fetcher.split_by_horizon(json.loads((tmp / "inbox.json").read_text("utf-8"))["articles"], cutoff)
assert [a.id for a in kept] == [fresh["id"]], kept
assert expired == [stale, older], expired          # raw dicts, in inbox order

# end-to-end: first run archives the two expired ones
sys.argv = ["fetcher", "--root", str(tmp)]
assert fetcher.main() == 0
archive = tmp / "archive.jsonl"
assert archive.exists(), "archive.jsonl was not written"
lines = [json.loads(l) for l in archive.read_text("utf-8").splitlines() if l.strip()]
assert [a["id"] for a in lines] == [stale["id"], older["id"]], lines
assert lines[0] == stale, "archive must keep the raw article dict"
inbox = json.loads((tmp / "inbox.json").read_text("utf-8"))["articles"]
assert [a["id"] for a in inbox] == [fresh["id"]], inbox

# second run: nothing new expired → archive unchanged, no duplicate lines
assert fetcher.main() == 0
assert archive.read_text("utf-8").count("\n") == 2, archive.read_text("utf-8")

# no expired at all → archive file is not even created
tmp2 = Path(tempfile.mkdtemp(prefix="newday_fetcher_test2_"))
(tmp2 / "sources.txt").write_text("#\n", encoding="utf-8")
(tmp2 / "inbox.json").write_text(json.dumps({"articles": [fresh]}), encoding="utf-8")
sys.argv = ["fetcher", "--root", str(tmp2)]
assert fetcher.main() == 0
assert not (tmp2 / "archive.jsonl").exists()

print("OK test_fetcher_archive")
