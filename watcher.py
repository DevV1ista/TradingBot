"""All-day trading watcher -> Telegram. XAUUSD (London breakout) + NASDAQ (5-min opening range).

Runs in three segments per day (GitHub Actions, each < 6 h). State lives in state/ and is committed
by the workflow between segments: state/<date>.json (today's orders) and state/account.json (the
simulated prop account: balance, account number, phase, payouts, all-time statistics).

BROKER=ftmo (default): CFD prices (XAUUSD spot, US100 = Nasdaq-100 index), position size in lots,
                       risk = RISK_PCT (0.5) % of ACCOUNT_SIZE (100000), FTMO 2-step account tracking.
BROKER=lucid:          futures prices (MGC / MNQ estimates), micro contracts, RISK_USD (300).
STRATEGY = classic (default for ftmo, ~40 % winners, big wins) | balanced (~60 %) | hw (~70 %)

Live prices (free): QQQ 1-min (real-time) scaled to the Nasdaq-100 index (^NDX, real-time) or to
NQ futures; gold = Coinbase PAXG 1-min + basis to the Swissquote spot quote (FTMO) or to MGC futures.
Orders are executed by YOUR broker at the exact levels; these alerts are informational.

  python watcher.py --segment 1 --slot est                 (GitHub; slot = winter/summer cron)
  python watcher.py --replay 2026-11-12 --to 2026-11-17    (simulate past days, prints only)
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
NY, CH, UTC = ZoneInfo("America/New_York"), ZoneInfo("Europe/Zurich"), ZoneInfo("UTC")


def env(name, default):
    return os.getenv(name) or default


BROKER = env("BROKER", "ftmo").lower()
FTMO = BROKER == "ftmo"
ACCOUNT_SIZE = float(env("ACCOUNT_SIZE", "100000"))
RISK = ACCOUNT_SIZE * float(env("RISK_PCT", "0.5")) / 100 if FTMO else float(env("RISK_USD", "300"))
MAX_QTY = float(env("MAX_MICROS", "10")) if not FTMO else 50.0
FEE = float(env("FEE_USD", "590"))
# $ per 1.0 price move for ONE unit (FTMO: 1 lot; Lucid: 1 micro). FTMO: XAUUSD 1 lot = 100 oz,
# US100.cash 1 lot = $1 per index point (check "Specification" in MT5 and override if different).
USD_PER_UNIT = {"gold": float(env("LOT_USD_XAU", "100")) if FTMO else 10.0,
                "nas": float(env("LOT_USD_US100", "1")) if FTMO else 2.0}
STEP = 0.01 if FTMO else 1.0
QTY_WORD = "Lots" if FTMO else "Menge"
LINE = "━━━━━━━━━━━━━━━"
STATE_DIR = Path(__file__).parent / "state"
SEGMENTS = {1: ((0, 50), (5, 40)), 2: ((5, 40), (11, 0)), 3: ((11, 0), (16, 5))}  # New York time, each < 6 h


def usd(x):
    return f"{'+' if x >= 0 else '−'}${abs(x):,.0f}".replace(",", "'")


def money(x):
    return f"${x:,.0f}".replace(",", "'")


def fmt(x, nd=1):
    return f"{x:,.{nd}f}".replace(",", "'")


def ny_time(day, hh, mm, ss=0):
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=NY)


def ch(day, hh, mm):
    return ny_time(day, hh, mm).astimezone(CH).strftime("%H:%M")


def size(risk_pts, market):
    """position size so that the stop costs ~RISK. Returns (qty, $ per 1.0 move of the position)."""
    one = risk_pts * USD_PER_UNIT[market]
    q = math.floor(RISK / one / STEP + 1e-9) * STEP
    if q < STEP and one * STEP <= 1.1 * RISK:
        q = STEP
    q = round(min(q, MAX_QTY), 2)
    return q, q * USD_PER_UNIT[market]


def qty_txt(q):
    return f"{q:.2f}" if FTMO else f"{q:.0f}"


# ===================================================================================== data
def _yf(ticker, **kw):
    for attempt in range(3):
        try:
            d = yf.download(ticker, progress=False, auto_adjust=False, **kw)
            if d is not None and not d.empty:
                d.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in d.columns]
                if d.index.tz is not None:
                    d.index = d.index.tz_convert(NY)
                return d[[c for c in ("open", "high", "low", "close") if c in d.columns]].dropna()
        except Exception as exc:
            print("yahoo error", ticker, exc)
        time.sleep(3 * (attempt + 1))
    return pd.DataFrame(columns=["open", "high", "low", "close"])


def _paxg_coinbase(start=None, end=None):
    params = {"granularity": 60}
    if start is not None:
        params |= {"start": start.astimezone(UTC).isoformat(), "end": end.astimezone(UTC).isoformat()}
    r = requests.get("https://api.exchange.coinbase.com/products/PAXG-USD/candles", params=params,
                     timeout=15, headers={"User-Agent": "signal-bot"})
    r.raise_for_status()
    df = pd.DataFrame(r.json(), columns=["t", "low", "high", "open", "close", "vol"])
    df.index = pd.DatetimeIndex(pd.to_datetime(df["t"], unit="s", utc=True)).tz_convert(NY)
    return df[["open", "high", "low", "close"]].astype(float).sort_index()


def _spot_gold():
    """Swissquote public quote (real-time XAU/USD spot), mid price"""
    r = requests.get("https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD", timeout=10)
    q = r.json()[0]["spreadProfilePrices"][0]
    return (float(q["bid"]) + float(q["ask"])) / 2


class LiveData:
    """real-time sources: completed bars only; Yahoo futures are 10 min delayed by the provider"""

    def __init__(self, clock):
        self.clock = clock
        self._cache = {}
        self._basis = []  # spot - PAXG samples

    def _cached(self, key, ttl, fn):
        t = time.time()
        if key in self._cache and t - self._cache[key][0] < ttl:
            return self._cache[key][1]
        v = fn()
        self._cache[key] = (t, v)
        return v

    def done(self, df):  # only completed 1-minute bars
        return df[df.index <= self.clock.now() - timedelta(minutes=1)]

    def before_today(self, df):
        return df[df.index.date < self.clock.now().date()]

    def qqq(self):
        return self.done(self._cached("qqq", 20, lambda: _yf("QQQ", interval="1m", period="1d", prepost=True)))

    def qqq_daily(self):
        return self.before_today(self._cached("qqqd", 3600, lambda: _yf("QQQ", interval="1d", period="3mo")))

    def ndx(self):
        return self.done(self._cached("ndx", 20, lambda: _yf("^NDX", interval="1m", period="1d")))

    def ndx_daily(self):
        return self.before_today(self._cached("ndxd", 3600, lambda: _yf("^NDX", interval="1d", period="3mo")))

    def nq(self):
        return self._cached("nq", 240, lambda: _yf("NQ=F", interval="1m", period="1d", prepost=True))

    def mgc(self):
        return self._cached("mgc", 240, lambda: _yf("MGC=F", interval="1m", period="2d", prepost=True))

    def mgc_daily(self):
        return self.before_today(self._cached("mgcd", 3600, lambda: _yf("MGC=F", interval="1d", period="3mo")))

    def paxg(self):
        def get():
            parts, end = [], self.clock.now()
            for _ in range(3):  # 3 x 300 bars = 15 h back (enough for the 20:00 range)
                start = end - timedelta(minutes=300)
                parts.append(_paxg_coinbase(start, end))
                end = start
            return pd.concat(parts).sort_index().loc[lambda d: ~d.index.duplicated()]
        return self.done(self._cached("paxg", 20, get))

    def spot_basis(self, paxg):
        """spot - PAXG, median of recent samples (Swissquote real-time quote vs last PAXG close)"""
        try:
            self._basis.append(self._cached("spot", 15, _spot_gold) - float(paxg["close"].iloc[-1]))
            self._basis = self._basis[-40:]
        except Exception as exc:
            print("spot quote error", exc)
        return float(pd.Series(self._basis).median()) if self._basis else None


class ReplayData(LiveData):
    """past day: same interface, data cut at the simulated clock (+10 min delay for futures)"""

    def __init__(self, clock, day, local_gold=None):
        super().__init__(clock)
        s, e = str(day - timedelta(days=1)), str(day + timedelta(days=1))
        d0 = str(day - timedelta(days=120))
        self._q = _yf("QQQ", interval="1m", start=s, end=e, prepost=True)
        self._ndx = _yf("^NDX", interval="1m", start=s, end=e)
        self._nq = _yf("NQ=F", interval="1m", start=s, end=e, prepost=True)
        self._mgc = _yf("MGC=F", interval="1m", start=s, end=e, prepost=True)
        self._qd = _yf("QQQ", interval="1d", start=d0, end=e)
        self._nd = _yf("^NDX", interval="1d", start=d0, end=e)
        self._md = _yf("MGC=F", interval="1d", start=d0, end=e)
        self._gold = None
        if local_gold is not None:  # exact spot data available on the PC (TradingLab)
            g = local_gold
            self._gold = g[(g.index >= ny_time(day - timedelta(days=1), 12, 0)) & (g.index < ny_time(day, 17, 0))]
        parts, t = [], ny_time(day, 16, 30)
        while t > ny_time(day - timedelta(days=1), 17, 0):
            st = t - timedelta(minutes=300)
            parts.append(_paxg_coinbase(st, t))
            t = st
            time.sleep(0.3)
        self._p = pd.concat(parts).sort_index().loc[lambda d: ~d.index.duplicated()]

    def delayed(self, df):
        return df[df.index <= self.clock.now() - timedelta(minutes=11)]

    def qqq(self):
        return self.done(self._q)

    def ndx(self):
        return self.done(self._ndx)

    def qqq_daily(self):
        return self.before_today(self._qd)

    def ndx_daily(self):
        return self.before_today(self._nd)

    def nq(self):
        return self.delayed(self._nq)

    def mgc(self):
        return self.delayed(self._mgc)

    def mgc_daily(self):
        return self.before_today(self._md)

    def paxg(self):
        return self.done(self._p)

    def spot_basis(self, paxg):
        return None  # replay without local spot data: PAXG used as is (approximation, ~$5-10 off)


# ===================================================================================== estimates
def nas_ratio(data):
    """QQQ -> traded instrument: Nasdaq-100 index (FTMO US100) or NQ futures (Lucid MNQ)"""
    other = data.ndx() if FTMO else data.nq()
    j = other[["close"]].join(data.qqq()[["close"]], how="inner", rsuffix="_q")
    if j.empty:
        return None
    return float((j["close"] / j["close_q"]).tail(30).median())


def nas_bars(data):
    r = nas_ratio(data)
    q = data.qqq()
    return None if r is None or q.empty else q * r


def gold_bars(data):
    """real-time gold estimate. FTMO: spot (PAXG + Swissquote basis); Lucid: MGC (PAXG + futures basis).
    Returns (estimate bars, reference bars for the night range)."""
    if getattr(data, "_gold", None) is not None:  # replay with exact local spot data
        g = data.done(data._gold)
        return g, g
    p = data.paxg()
    if p.empty:
        return None, p
    if FTMO:
        b = data.spot_basis(p)
        est = p + (b or 0.0)
        return est, est
    m = data.mgc()
    j = m[["close"]].join(p[["close"]], how="inner", rsuffix="_p")
    if j.empty:
        return None, m
    return p + float((j["close"] - j["close_p"]).tail(30).median()), m


def nas_daily_atr(data, ratio):
    if FTMO:
        return daily_atr(data.ndx_daily())
    return daily_atr(data.qqq_daily()) * ratio


def gold_daily_atr(data):
    return daily_atr(data.mgc_daily())


# ===================================================================================== strategies
# Backtested Nov 2023 - Nov 2026 (TradingLab: run_all.py, portfolio.py, hw_final.py, ftmo_path.py).
PROFILES = {
    # classic: ~40 % winners, biggest profit per risk (FTMO 100K at 0.5 %: ~$16k/year in the simulation)
    "classic": dict(name="Klassisch", gold="range", g_tp_r=4.0, g_entry_end=(14, 0),
                    n_body=0.0, n_tp=0.75, n_sl=0.25),
    # balanced: ~60 % winners (user's Lucid choice 2026-11-19)
    "balanced": dict(name="Ausgewogen", gold="atr", g_tp=0.25, g_sl=0.3, g_entry_end=(8, 0),
                     n_body=0.08, n_tp=0.25, n_sl=0.3),
    # high win rate: ~70 % winners, small targets, small profit
    "hw": dict(name="Hohe Trefferquote", gold="atr", g_tp=0.15, g_sl=0.3, g_entry_end=(8, 0),
               n_body=0.08, n_tp=0.15, n_sl=0.3),
}
P = PROFILES.get(env("STRATEGY", "classic" if FTMO else "balanced").lower(), PROFILES["classic"])


def daily_atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


# ===================================================================================== FTMO account tracker
class Account:
    """Simulated FTMO 2-step account that follows every signal exactly.
    Phase 1: +10 % target, Phase 2: +5 %, both with max daily loss 5 % and max loss 10 % of the start
    balance and at least 4 trading days. Funded: payout possible 14 days after the start or the last
    payout (80 % of the profit, first payout also refunds the fee). Phase changes, new accounts and
    payouts take effect at the daily summary (the bot assumes you request every possible payout)."""

    NAMES = {"P1": "Challenge Phase 1", "P2": "Challenge Phase 2 (Verification)", "F": "Funded"}

    def __init__(self, path):
        self.path = path
        self.a = json.loads(path.read_text()) if path.exists() else None

    def ensure(self, day):
        if self.a is None:
            self.a = {"num": 1, "phase": "P1", "start": ACCOUNT_SIZE, "bal": ACCOUNT_SIZE, "since": str(day),
                      "traded_days": [], "day": str(day), "day_start_bal": ACCOUNT_SIZE, "status": "active",
                      "pending": None, "last_payout": None, "payouts": 0.0, "fees": FEE, "fee_refunded": False,
                      "all": {"trades": 0, "wins": 0, "pnl": 0.0, "days": 0, "accounts_lost": 0, "passed": 0}}
        if self.a["day"] != str(day):
            self.a["day"], self.a["day_start_bal"] = str(day), self.a["bal"]
        return self.a

    def save(self):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.a, indent=1))

    def target(self):
        return self.a["start"] * {"P1": 1.10, "P2": 1.05, "F": float("inf")}[self.a["phase"]]

    def apply(self, day, pnl):
        """book one closed trade; returns (old balance, new balance, event or None)"""
        a = self.ensure(day)
        old = a["bal"]
        if a["status"] != "active":  # account already lost/passed today: trade counted in statistics only
            a["all"]["trades"] += 1
            a["all"]["wins"] += pnl > 0
            a["all"]["pnl"] += pnl
            self.save()
            return old, old, None
        a["bal"] = old + pnl
        if str(day) not in a["traded_days"]:
            a["traded_days"].append(str(day))
        a["all"]["trades"] += 1
        a["all"]["wins"] += pnl > 0
        a["all"]["pnl"] += pnl
        event = None
        if a["bal"] <= a["start"] * 0.90:
            event = "lost_total"
        elif a["bal"] - a["day_start_bal"] <= -0.05 * a["start"]:
            event = "lost_daily"
        if event:
            a["status"], a["pending"] = "lost", "new"
            a["all"]["accounts_lost"] += 1
        elif a["phase"] in ("P1", "P2") and a["bal"] >= self.target() and len(a["traded_days"]) >= 4:
            a["status"], a["pending"] = "passed", ("P2" if a["phase"] == "P1" else "F")
            a["all"]["passed"] += 1
            event = "passed"
        self.save()
        return old, a["bal"], event

    def payout_info(self, day):
        a = self.a
        if a["phase"] != "F":
            return None
        ref = date.fromisoformat(a["last_payout"] or a["since"])
        days_left = max(0, 14 - (day - ref).days)
        profit = a["bal"] - a["start"]
        share = 0.8 * max(0.0, profit) + (0 if a["fee_refunded"] else FEE if profit > 0 else 0)
        return days_left, profit, share

    def block(self, day, old=None, event=None):
        a = self.ensure(day)
        lines = [f"🏦 FTMO #{a['num']} · {self.NAMES[a['phase']]}"]
        lines.append(f"Konto: {money(old)} → <b>{money(a['bal'])}</b>" if old is not None else f"Konto: <b>{money(a['bal'])}</b>")
        if event == "lost_total":
            lines.append(f"❌ Verlustgrenze {money(a['start']*0.9)} erreicht – Konto #{a['num']} verloren")
        elif event == "lost_daily":
            lines.append(f"❌ Tagesverlust-Limit (−5 %) erreicht – Konto #{a['num']} verloren")
        elif event == "passed":
            lines.append(f"🎉 Ziel erreicht – {self.NAMES[a['phase']].split(' (')[0]} BESTANDEN")
        elif a["phase"] in ("P1", "P2") and a["status"] == "active":
            need, days = self.target() - a["bal"], len(a["traded_days"])
            if need > 0:
                lines.append(f"Ziel {money(self.target())}: noch {money(need)} · Verlustgrenze {money(a['start']*0.9)}")
            else:
                lines.append(f"Ziel erreicht ✅ – noch {4 - days} Handelstag(e) bis bestanden (min. 4)")
        elif a["phase"] == "F" and a["status"] == "active":
            days_left, profit, share = self.payout_info(day)
            if profit <= 0:
                lines.append(f"💰 Auszahlung: noch nichts (Konto unter {money(a['start'])})")
            elif days_left > 0:
                lines.append(f"💰 Auszahlung in {days_left} Tagen möglich (aktuell ca. {money(share)} für dich)")
            else:
                lines.append(f"💰 Auszahlung JETZT möglich: ca. {money(share)} für dich")
        return "\n".join(lines)

    def end_of_day(self, day):
        """daily summary: payouts, phase changes and new accounts take effect; returns extra lines"""
        a = self.ensure(day)
        notes = []
        if a["status"] == "active" and a["phase"] == "F":
            days_left, profit, share = self.payout_info(day)
            if days_left == 0 and profit > 0:
                notes.append(f"💰 Auszahlung jetzt beantragen: ca. <b>{money(share)}</b> für dich "
                             f"(80 % von {money(profit)}{'' if a['fee_refunded'] else ' + Gebühr zurück'}).\n"
                             f"Der Bot rechnet ab morgen so, als hättest du sie genommen (Konto wieder {money(a['start'])}).")
                a["payouts"] += share
                a["fee_refunded"] = True
                a["bal"], a["last_payout"] = a["start"], str(day)
        if a["pending"] == "new":
            a.update(num=a["num"] + 1, phase="P1", bal=a["start"], since=str(day + timedelta(days=1)), traded_days=[],
                     status="active", pending=None, last_payout=None, fee_refunded=False, fees=a["fees"] + FEE)
            notes.append(f"🔁 Ab morgen: neues Konto FTMO #{a['num']} · Challenge Phase 1 ({money(a['start'])}, Gebühr ca. {money(FEE)})")
        elif a["pending"] in ("P2", "F"):
            a.update(phase=a["pending"], bal=a["start"], since=str(day + timedelta(days=1)), traded_days=[],
                     status="active", pending=None, last_payout=None)
            notes.append(f"➡️ Ab morgen: FTMO #{a['num']} · {self.NAMES[a['phase']]} ({money(a['start'])})")
        if a["day_start_bal"] != a["bal"] or notes:
            pass
        a["all"]["days"] += 1
        self.save()
        return notes

    def alltime(self):
        x = self.a["all"]
        rate = 100 * x["wins"] / max(1, x["trades"])
        return (f"Seit Start: {x['trades']} Trades · {x['wins']} Treffer ({rate:.0f} %) · {usd(x['pnl'])}\n"
                f"Konten: {self.a['num']} (verloren {x['accounts_lost']}) · Auszahlungen {money(self.a['payouts'])} · Gebühren {money(self.a['fees'])}")


# ===================================================================================== bot (signals)
class Bot:
    def __init__(self, data, clock, send, day, state_dir=None):
        self.data, self.clock, self.send_fn, self.day = data, clock, send, day
        self.dir = state_dir or STATE_DIR
        self.path = self.dir / f"{day}.json"
        self.s = json.loads(self.path.read_text()) if self.path.exists() else {"gold": {}, "nas": {}, "sent": []}
        self.acct = Account(self.dir / "account.json") if FTMO else None

    def save(self):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.s, indent=1))

    def once(self, key, text):
        if key in self.s["sent"]:
            return False
        self.send_fn(text)
        self.s["sent"].append(key)
        self.save()
        return True

    def late(self, t):
        return " – verspätete Meldung" if self.clock.now() - t > timedelta(minutes=4) else ""

    def after(self, hh, mm, ss=0):
        return self.clock.now() >= ny_time(self.day, hh, mm, ss)

    def at(self, t):
        return t.astimezone(CH).strftime("%H:%M")

    def result(self, pnl):
        """result line + (FTMO) account block, only used when a trade closes"""
        if not FTMO:
            return f"Ergebnis Lucid 50K: <b>{usd(pnl)}</b>{' 🎉' if pnl > 0 else ''}"
        old, new, event = self.acct.apply(self.day, pnl)
        return f"Ergebnis: <b>{usd(pnl)}</b>{' 🎉' if pnl > 0 else ''}\n{LINE}\n{self.acct.block(self.day, old, event)}"

    # ------------------------------------------------------------------ gold (XAUUSD)
    def gold_setup(self):
        g = self.s["gold"]
        if g or not self.after(1, 0, 30):
            return
        if self.after(1, 30):  # segment started too late: a stale order would be wrong
            self.s["gold"] = {"status": "off"}
            self.once("gold_late", "⚠️ XAUUSD: Bot heute zu spät gestartet (GitHub) – kein Gold-Trade heute")
            return
        est, ref = gold_bars(self.data)
        d0 = ny_time(self.day, 0, 0)
        lo_t, hi_t = d0 - timedelta(hours=4), d0 + timedelta(hours=1)
        if ref is None or est is None:
            bars = pd.DataFrame()
        else:
            fut = ref[(ref.index >= lo_t) & (ref.index < hi_t)]
            last = fut.index.max() if len(fut) else lo_t
            bars = pd.concat([fut, est[(est.index > last) & (est.index < hi_t)]])
        atr = gold_daily_atr(self.data)
        if len(bars) < 120 or not math.isfinite(atr):
            self.s["gold"] = {"status": "off"}
            self.once("gold_nodata", f"⚪ XAUUSD {self.day:%d.%m.}: zu wenig Kursdaten – heute kein Trade")
            return
        hi, lo = float(bars["high"].max()), float(bars["low"].min())
        if P["gold"] == "range":
            width = (hi - lo) / ((hi + lo) / 2)
            if not 0.003 <= width <= 0.016:
                self.s["gold"] = {"status": "off"}
                self.once("gold_none", f"⚪ XAUUSD {self.day:%d.%m.}: heute kein Trade (Nacht-Range passt nicht)")
                return
            rb, rs = min(hi - lo, 0.006 * hi), min(hi - lo, 0.006 * lo)
            tb, ts = P["g_tp_r"] * rb, P["g_tp_r"] * rs
        else:
            rb = rs = P["g_sl"] * atr
            tb = ts = P["g_tp"] * atr
        (nb, ub), (ns, us) = size(rb, "gold"), size(rs, "gold")
        legs = {"long": {"entry": hi, "sl": hi - rb, "tp": hi + tb, "n": nb, "upp": ub, "r": rb, "t": tb},
                "short": {"entry": lo, "sl": lo + rs, "tp": lo - ts, "n": ns, "upp": us, "r": rs, "t": ts}}
        eh, em = P["g_entry_end"]
        self.s["gold"] = {"status": "pending", "legs": legs, "checked": hi_t.isoformat(), "alerted": [], "atr": atr}
        if not nb and not ns:
            self.s["gold"]["status"] = "off"
            self.once("gold_none", f"⚪ XAUUSD {self.day:%d.%m.}: heute kein Trade (Stop zu weit für dein Risiko {money(RISK)})")
            return

        def leg(side, L):
            word = "🟢 XAUUSD BUY STOP" if side == "long" else "🔴 XAUUSD SELL STOP"
            if not L["n"]:
                return f"{word}: heute auslassen (Risiko zu gross)"
            return f"{word} {fmt(L['entry'])}\nTP {fmt(L['tp'])}  ·  SL {fmt(L['sl'])}  ·  {QTY_WORD} {qty_txt(L['n'])}"
        L = legs["long"] if nb else legs["short"]
        msg = (f"🟡 XAUUSD {self.day:%d.%m.}\n<b>OCO-ORDER: BEIDE ORDERS SETZEN</b>\n(wird eine ausgelöst, wird die andere gelöscht)\n"
               f"{LINE}\n{leg('long', legs['long'])}\n{LINE}\n{leg('short', legs['short'])}\n{LINE}\n"
               f"Gewinn bei TP ca. {usd(L['t']*L['upp'])}  ·  Verlust bei SL ca. {usd(-L['r']*L['upp'])}\n"
               + (f"Bis {ch(self.day, 14, 0)} Uhr: nicht ausgelöste Order löschen bzw. offene Position schliessen"
                  if (eh, em) == (14, 0) else
                  f"Nicht ausgelöst bis {ch(self.day, eh, em)} Uhr → beide löschen\n"
                  f"Offene Position spätestens {ch(self.day, 14, 0)} Uhr schliessen"))
        self.once("gold_setup", msg)

    def gold_watch(self):
        g = self.s["gold"]
        if g.get("status") not in ("pending", "long", "short"):
            return
        est, _ = gold_bars(self.data)
        if est is None or est.empty:
            return
        eh, em = P["g_entry_end"]
        entry_end, end = ny_time(self.day, eh, em), ny_time(self.day, 14, 0)
        checked = datetime.fromisoformat(g["checked"])
        price = float(est["close"].iloc[-1])
        for t, b in est[(est.index >= checked) & (est.index < end)].iterrows():
            if g["status"] == "pending" and t < entry_end:
                Lg, Sh = g["legs"]["long"], g["legs"]["short"]
                up, dn = bool(b["high"] >= Lg["entry"] and Lg["n"]), bool(b["low"] <= Sh["entry"] and Sh["n"])
                if up and dn:
                    up = abs(b["open"] - Lg["entry"]) <= abs(b["open"] - Sh["entry"])
                    dn = not up
                if up or dn:
                    side = "long" if up else "short"
                    g["status"] = side
                    leg = g["legs"][side]
                    self.once("gold_fill", f"{'🟢 XAUUSD BUY' if up else '🔴 XAUUSD SELL'} ausgelöst {fmt(leg['entry'])} ({self.at(t)} Uhr){self.late(t)}\n"
                                           f"TP {fmt(leg['tp'])}  ·  SL {fmt(leg['sl'])}  ·  {QTY_WORD} {qty_txt(leg['n'])}\n"
                                           f"(ohne OCO: die andere Order jetzt löschen)")
            if g["status"] in ("long", "short"):
                leg, sg = g["legs"][g["status"]], 1 if g["status"] == "long" else -1
                hit_sl = b["low"] <= leg["sl"] if sg > 0 else b["high"] >= leg["sl"]
                hit_tp = b["high"] >= leg["tp"] if sg > 0 else b["low"] <= leg["tp"]
                if hit_sl or hit_tp:
                    px = leg["sl"] if hit_sl else leg["tp"]
                    pnl = (px - leg["entry"]) * sg * leg["upp"]
                    g |= {"status": "closed", "pnl": pnl}
                    self.save()
                    if "gold_exit" not in self.s["sent"]:
                        self.once("gold_exit", f"{'❌ XAUUSD SL' if hit_sl else '✅ XAUUSD TP'} {fmt(px)} ({self.at(t)} Uhr){self.late(t)}\n{self.result(pnl)}")
                    break
            g["checked"] = (t + timedelta(minutes=1)).isoformat()
        if g["status"] == "pending" and not self.after(eh, em):  # pre-alert near a level
            for side in ("long", "short"):
                Lx = g["legs"][side]
                dist = (Lx["entry"] - price) if side == "long" else (price - Lx["entry"])
                if Lx["n"] and 0 < dist <= 0.035 * g["atr"] and side not in g["alerted"]:
                    g["alerted"].append(side)
                    self.once(f"gold_pre_{side}", f"⚠️ GLEICH: XAUUSD {'BUY' if side == 'long' else 'SELL'} {fmt(Lx['entry'])}\n"
                                                  f"TP {fmt(Lx['tp'])}  ·  SL {fmt(Lx['sl'])}  ·  {QTY_WORD} {qty_txt(Lx['n'])}\n(noch ca. ${dist:.1f})")
        if g["status"] == "pending" and self.after(eh, em):
            g["status"] = "expired"
            self.once("gold_exit", "⏹ XAUUSD: heute nicht ausgelöst – beide Orders JETZT löschen")
        if g["status"] in ("long", "short"):
            leg, sg = g["legs"][g["status"]], 1 if g["status"] == "long" else -1
            now_pnl = (price - leg["entry"]) * sg * leg["upp"]
            if self.after(13, 50):
                self.once("gold_rem", f"⏰ XAUUSD in 10 Min schliessen ({ch(self.day, 14, 0)} Uhr)\naktuell ca. {usd(now_pnl)}")
            if self.after(14, 0):
                g |= {"status": "closed", "pnl": now_pnl}
                self.save()
                if "gold_exit" not in self.s["sent"]:
                    self.once("gold_exit", f"⏹ XAUUSD JETZT SCHLIESSEN ca. {fmt(price)}\n{self.result(now_pnl)}")
        self.save()

    # ------------------------------------------------------------------ nasdaq
    def nas_plan(self, o, c, ratio):
        """o/c of the first QQQ 5-minute candle; levels in the traded instrument's points"""
        atr = nas_daily_atr(self.data, ratio)
        if not math.isfinite(atr) or abs(c - o) * ratio <= P["n_body"] * atr:
            return None
        side = 1 if c > o else -1
        entry = c * ratio
        r, t = P["n_sl"] * atr, P["n_tp"] * atr
        n, upp = size(r, "nas")
        return {"side": side, "entry": entry, "sl": entry - side * r, "tp": entry + side * t, "r": r, "t": t, "n": n, "upp": upp}

    def nas_text(self, L):
        w = "BUY" if L["side"] > 0 else "SELL"
        return (f"NASDAQ {w} ca. {fmt(L['entry'], 0)}\nTP {fmt(L['tp'], 0)}  ·  SL {fmt(L['sl'], 0)}  ·  {QTY_WORD} {qty_txt(L['n'])}\n"
                f"(TP {'+' if L['side'] > 0 else '−'}{L['t']:.0f} / SL {'−' if L['side'] > 0 else '+'}{L['r']:.0f} Punkte ab deinem Einstieg)\n"
                f"Gewinn bei TP ca. {usd(L['t']*L['upp'])}  ·  Verlust bei SL ca. {usd(-L['r']*L['upp'])}")

    def nas(self):
        n = self.s["nas"]
        q = self.data.qqq()
        today = q[q.index.date == self.day]
        x = self.data.ndx()
        idx_today = x[x.index.date == self.day]
        if self.after(9, 33, 40) and not self.after(9, 35, 30) and not n:
            part, r = today.between_time("09:30", "09:33"), nas_ratio(self.data)
            px = idx_today.between_time("09:30", "09:33")
            if len(part) >= 3 and r:
                o, c = float(part["open"].iloc[0]), float(part["close"].iloc[-1])
                agree = len(px) < 3 or (c > o) == (float(px["close"].iloc[-1]) > float(px["open"].iloc[0]))
                L = self.nas_plan(o, c, r) if abs(c / o - 1) > 0.0005 and agree else None
                if L and L["n"]:
                    self.s["nas_pred"] = L["side"]
                    self.once("nas_pre", f"🔮 GLEICH (wahrscheinlich): {'🟢' if L['side'] > 0 else '🔴'} {self.nas_text(L)}\n"
                                         f"Bestätigung kommt um {ch(self.day, 9, 35)} Uhr")
        if self.after(9, 35, 5) and not n.get("status"):
            if self.after(9, 45):
                self.s["nas"] = {"status": "off"}
                self.once("nas_late", "⚠️ NASDAQ: Bot heute zu spät gestartet (GitHub) – kein NASDAQ-Trade heute")
                return
            first, r = today.between_time("09:30", "09:34"), nas_ratio(self.data)
            if len(first) < 4 or not r:
                if self.after(9, 40):
                    self.s["nas"] = {"status": "off"}
                    self.once("nas_none", f"⚪ NASDAQ {self.day:%d.%m.}: keine Kursdaten (Börse zu?) – kein Trade")
                return
            o, c = float(first["open"].iloc[0]), float(first["close"].iloc[-1])
            fx = idx_today.between_time("09:30", "09:34")
            # QQQ and the Nasdaq-100 index must agree; they only disagree on near-flat candles, where the
            # direction is a coin flip (in the backtest those trades were worth ~0 R)
            agree = len(fx) < 4 or (c > o) == (float(fx["close"].iloc[-1]) > float(fx["open"].iloc[0]))
            L = self.nas_plan(o, c, r) if o != c and agree else None
            if not L or not L["n"]:
                self.s["nas"] = {"status": "off"}
                why = ("Startkerze unklar" if not agree or o == c else "Startkerze zu schwach") if not L else f"Stop zu weit für dein Risiko {money(RISK)}"
                self.once("nas_none", f"⚪ NASDAQ {self.day:%d.%m.}: heute kein Trade ({why})")
                return
            self.s["nas"] = L | {"status": "open", "checked": ny_time(self.day, 9, 35).isoformat()}
            flip = "\n⚠️ Achtung: andere Richtung als vorhergesagt!" if self.s.get("nas_pred") not in (None, L["side"]) else ""
            self.once("nas_signal", f"{'🟢' if L['side'] > 0 else '🔴'} <b>JETZT:</b> {self.nas_text(L)}{flip}\n"
                                    f"Spätestens {ch(self.day, 15, 59)} Uhr schliessen")
        if n.get("status") == "open":
            bars = nas_bars(self.data)
            if bars is None or bars.empty:
                return
            price = float(bars["close"].iloc[-1])
            for t, b in bars[(bars.index >= datetime.fromisoformat(n["checked"])) & (bars.index < ny_time(self.day, 15, 59))].iterrows():
                s = n["side"]
                hit_sl = b["low"] <= n["sl"] if s > 0 else b["high"] >= n["sl"]
                hit_tp = b["high"] >= n["tp"] if s > 0 else b["low"] <= n["tp"]
                if hit_sl or hit_tp:
                    px = n["sl"] if hit_sl else n["tp"]
                    pnl = (px - n["entry"]) * s * n["upp"]
                    n |= {"status": "closed", "pnl": pnl}
                    self.save()
                    if "nas_exit" not in self.s["sent"]:
                        self.once("nas_exit", f"{'❌ NASDAQ SL' if hit_sl else '✅ NASDAQ TP'} ca. {fmt(px, 0)} ({self.at(t)} Uhr){self.late(t)}\n{self.result(pnl)}")
                    break
                n["checked"] = (t + timedelta(minutes=1)).isoformat()
            if n["status"] == "open":
                now_pnl = (price - n["entry"]) * n["side"] * n["upp"]
                if self.after(15, 50):
                    self.once("nas_rem", f"⏰ NASDAQ in 10 Min schliessen ({ch(self.day, 15, 59)} Uhr) – Markt schliesst\naktuell ca. {usd(now_pnl)}")
                if self.after(15, 59):
                    n |= {"status": "closed", "pnl": now_pnl}
                    self.save()
                    if "nas_exit" not in self.s["sent"]:
                        self.once("nas_exit", f"⏹ NASDAQ JETZT SCHLIESSEN ca. {fmt(price, 0)}\n{self.result(now_pnl)}")
        self.save()

    # ------------------------------------------------------------------ summary
    def summary(self):
        if not self.after(16, 0) or "summary" in self.s["sent"]:
            return
        g, n = self.s["gold"].get("pnl"), self.s["nas"].get("pnl")
        res = [x for x in (g, n) if x is not None]
        lines = [f"📊 TAGESBILANZ {self.day:%d.%m.}"]
        if g is not None:
            lines.append(f"XAUUSD: {usd(g)}")
        if n is not None:
            lines.append(f"NASDAQ: {usd(n)}")
        lines.append(f"Heute: <b>{usd(sum(res))}</b>" if res else "Heute keine Trades")
        if FTMO:
            lines += [LINE, self.acct.block(self.day)]
            lines += self.acct.end_of_day(self.day)
            lines += [LINE, self.acct.alltime()]
        else:
            tot_path = self.dir / "total.json"
            tot = json.loads(tot_path.read_text()) if tot_path.exists() else {"pnl": 0.0, "wins": 0, "trades": 0}
            tot = {"pnl": tot["pnl"] + sum(res), "wins": tot.get("wins", 0) + sum(1 for x in res if x > 0),
                   "trades": tot.get("trades", 0) + len(res)}
            tot_path.write_text(json.dumps(tot))
            lines += [LINE, f"Seit Start: {usd(tot['pnl'])} · Treffer {tot['wins']}/{tot['trades']}"]
        self.once("summary", "\n".join(lines))

    def tick(self):
        if self.day.weekday() >= 5:
            return
        self.gold_setup()
        self.gold_watch()
        self.nas()
        self.summary()


# ===================================================================================== runtime
class Clock:
    def __init__(self, fixed=None):
        self.t = fixed

    def now(self):
        return self.t or datetime.now(tz=NY)


def telegram(text):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    print(f"[{datetime.now(tz=NY):%H:%M:%S} NY] {text}\n")
    if not tok or not chat:
        return
    for _ in range(3):
        try:
            if requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                             data={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=20).ok:
                return
        except Exception as exc:
            print("telegram error", exc)
        time.sleep(3)


def git_pull():
    subprocess.run(["git", "pull", "--rebase", "--quiet"], cwd=Path(__file__).parent, check=False)


def run_live(segment, slot):
    now = datetime.now(tz=NY)
    if slot and (slot == "edt") != bool(now.dst()):
        print(f"slot {slot} not active - exit")
        return
    day = now.date()
    if day.weekday() >= 5:
        return
    (sh, sm), (eh, em) = SEGMENTS[segment]
    start, end = ny_time(day, sh, sm), ny_time(day, eh, em)
    while datetime.now(tz=NY) < start:  # started early: wait, previous segment finishes first
        time.sleep(15)
    git_pull()  # get the state the previous segment committed
    clock = Clock()
    bot = Bot(LiveData(clock), clock, telegram, day)
    errors = 0
    while datetime.now(tz=NY) < end:
        try:
            bot.tick()
            errors = 0
        except Exception:
            errors += 1
            traceback.print_exc()
            if errors == 10:
                telegram("⚠️ Bot: Datenquelle antwortet nicht – Meldungen können fehlen. Bitte Positionen selbst prüfen.")
        time.sleep(30)
    bot.save()


def load_local_gold():
    """exact XAUUSD 1-minute data from TradingLab (only on the PC, optional)"""
    p = Path(env("REPLAY_XAU_FILE", str(Path(__file__).parent.parent / "data" / "xauusd_m1_raw.csv")))
    if not p.exists():
        return None
    df = pd.read_csv(p)
    df.index = pd.DatetimeIndex(pd.to_datetime(df["time"], utc=True)).tz_convert(NY)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close"})[["open", "high", "low", "close"]] / 1000
    return df[~df.index.duplicated()]


def run_replay(first, last):
    tmp = Path(os.getenv("TEMP", "/tmp")) / f"replay_{first}_{last}"
    if tmp.exists():
        for f in tmp.glob("*.json"):
            f.unlink()
    gold = load_local_gold() if FTMO else None
    print(f"replay {first} -> {last} | broker {BROKER} | strategy {P['name']} | risk {money(RISK)} | "
          f"gold source {'local spot file' if gold is not None else 'PAXG (approx.)'}\n")
    day = first
    while day <= last:
        if day.weekday() < 5:
            clock = Clock(ny_time(day, 0, 45))
            data = ReplayData(clock, day, gold)
            out = []
            bot = Bot(data, clock, lambda m: out.append(f"[{clock.now().astimezone(CH):%a %d.%m. %H:%M} CH]\n{m}\n"), day, tmp)
            while clock.now() < ny_time(day, 16, 5):
                bot.tick()
                clock.t = clock.now() + timedelta(minutes=1)
            print("\n".join(out))
        day += timedelta(days=1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--segment", type=int, choices=[1, 2, 3])
    ap.add_argument("--slot", choices=["edt", "est"])
    ap.add_argument("--replay")
    ap.add_argument("--to")
    a = ap.parse_args()
    if a.replay:
        d1 = date.fromisoformat(a.replay)
        run_replay(d1, date.fromisoformat(a.to) if a.to else d1)
    else:
        run_live(a.segment, a.slot)
