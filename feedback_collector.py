"""
Feedback collector for the news-agent.

Polls the linked discussion group for replies (user comments under
auto-forwarded channel posts and under the bot's methodology comments),
matches them to the originating alert or digest via message_thread_id,
and appends to feedback_log.json.

Runs as a scheduled task every ~15 minutes. Zero LLM, zero billing.

Design notes:
  - getUpdates is called WITHOUT offset so we don't break
    mcp_server.send_telegram_comment, which also polls getUpdates without
    offset to find the auto-forward of channel posts. We track our own
    last_seen_update_id locally for deduplication.
  - Telegram retains updates 24h; 15-min polling guarantees we never lose
    feedback within that window.
  - Cross-reference: incoming reply has message_thread_id pointing at the
    discussion group's auto-forward copy. We match that against
    state.alerted[*].discussion_msg_id and state.digest_runs[*].discussion_msg_id.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(os.environ.get("NEWS_ROOT", Path(__file__).parent)).resolve()
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
FEEDBACK_LOG_PATH = ROOT / "feedback_log.json"
LAST_UPDATE_ID_PATH = ROOT / ".feedback_last_update_id"
DISCUSSION_ID_CACHE_PATH = ROOT / ".feedback_discussion_id"
BOT_ID_CACHE_PATH = ROOT / ".feedback_bot_id"

GETUPDATES_LIMIT = 100
GETUPDATES_TIMEOUT_S = 1

# Telegram's system account id used as `from` for messages posted by anonymous
# group admins. Such messages carry is_bot=True but are real human feedback —
# their sender_chat points at the discussion group, not the channel.
GROUP_ANONYMOUS_BOT_ID = 1087968824


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _log(f"WARN: could not read {path.name}: {e}; using default")
        return default


def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _resolve_discussion_id(token: str, channel_chat_id: str) -> int | None:
    """Get linked discussion group id, cached on disk."""
    if DISCUSSION_ID_CACHE_PATH.exists():
        try:
            return int(DISCUSSION_ID_CACHE_PATH.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pass

    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getChat",
            params={"chat_id": channel_chat_id},
            timeout=20,
        )
        info = r.json()
    except (requests.RequestException, ValueError) as e:
        _log(f"ERROR: getChat failed: {e}")
        return None

    if not info.get("ok"):
        _log(f"ERROR: getChat returned not ok: {info}")
        return None

    linked = info["result"].get("linked_chat_id")
    if not linked:
        _log(f"ERROR: channel {channel_chat_id} has no linked discussion group")
        return None

    DISCUSSION_ID_CACHE_PATH.write_text(str(linked), encoding="utf-8")
    _log(f"resolved discussion_chat_id={linked} (cached)")
    return int(linked)


def _resolve_bot_id(token: str) -> int | None:
    """Get bot's own user id, cached on disk. Used to ignore bot's own messages."""
    if BOT_ID_CACHE_PATH.exists():
        try:
            return int(BOT_ID_CACHE_PATH.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pass

    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=20)
        info = r.json()
    except (requests.RequestException, ValueError) as e:
        _log(f"ERROR: getMe failed: {e}")
        return None

    if not info.get("ok"):
        _log(f"ERROR: getMe returned not ok: {info}")
        return None

    bot_id = int(info["result"]["id"])
    BOT_ID_CACHE_PATH.write_text(str(bot_id), encoding="utf-8")
    _log(f"resolved bot_id={bot_id} (cached)")
    return bot_id


def _get_updates(token: str) -> list[dict]:
    """Pull updates without advancing offset (cooperative with mcp_server)."""
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={
                "limit": GETUPDATES_LIMIT,
                "timeout": GETUPDATES_TIMEOUT_S,
                "allowed_updates": json.dumps(["message"]),
            },
            timeout=10,
        )
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        _log(f"ERROR: getUpdates failed: {e}")
        return []

    if not data.get("ok"):
        _log(f"ERROR: getUpdates returned not ok: {data}")
        return []

    return data.get("result", [])


def _match_thread_to_state(
    thread_id: int,
    channel_post_id: int | None,
    state: dict,
) -> dict:
    """
    Attribute a reply to the alert/digest it belongs to.

    Two matching strategies, tried in order:
      1. thread_id (the discussion thread root) == stored discussion_msg_id.
      2. channel_post_id (origin of the auto-forward the user replied to)
         == stored channel_msg_id. This recovers attribution even for runs
         that never persisted discussion_msg_id.

    Returns a context dict with kind = 'alert' | 'digest' | 'unknown'.
    """
    def _alert_ctx(a: dict) -> dict:
        return {
            "kind": "alert",
            "topic_key": a.get("topic_key"),
            "headline": a.get("headline"),
            "article_ids": a.get("article_ids", []),
            "channel_msg_id": a.get("channel_msg_id"),
            "discussion_msg_id": thread_id,
            "alert_ts": a.get("ts"),
        }

    def _digest_ctx(run: dict) -> dict:
        return {
            "kind": "digest",
            "digest_ts": run.get("ts"),
            "page_url": run.get("page_url") or run.get("url"),
            "channel_msg_id": run.get("main_msg_id") or run.get("channel_msg_id"),
            "discussion_msg_id": thread_id,
            "topics": [t.get("slug") for t in run.get("topics", []) if isinstance(t, dict)],
        }

    # 1. direct match on the stored discussion auto-forward id
    for a in state.get("alerted", []):
        if a.get("discussion_msg_id") is not None and a["discussion_msg_id"] == thread_id:
            return _alert_ctx(a)
    for run in state.get("digest_runs", []):
        if run.get("discussion_msg_id") is not None and run["discussion_msg_id"] == thread_id:
            return _digest_ctx(run)

    # 2. fallback via the channel post the auto-forward originated from
    if channel_post_id is not None:
        for a in state.get("alerted", []):
            if a.get("channel_msg_id") == channel_post_id:
                return _alert_ctx(a)
        for run in state.get("digest_runs", []):
            if (run.get("main_msg_id") or run.get("channel_msg_id")) == channel_post_id:
                return _digest_ctx(run)

    return {
        "kind": "unknown",
        "discussion_msg_id": thread_id,
        "channel_post_id": channel_post_id,
    }


def _process(
    updates: list[dict],
    discussion_id: int,
    channel_id: int | None,
    bot_id: int,
    state: dict,
    last_seen_update_id: int,
) -> tuple[list[dict], int]:
    """
    Filter updates → feedback entries.

    Returns (new_entries, new_last_seen_update_id).
    """
    new_entries: list[dict] = []
    max_seen = last_seen_update_id

    for upd in updates:
        update_id = upd.get("update_id", 0)
        if update_id <= last_seen_update_id:
            continue
        max_seen = max(max_seen, update_id)

        msg = upd.get("message")
        if not msg:
            continue

        if msg.get("chat", {}).get("id") != discussion_id:
            continue

        # auto-forwards of channel posts aren't user feedback
        if msg.get("is_automatic_forward"):
            continue

        from_user = msg.get("from", {})
        from_id = from_user.get("id")
        sender_chat_id = (msg.get("sender_chat") or {}).get("id")

        # the news bot's own messages (methodology comments etc.)
        if from_id == bot_id:
            continue
        # anything spoken "as the channel" is our own voice, not feedback
        if channel_id is not None and sender_chat_id == channel_id:
            continue
        # real bots are noise — EXCEPT anonymous group admins, who post via
        # GroupAnonymousBot (is_bot=True, sender_chat=the group) and are humans
        # leaving feedback. Keep those.
        if from_user.get("is_bot") and from_id != GROUP_ANONYMOUS_BOT_ID:
            continue

        thread_id = msg.get("message_thread_id")
        reply_to = msg.get("reply_to_message")
        if thread_id is None and reply_to:
            thread_id = reply_to.get("message_id")

        if thread_id is None:
            # plain message in the group, no thread anchor → can't attribute
            continue

        feedback_text = msg.get("text") or msg.get("caption") or ""
        if not feedback_text.strip():
            continue

        # if the reply targets an auto-forwarded channel post, recover the
        # original channel message id from the forward origin so we can
        # attribute even when discussion_msg_id was never persisted to state.
        channel_post_id = None
        if reply_to and channel_id is not None:
            fwd = reply_to.get("forward_origin") or {}
            fwd_chat_id = (fwd.get("chat") or {}).get("id")
            if fwd_chat_id is None:
                fwd_chat_id = (reply_to.get("forward_from_chat") or {}).get("id")
            if fwd_chat_id == channel_id:
                channel_post_id = fwd.get("message_id") or reply_to.get("forward_from_message_id")

        context = _match_thread_to_state(thread_id, channel_post_id, state)

        entry = {
            "ts": datetime.fromtimestamp(
                msg.get("date", time.time()), tz=timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "update_id": update_id,
            "from_user": {
                "id": from_user.get("id"),
                "username": from_user.get("username"),
                "first_name": from_user.get("first_name"),
            },
            "feedback_text": feedback_text,
            "context": context,
            "reply_to_text": (reply_to or {}).get("text"),
        }
        new_entries.append(entry)

    return new_entries, max_seen


def main() -> int:
    cfg = _load_json(CONFIG_PATH, None)
    if not cfg:
        _log(f"FATAL: cannot load {CONFIG_PATH}")
        return 0

    token = cfg.get("tg_bot_token")
    channel_chat_id = cfg.get("tg_chat_id")
    if not token or not channel_chat_id:
        _log("FATAL: tg_bot_token or tg_chat_id missing in config.json")
        return 0

    discussion_id = _resolve_discussion_id(token, channel_chat_id)
    if discussion_id is None:
        return 0

    bot_id = _resolve_bot_id(token)
    if bot_id is None:
        return 0

    state = _load_json(STATE_PATH, {})
    feedback_log = _load_json(FEEDBACK_LOG_PATH, [])
    if not isinstance(feedback_log, list):
        _log(f"WARN: feedback_log is not a list, resetting")
        feedback_log = []

    last_seen_raw = LAST_UPDATE_ID_PATH.read_text(encoding="utf-8").strip() \
        if LAST_UPDATE_ID_PATH.exists() else "0"
    try:
        last_seen_update_id = int(last_seen_raw)
    except ValueError:
        last_seen_update_id = 0

    updates = _get_updates(token)
    _log(f"got {len(updates)} updates from getUpdates")

    # Bootstrap: on the very first run skip the existing backlog so we don't
    # ingest months of unrelated history. We mark every visible update as seen
    # but produce no entries.
    if last_seen_update_id == 0 and updates:
        max_id = max(u.get("update_id", 0) for u in updates)
        LAST_UPDATE_ID_PATH.write_text(str(max_id), encoding="utf-8")
        _log(f"bootstrap: skipped {len(updates)} existing updates, last_seen={max_id}")
        return 0

    try:
        channel_id = int(channel_chat_id)
    except (TypeError, ValueError):
        channel_id = None

    new_entries, new_last_seen = _process(
        updates, discussion_id, channel_id, bot_id, state, last_seen_update_id
    )

    if not new_entries:
        if new_last_seen > last_seen_update_id:
            LAST_UPDATE_ID_PATH.write_text(str(new_last_seen), encoding="utf-8")
        _log(f"no new feedback entries; last_seen={new_last_seen}")
        return 0

    feedback_log.extend(new_entries)
    _atomic_write_json(FEEDBACK_LOG_PATH, feedback_log)
    LAST_UPDATE_ID_PATH.write_text(str(new_last_seen), encoding="utf-8")

    _log(f"appended {len(new_entries)} new feedback entries; last_seen={new_last_seen}")
    for e in new_entries:
        kind = e["context"]["kind"]
        preview = e["feedback_text"][:60].replace("\n", " ")
        _log(f"  [{kind}] @{e['from_user'].get('username') or e['from_user'].get('id')}: {preview!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
