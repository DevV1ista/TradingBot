"""Watchdog: restart the bot chain if it is not running. Safe to call as often as you like.

Called by `job=watchdog` (trading signals workflow), e.g. from an external free cron service
or a GitHub schedule. It never touches a running chain:
  - a bot-chain run (start/continue) is queued or running      -> nothing
  - the last chain run ended less than 10 minutes ago          -> nothing (hand-over in progress)
  - the last chain run was cancelled (you stopped the bot)     -> nothing
  - otherwise (chain died / never started / hand-over failed)  -> dispatch job=continue + Telegram
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ACTIVE = ("queued", "in_progress", "waiting", "requested", "pending")


def is_chain(run):
    t = (run.get("display_title") or "").strip()
    # runs created before 2026-11-20 evening have no job in the title: count them as chain runs
    return "(start)" in t or "(continue)" in t or t == "trading signals"


def decide(runs, me, now):
    """returns 'nothing:<reason>' or 'restart:<reason>'"""
    chain = [r for r in runs if r["id"] != me and is_chain(r)]
    if any(r["status"] in ACTIVE for r in chain):
        return "nothing:chain running"
    if not chain:
        return "restart:no chain run found"
    last = max(chain, key=lambda r: r["updated_at"])
    ended = datetime.fromisoformat(last["updated_at"].replace("Z", "+00:00"))
    if now - ended < timedelta(minutes=10):
        return "nothing:hand-over in progress"
    if last["conclusion"] == "cancelled":
        return "nothing:stopped by user"
    return f"restart:last chain run ended {int((now - ended).total_seconds() // 60)} min ago ({last['conclusion']})"


def gh(path, body=None):
    req = urllib.request.Request(f"https://api.github.com/repos/{os.environ['REPOSITORY']}{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    req.add_header("Authorization", f"Bearer {os.environ['GH_TOKEN']}")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"{}")


def telegram(text):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=20)


def main():
    runs = gh("/actions/workflows/signals.yml/runs?per_page=30")["workflow_runs"]
    verdict = decide(runs, int(os.environ.get("RUN_ID", "0")), datetime.now(timezone.utc))
    print("watchdog:", verdict)
    if verdict.startswith("restart"):
        gh("/actions/workflows/signals.yml/dispatches", {"ref": "main", "inputs": {"job": "continue"}})
        telegram(f"Trading-Bot Wächter: Bot lief nicht ({verdict.split(':', 1)[1]}) – automatisch neu gestartet.")


if __name__ == "__main__":
    main()
