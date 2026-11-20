"""GitHub Actions supervisor: time-limited runs that hand over to the next run.

No cron, daemon, external scheduler or machine outside GitHub required.
The original trading strategy remains in watcher.Bot, unchanged.
"""
import argparse
import os
import subprocess
import time
import traceback
from datetime import datetime

import requests

from watcher import Bot, CH, Clock, LiveData, NY, SEGMENTS, telegram

MAX_CONSECUTIVE_ERRORS = 10
CHECKPOINT_SECONDS = 300
POLL_SECONDS = 30


def segment_at(now):
    """Return the active NY trading segment (1-3), or None outside trading hours."""
    now = now.astimezone(NY)
    if now.weekday() >= 5:
        return None
    hm = (now.hour, now.minute)
    for number, (start, end) in SEGMENTS.items():
        if start <= hm < end:
            return number
    return None


def telegram_status(message):
    """System status must fail loudly rather than silently ignoring Telegram errors."""
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Missing Telegram secrets")
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": message},
                timeout=15,
            )
            if r.ok:
                print("Telegram status delivered", flush=True)
                return
            print(f"Telegram returned HTTP {r.status_code}", flush=True)
        except requests.RequestException as exc:
            print(f"Telegram transport error: {type(exc).__name__}", flush=True)
        if attempt < 2:
            time.sleep(2)
    raise RuntimeError("Could not deliver Telegram status")


def checkpoint():
    """Persist trading state before handing off; never continue if persistence fails."""
    subprocess.run(["git", "add", "--", "state"], check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
    if staged.returncode == 0:
        return
    if staged.returncode != 1:
        raise RuntimeError("git diff failed")
    subprocess.run(["git", "commit", "-m", f"state {datetime.now(tz=NY):%Y-%m-%d_%H%M%S}"], check=True)
    for attempt in range(3):
        pull = subprocess.run(["git", "pull", "--rebase", "--quiet"], check=False)
        if pull.returncode == 0:
            push = subprocess.run(["git", "push", "--quiet"], check=False)
            if push.returncode == 0:
                print("Checkpoint pushed", flush=True)
                return
        if attempt < 2:
            time.sleep(5)
    raise RuntimeError("Could not persist state to GitHub; chain stopped to avoid duplicate signals")


def run_service(seconds, initial=False):
    if not 1 <= seconds <= 18000:
        raise ValueError("Run window must be 1..18000 seconds")
    start = time.monotonic()
    deadline = start + seconds
    clock = Clock()
    bot = None
    key = None
    errors = 0
    next_checkpoint = start + CHECKPOINT_SECONDS

    if initial:
        now_ch = datetime.now(tz=CH)
        market = "Handelszeit" if segment_at(now_ch) else "Wartet auf Handelszeit"
        telegram_status(f"Trading-Bot gestartet ({now_ch:%d.%m.%Y %H:%M} CH). Status: {market}. GitHub-Autostart aktiv.")
    try:
        while time.monotonic() < deadline:
            now = datetime.now(tz=NY)
            segment = segment_at(now)
            new_key = (now.date(), segment) if segment is not None else None

            if new_key != key:
                if bot is not None:
                    bot.save()
                bot = None
                key = new_key
                errors = 0
                if key is not None:
                    bot = Bot(LiveData(clock), clock, telegram, now.date())
                    if "bot_online" not in bot.s["sent"]:
                        telegram_status(f"Trading-Bot kontrolliert den Handelstag {now:%d.%m.%Y} (New York). Marktstatus und Signale werden geprueft.")
                        bot.s["sent"].append("bot_online")
                        bot.save()
                    print(f"Monitoring NY day {now.date()} / segment {segment}", flush=True)
                else:
                    print(f"Outside NY trading segments: {now:%Y-%m-%d %H:%M}", flush=True)

            if bot is not None:
                try:
                    bot.tick()
                    errors = 0
                except Exception:
                    errors += 1
                    traceback.print_exc()
                    if errors >= MAX_CONSECUTIVE_ERRORS:
                        raise RuntimeError("Trading watcher failed 10 consecutive checks")

            if time.monotonic() >= next_checkpoint:
                if bot is not None:
                    bot.save()
                checkpoint()
                next_checkpoint = time.monotonic() + CHECKPOINT_SECONDS

            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(POLL_SECONDS, remaining))
    finally:
        if bot is not None:
            bot.save()
        checkpoint()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=14400)
    parser.add_argument("--initial", action="store_true")
    args = parser.parse_args()
    run_service(args.seconds, args.initial)
