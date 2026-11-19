"""Free cloud signal bot for Lucid (micro futures), run by GitHub Actions. Sends Telegram signals.

  python signal_bot.py gold         London breakout on gold  (acts at 01:00 New York)
  python signal_bot.py nas          ORB5 on Nasdaq           (acts at 09:35 New York)
  python signal_bot.py nas --test 2026-11-18   replay a past day (Yahoo keeps ~30 days of 1-min data)
  python signal_bot.py ping         send a test message

Same rules as the backtest (TradingLab/lab/strategies.py):
  gold: range 20:00-01:00 New York on GC futures (= MGC prices); skip if range < 0.3 % or > 1.6 %;
        buy-stop at the high / sell-stop at the low (OCO); stop = other side capped at 0.6 % of price;
        target 4R; close everything at 14:00 New York
  nas : direction of the first 5-minute candle 09:30-09:35 (QQQ, real-time on Yahoo);
        stop = 0.25 x ATR(14) of daily cash-session bars, converted to NQ points; target 3R;
        close at 15:59 New York
Sizing: whole micro contracts. MNQ = $2 per point, MGC = $10 per $1. A trade is skipped when even
one micro would risk more than RISK_USD.

Environment: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, RISK_USD (default 400), MAX_MICROS (default 10)
"""
import argparse
import math
import os
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

NY = ZoneInfo("America/New_York")
CH = ZoneInfo("Europe/Zurich")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
RISK = float(os.getenv("RISK_USD") or "400")
MAX_MICROS = int(os.getenv("MAX_MICROS") or "10")


# ----------------------------------------------------------------------------------- helpers
def now_ny():
    return datetime.now(tz=NY)


def ch(hh, mm, day):
    """New York clock time on `day` -> Zurich clock time string"""
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=NY).astimezone(CH).strftime("%H:%M")


def send(text, test=False):
    print(text)
    if test:
        return
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("Telegram secrets missing - message only printed")
        return
    for _ in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                              data={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=20)
            if r.ok:
                return
            print("Telegram error", r.status_code, r.text[:200])
        except Exception as exc:  # network hiccup
            print("Telegram failed", exc)
        time.sleep(3)


def wait_until(target, latest_minutes):
    """sleep until target (NY datetime). Returns False if we are already later than target+latest."""
    n = now_ny()
    if n > target + timedelta(minutes=latest_minutes):
        return False
    while now_ny() < target:
        time.sleep(min(30, max(1, (target - now_ny()).total_seconds())))
    return True


def m1(ticker, days_back=5, test_day=None):
    if test_day:
        start = test_day - timedelta(days=days_back)
        df = yf.download(ticker, interval="1m", start=str(start), end=str(test_day + timedelta(days=1)),
                         progress=False, prepost=True, auto_adjust=False)
    else:
        df = yf.download(ticker, interval="1m", period=f"{days_back}d", progress=False, prepost=True, auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df.index = df.index.tz_convert(NY)
    return df[["open", "high", "low", "close"]].dropna()


def daily(ticker, end_day):
    df = yf.download(ticker, interval="1d", period="3mo", progress=False, auto_adjust=False)
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index).date
    return df[df.index < end_day]


def micros(risk_pts, usd_per_pt):
    one = risk_pts * usd_per_pt
    n = math.floor(RISK / one)
    if n == 0 and one <= 1.1 * RISK:  # 1 micro only slightly above budget: still take it
        n = 1
    return min(n, MAX_MICROS)


# ----------------------------------------------------------------------------------- gold
def right_slot(slot):
    """GitHub cron runs in UTC; we schedule one run for summer (EDT) and one for winter (EST)
    time and only the matching one continues."""
    if slot is None:
        return True
    is_dst = bool(now_ny().dst())
    ok = (slot == "edt") == is_dst
    if not ok:
        print(f"slot {slot} not active (New York DST={is_dst}) - exit")
    return ok


def gold(test_day=None):
    n = now_ny()
    day = test_day or (n.date() + timedelta(days=1) if n.hour >= 12 else n.date())
    if day.weekday() >= 5:
        return
    if not test_day:
        target = datetime(day.year, day.month, day.day, 1, 0, 30, tzinfo=NY)
        if target - now_ny() > timedelta(minutes=75):
            print("too early - exit")
            return
        if not wait_until(target, latest_minutes=30):
            send(f"GOLD London breakout {day}: bot started too late (GitHub delay) - no signal today")
            return
    df = pd.DataFrame()
    for _ in range(12):  # wait until the data feed has the 00:59 bar
        df = m1("GC=F", 3, test_day)
        d0 = datetime(day.year, day.month, day.day, tzinfo=NY)
        if not df.empty and df.index.max() >= d0 + timedelta(minutes=59) or test_day:
            break
        time.sleep(60)
    rng = df[(df.index >= d0 - timedelta(hours=4)) & (df.index < d0 + timedelta(hours=1))]
    if len(rng) < 150:
        send(f"GOLD {day}: not enough price data ({len(rng)} min) - no signal today", test_day)
        return
    hi, lo = float(rng["high"].max()), float(rng["low"].min())
    width = (hi - lo) / ((hi + lo) / 2)
    if not 0.003 <= width <= 0.016:
        send(f"GOLD London breakout {day}: no trade (night range {width*100:.2f} %, allowed 0.30-1.60 %)", test_day)
        return
    rb, rs = min(hi - lo, 0.006 * hi), min(hi - lo, 0.006 * lo)
    nb, ns = micros(rb, 10), micros(rs, 10)
    if nb == 0 and ns == 0:
        send(f"GOLD {day}: no trade - stop ${rb:.1f} too wide, 1 MGC would risk ${rb*10:.0f} > ${RISK:.0f}", test_day)
        return

    def leg(name, n, entry, stop, tp, r):
        if n == 0:
            return f"{name}: skip (1 MGC would risk ${r*10:.0f})\n"
        return (f"<b>{name} {n} MGC</b> @ {entry:.1f}\n   SL {stop:.1f}   TP {tp:.1f}   (risk ${n*r*10:.0f})\n")

    msg = (f"<b>GOLD London breakout</b> {day:%a %d.%m.}\nNight range {lo:.1f} - {hi:.1f}\n\n"
           + leg("BUY STOP", nb, hi, hi - rb, hi + 4 * rb, rb)
           + leg("SELL STOP", ns, lo, lo + rs, lo - 4 * rs, rs)
           + f"\nPlace both as OCO now (one cancels the other).\nCancel unfilled orders and close at "
             f"<b>{ch(14, 0, day)} (CH)</b> = 14:00 New York.")
    send(msg, test_day)


# ----------------------------------------------------------------------------------- nasdaq
def nas(test_day=None):
    day = test_day or now_ny().date()
    if day.weekday() >= 5:
        return
    if not test_day:
        target = datetime(day.year, day.month, day.day, 9, 35, 8, tzinfo=NY)
        if target - now_ny() > timedelta(minutes=75):
            print("too early - exit")
            return
        if not wait_until(target, latest_minutes=10):
            send(f"NAS ORB5 {day}: bot started too late (GitHub delay) - no signal today")
            return
    q = pd.DataFrame()
    for _ in range(6):
        q = m1("QQQ", 2, test_day)
        first = q[q.index.date == day].between_time("09:30", "09:34") if not q.empty else q
        if len(first) >= 5 or test_day:
            break
        time.sleep(10)
    if len(first) < 4:
        send(f"NAS ORB5 {day}: market closed or no data - no signal today", test_day)
        return
    o5, c5 = float(first["open"].iloc[0]), float(first["close"].iloc[-1])
    if o5 == c5:
        send(f"NAS ORB5 {day}: first 5-min candle unchanged - no trade", test_day)
        return
    side = 1 if c5 > o5 else -1
    qd = daily("QQQ", day)
    pc = qd["close"].shift()
    tr = pd.concat([qd["high"] - qd["low"], (qd["high"] - pc).abs(), (qd["low"] - pc).abs()], axis=1).max(axis=1)
    atr_q = float(tr.rolling(14).mean().iloc[-1])
    nq = daily("NQ=F", day)["close"].iloc[-1]
    ratio = float(nq) / float(qd["close"].iloc[-1])
    risk_pts = 0.25 * atr_q * ratio
    n = micros(risk_pts, 2)
    exit_ch = ch(15, 59, day)
    if n == 0:
        send(f"NAS ORB5 {day:%a %d.%m.}: {'BUY' if side > 0 else 'SELL'} signal but stop {risk_pts:.0f} pts is too wide "
             f"(1 MNQ = ${risk_pts*2:.0f} > ${RISK:.0f}) - skip", test_day)
        return
    sl_ticks, tp_ticks = round(risk_pts * 4), round(3 * risk_pts * 4)
    msg = (f"<b>NAS ORB5 {'BUY' if side > 0 else 'SELL'} {n} MNQ</b> {day:%a %d.%m.}\n"
           f"Market order NOW\nSL {risk_pts:.0f} pts ({sl_ticks} ticks) {'below' if side > 0 else 'above'} your fill\n"
           f"TP {3*risk_pts:.0f} pts ({tp_ticks} ticks) {'above' if side > 0 else 'below'} your fill\n"
           f"Risk ${n*risk_pts*2:.0f}\nClose at <b>{exit_ch} (CH)</b> = 15:59 New York if still open.")
    send(msg, test_day)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=["gold", "nas", "ping"])
    ap.add_argument("--test", help="YYYY-MM-DD replay (prints only)")
    ap.add_argument("--slot", choices=["edt", "est"], help="cron slot (summer/winter time)")
    a = ap.parse_args()
    td = date.fromisoformat(a.test) if a.test else None
    if not td and a.job != "ping" and not right_slot(a.slot):
        sys.exit(0)
    if a.job == "ping":
        send(f"✅ Signal bot connected. Risk per trade ${RISK:.0f}. Gold signal ~07:00, NAS signal ~15:35 (CH).")
    elif a.job == "gold":
        gold(td)
    else:
        nas(td)
    sys.exit(0)
