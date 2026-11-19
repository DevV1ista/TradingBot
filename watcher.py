"""All-day trading watcher for Lucid micro futures (MGC gold, MNQ Nasdaq) -> Telegram.

Runs in three segments per day (GitHub Actions, each < 6 h). State is kept in state/<date>.json,
which the workflow commits between segments.

Messages:
  GOLD  01:00 NY  OCO order (two pending orders)    -> pre-alert near a level -> triggered
        -> TP / SL hit with $ result -> reminder 13:50 -> close/cancel 14:00
  NAS   09:33 NY  prediction (candle still forming) -> 09:35 final BUY/SELL now
        -> TP / SL hit with $ result -> reminder 15:50 -> close 15:59
  16:00 NY daily summary (simulated result on a Lucid 50K account, plus running total)

Live prices (free):
  Nasdaq: QQQ 1-min (real-time on Yahoo) x ratio to NQ futures (Yahoo futures are 10 min delayed;
          the ratio is taken from the delayed data). Measured error: ~1.6 pts average.
  Gold:   PAXG 1-min on Coinbase (real-time) + basis to MGC futures (from delayed Yahoo data).
          Measured error: ~$1 average.
Orders are executed by YOUR broker at the exact levels; these alerts are informational.

  python watcher.py --segment 1 --slot est      (GitHub; slot = winter/summer cron)
  python watcher.py --replay 2026-11-17        (simulate a past day minute by minute, prints only)
  env STRATEGY = balanced (default, ~60 % winners) | hw (~70 %) | classic (~40 %, biggest wins)
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
RISK = float(os.getenv("RISK_USD") or "300")
MAX_MICROS = int(os.getenv("MAX_MICROS") or "10")
MGC_USD, MNQ_USD = 10.0, 2.0          # $ per 1.0 price move per micro contract
LINE = "━━━━━━━━━━━━━━━"
STATE_DIR = Path(__file__).parent / "state"
SEGMENTS = {1: ((0, 50), (5, 40)), 2: ((5, 40), (11, 0)), 3: ((11, 0), (16, 5))}  # New York time, each < 6 h


def usd(x):
    return f"{'+' if x >= 0 else '−'}${abs(x):,.0f}".replace(",", "'")


def fmt(x, nd=1):
    return f"{x:,.{nd}f}".replace(",", "'")


def ny_time(day, hh, mm, ss=0):
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=NY)


def ch(day, hh, mm):
    return ny_time(day, hh, mm).astimezone(CH).strftime("%H:%M")


def micros(risk_pts, usd_per_pt):
    one = risk_pts * usd_per_pt
    n = math.floor(RISK / one)
    if n == 0 and one <= 1.1 * RISK:
        n = 1
    return min(n, MAX_MICROS)


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


class LiveData:
    """real-time sources: completed bars only; futures are 10 min delayed by the provider"""

    def __init__(self, clock):
        self.clock = clock
        self._cache = {}

    def _cached(self, key, ttl, fn):
        t = time.time()
        if key in self._cache and t - self._cache[key][0] < ttl:
            return self._cache[key][1]
        v = fn()
        self._cache[key] = (t, v)
        return v

    def done(self, df):  # only completed 1-minute bars
        return df[df.index <= self.clock.now() - timedelta(minutes=1)]

    def qqq(self):
        return self.done(self._cached("qqq", 20, lambda: _yf("QQQ", interval="1m", period="1d", prepost=True)))

    def qqq_daily(self):
        return self._cached("qqqd", 3600, lambda: _yf("QQQ", interval="1d", period="3mo"))

    def nq(self):
        return self._cached("nq", 240, lambda: _yf("NQ=F", interval="1m", period="1d", prepost=True))

    def mgc(self):
        return self._cached("mgc", 240, lambda: _yf("MGC=F", interval="1m", period="2d", prepost=True))

    def mgc_daily(self):
        d = self._cached("mgcd", 3600, lambda: _yf("MGC=F", interval="1d", period="3mo"))
        return d[d.index.date < self.clock.now().date()]

    def paxg(self):
        def get():
            parts, end = [], self.clock.now()
            for _ in range(3):  # 3 x 300 bars = 15 h back (enough for the 20:00 range)
                start = end - timedelta(minutes=300)
                parts.append(_paxg_coinbase(start, end))
                end = start
            return pd.concat(parts).sort_index().loc[lambda d: ~d.index.duplicated()]
        return self.done(self._cached("paxg", 20, get))


class ReplayData(LiveData):
    """past day: same interface, data cut at the simulated clock (+10 min delay for futures)"""

    def __init__(self, clock, day):
        super().__init__(clock)
        s, e = str(day - timedelta(days=1)), str(day + timedelta(days=1))
        self._q = _yf("QQQ", interval="1m", start=s, end=e, prepost=True)
        self._nq = _yf("NQ=F", interval="1m", start=s, end=e, prepost=True)
        self._mgc = _yf("MGC=F", interval="1m", start=s, end=e, prepost=True)
        self._qd = _yf("QQQ", interval="1d", start=str(day - timedelta(days=120)), end=str(day + timedelta(days=1)))
        self._md = _yf("MGC=F", interval="1d", start=str(day - timedelta(days=120)), end=str(day + timedelta(days=1)))
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

    def qqq_daily(self):
        return self._qd[self._qd.index.date < self.clock.now().date()]

    def nq(self):
        return self.delayed(self._nq)

    def mgc(self):
        return self.delayed(self._mgc)

    def mgc_daily(self):
        return self._md[self._md.index.date < self.clock.now().date()]

    def paxg(self):
        return self.done(self._p)


# ===================================================================================== estimates
def nq_ratio(data):
    j = data.nq()[["close"]].join(data.qqq()[["close"]], how="inner", rsuffix="_q")
    if j.empty:
        return None
    return float((j["close"] / j["close_q"]).tail(30).median())


def nq_bars(data):
    r = nq_ratio(data)
    q = data.qqq()
    return None if r is None or q.empty else q * r


def gold_bars(data):
    """real-time MGC estimate: PAXG + basis (basis from the delayed MGC futures)"""
    p, m = data.paxg(), data.mgc()
    j = m[["close"]].join(p[["close"]], how="inner", rsuffix="_p")
    if j.empty:
        return None, m
    basis = float((j["close"] - j["close_p"]).tail(30).median())
    return p + basis, m


# ===================================================================================== strategies
# All three were backtested on Nov 2023 - Nov 2026 (TradingLab/hw_final.py, research_hw.py, run_all.py).
PROFILES = {
    # high win rate: ~70 % winners, small targets, small profit (weak in the last year)
    "hw": dict(name="Hohe Trefferquote", gold="atr", g_tp=0.15, g_sl=0.3, g_entry_end=(8, 0),
               n_body=0.08, n_tp=0.15, n_sl=0.3),
    # balanced (DEFAULT, user choice 2026-11-19): ~60 % winners, +$23.8k in 3 years on Lucid 50K sizing
    "balanced": dict(name="Ausgewogen", gold="atr", g_tp=0.25, g_sl=0.3, g_entry_end=(8, 0),
                     n_body=0.08, n_tp=0.25, n_sl=0.3),
    # classic: ~40 % winners, biggest profit per risk (original backtest winner)
    "classic": dict(name="Klassisch", gold="range", g_tp_r=4.0, g_entry_end=(14, 0),
                    n_body=0.0, n_tp=0.75, n_sl=0.25),
}
P = PROFILES.get((os.getenv("STRATEGY") or "balanced").lower(), PROFILES["balanced"])


def daily_atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


# ===================================================================================== state + messages
class Bot:
    def __init__(self, data, clock, send, day):
        self.data, self.clock, self.send_fn, self.day = data, clock, send, day
        self.path = STATE_DIR / f"{day}.json"
        self.s = json.loads(self.path.read_text()) if self.path.exists() else {"gold": {}, "nas": {}, "sent": []}

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

    # ------------------------------------------------------------------ gold (XAUUSD)
    def gold_setup(self):
        g = self.s["gold"]
        if g or not self.after(1, 0, 30):
            return
        if self.after(1, 30):  # segment started too late: a stale order would be wrong
            self.s["gold"] = {"status": "off"}
            self.once("gold_late", "⚠️ XAUUSD: Bot heute zu spät gestartet (GitHub) – kein Gold-Trade heute")
            return
        est, mgc = gold_bars(self.data)
        d0 = ny_time(self.day, 0, 0)
        lo_t, hi_t = d0 - timedelta(hours=4), d0 + timedelta(hours=1)
        fut = mgc[(mgc.index >= lo_t) & (mgc.index < hi_t)]
        last_fut = fut.index.max() if len(fut) else lo_t
        rest = est[(est.index > last_fut) & (est.index < hi_t)] if est is not None else pd.DataFrame()
        bars = pd.concat([fut, rest])
        atr = daily_atr(self.data.mgc_daily())
        if len(bars) < 120 or not math.isfinite(atr):
            self.s["gold"] = {"status": "off"}
            self.once("gold_nodata", f"⚪ XAUUSD {self.day:%d.%m.}: zu wenig Kursdaten – heute kein Trade")
            return
        hi, lo = float(bars["high"].max()), float(bars["low"].min())
        if P["gold"] == "range":  # classic
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
        legs = {"long": {"entry": hi, "sl": hi - rb, "tp": hi + tb, "n": micros(rb, MGC_USD), "r": rb, "t": tb},
                "short": {"entry": lo, "sl": lo + rs, "tp": lo - ts, "n": micros(rs, MGC_USD), "r": rs, "t": ts}}
        eh, em = P["g_entry_end"]
        self.s["gold"] = {"status": "pending", "legs": legs, "checked": hi_t.isoformat(), "alerted": [], "atr": atr}
        if not legs["long"]["n"] and not legs["short"]["n"]:
            self.s["gold"]["status"] = "off"
            self.once("gold_none", f"⚪ XAUUSD {self.day:%d.%m.}: heute kein Trade (Stop zu weit für dein Risiko ${RISK:.0f})")
            return

        def leg(side, L):
            word = "🟢 XAUUSD BUY STOP" if side == "long" else "🔴 XAUUSD SELL STOP"
            if not L["n"]:
                return f"{word}: heute auslassen (Risiko zu gross)"
            return f"{word} {fmt(L['entry'])}\nTP {fmt(L['tp'])}  ·  SL {fmt(L['sl'])}  ·  Menge {L['n']}"
        L = legs["long"] if legs["long"]["n"] else legs["short"]
        msg = (f"🟡 XAUUSD {self.day:%d.%m.}\n<b>OCO-ORDER: BEIDE ORDERS SETZEN</b>\n(wird eine ausgelöst, wird die andere gelöscht)\n"
               f"{LINE}\n{leg('long', legs['long'])}\n{LINE}\n{leg('short', legs['short'])}\n{LINE}\n"
               f"Gewinn bei TP ca. {usd(L['n']*L['t']*MGC_USD)}  ·  Verlust bei SL ca. {usd(-L['n']*L['r']*MGC_USD)}\n"
               f"Nicht ausgelöst bis {ch(self.day, eh, em)} Uhr → beide löschen\n"
               f"Offene Position spätestens {ch(self.day, 14, 0)} Uhr schliessen")
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
                                           f"TP {fmt(leg['tp'])}  ·  SL {fmt(leg['sl'])}  ·  Menge {leg['n']}\n"
                                           f"(ohne OCO: die andere Order jetzt löschen)")
            if g["status"] in ("long", "short"):
                leg, sg = g["legs"][g["status"]], 1 if g["status"] == "long" else -1
                hit_sl = b["low"] <= leg["sl"] if sg > 0 else b["high"] >= leg["sl"]
                hit_tp = b["high"] >= leg["tp"] if sg > 0 else b["low"] <= leg["tp"]
                if hit_sl or hit_tp:
                    px = leg["sl"] if hit_sl else leg["tp"]
                    pnl = (px - leg["entry"]) * sg * leg["n"] * MGC_USD
                    g |= {"status": "closed", "pnl": pnl}
                    self.once("gold_exit", f"{'❌ XAUUSD SL' if hit_sl else '✅ XAUUSD TP'} {fmt(px)} ({self.at(t)} Uhr){self.late(t)}\n"
                                           f"Ergebnis Lucid 50K: <b>{usd(pnl)}</b>{'' if hit_sl else ' 🎉'}")
                    break
            g["checked"] = (t + timedelta(minutes=1)).isoformat()
        if g["status"] == "pending" and not self.after(eh, em):  # pre-alert near a level
            for side in ("long", "short"):
                Lx = g["legs"][side]
                dist = (Lx["entry"] - price) if side == "long" else (price - Lx["entry"])
                if Lx["n"] and 0 < dist <= 0.035 * g["atr"] and side not in g["alerted"]:
                    g["alerted"].append(side)
                    self.once(f"gold_pre_{side}", f"⚠️ GLEICH: XAUUSD {'BUY' if side == 'long' else 'SELL'} {fmt(Lx['entry'])}\n"
                                                  f"TP {fmt(Lx['tp'])}  ·  SL {fmt(Lx['sl'])}  ·  Menge {Lx['n']}\n(noch ca. ${dist:.1f})")
        if g["status"] == "pending" and self.after(eh, em):
            g["status"] = "expired"
            self.once("gold_exit", f"⏹ XAUUSD: heute nicht ausgelöst – beide Orders JETZT löschen")
        if g["status"] in ("long", "short"):
            leg, sg = g["legs"][g["status"]], 1 if g["status"] == "long" else -1
            now_pnl = (price - leg["entry"]) * sg * leg["n"] * MGC_USD
            if self.after(13, 50):
                self.once("gold_rem", f"⏰ XAUUSD in 10 Min schliessen ({ch(self.day, 14, 0)} Uhr)\naktuell ca. {usd(now_pnl)}")
            if self.after(14, 0):
                g |= {"status": "closed", "pnl": now_pnl}
                self.once("gold_exit", f"⏹ XAUUSD JETZT SCHLIESSEN ca. {fmt(price)}\nErgebnis Lucid 50K: <b>{usd(now_pnl)}</b>")
        self.save()

    # ------------------------------------------------------------------ nasdaq
    def nas_plan(self, o, c, ratio):
        """o/c of the first QQQ candle; levels in NQ points"""
        qd = self.data.qqq_daily()
        atr_q = daily_atr(qd)
        if abs(c - o) <= P["n_body"] * atr_q:
            return None
        side = 1 if c > o else -1
        entry, atr = c * ratio, atr_q * ratio
        r, t = P["n_sl"] * atr, P["n_tp"] * atr
        return {"side": side, "entry": entry, "sl": entry - side * r, "tp": entry + side * t, "r": r, "t": t, "n": micros(r, MNQ_USD)}

    def nas_text(self, L):
        w = "BUY" if L["side"] > 0 else "SELL"
        return (f"NASDAQ {w} ca. {fmt(L['entry'], 0)}\nTP {fmt(L['tp'], 0)}  ·  SL {fmt(L['sl'], 0)}  ·  Menge {L['n']}\n"
                f"(TP {'+' if L['side'] > 0 else '−'}{L['t']:.0f} / SL {'−' if L['side'] > 0 else '+'}{L['r']:.0f} Punkte ab deinem Einstieg)\n"
                f"Gewinn bei TP ca. {usd(L['n']*L['t']*MNQ_USD)}  ·  Verlust bei SL ca. {usd(-L['n']*L['r']*MNQ_USD)}")

    def nas(self):
        n = self.s["nas"]
        q = self.data.qqq()
        today = q[q.index.date == self.day]
        if self.after(9, 33, 40) and not self.after(9, 35, 30) and not n:
            part, r = today.between_time("09:30", "09:33"), nq_ratio(self.data)
            if len(part) >= 3 and r:
                L = self.nas_plan(float(part["open"].iloc[0]), float(part["close"].iloc[-1]), r)
                if L and L["n"]:
                    self.once("nas_pre", f"🔮 GLEICH (wahrscheinlich): {'🟢' if L['side'] > 0 else '🔴'} {self.nas_text(L)}\n"
                                         f"Bestätigung kommt um {ch(self.day, 9, 35)} Uhr")
        if self.after(9, 35, 5) and not n.get("status"):
            if self.after(9, 45):
                self.s["nas"] = {"status": "off"}
                self.once("nas_late", "⚠️ NASDAQ: Bot heute zu spät gestartet (GitHub) – kein NASDAQ-Trade heute")
                return
            first, r = today.between_time("09:30", "09:34"), nq_ratio(self.data)
            if len(first) < 4 or not r:
                if self.after(9, 40):
                    self.s["nas"] = {"status": "off"}
                    self.once("nas_none", f"⚪ NASDAQ {self.day:%d.%m.}: keine Kursdaten (Börse zu?) – kein Trade")
                return
            L = self.nas_plan(float(first["open"].iloc[0]), float(first["close"].iloc[-1]), r)
            if not L or not L["n"]:
                self.s["nas"] = {"status": "off"}
                why = "Startkerze zu schwach" if not L else f"Stop zu weit für dein Risiko ${RISK:.0f}"
                self.once("nas_none", f"⚪ NASDAQ {self.day:%d.%m.}: heute kein Trade ({why})")
                return
            self.s["nas"] = L | {"status": "open", "checked": ny_time(self.day, 9, 35).isoformat()}
            self.once("nas_signal", f"{'🟢' if L['side'] > 0 else '🔴'} <b>JETZT:</b> {self.nas_text(L)}\n"
                                    f"Spätestens {ch(self.day, 15, 59)} Uhr schliessen")
        if n.get("status") == "open":
            bars = nq_bars(self.data)
            if bars is None or bars.empty:
                return
            price = float(bars["close"].iloc[-1])
            for t, b in bars[(bars.index >= datetime.fromisoformat(n["checked"])) & (bars.index < ny_time(self.day, 15, 59))].iterrows():
                s = n["side"]
                hit_sl = b["low"] <= n["sl"] if s > 0 else b["high"] >= n["sl"]
                hit_tp = b["high"] >= n["tp"] if s > 0 else b["low"] <= n["tp"]
                if hit_sl or hit_tp:
                    px = n["sl"] if hit_sl else n["tp"]
                    pnl = (px - n["entry"]) * s * n["n"] * MNQ_USD
                    n |= {"status": "closed", "pnl": pnl}
                    self.once("nas_exit", f"{'❌ NASDAQ SL' if hit_sl else '✅ NASDAQ TP'} ca. {fmt(px, 0)} ({self.at(t)} Uhr){self.late(t)}\n"
                                          f"Ergebnis Lucid 50K: <b>{usd(pnl)}</b>{'' if hit_sl else ' 🎉'}")
                    break
                n["checked"] = (t + timedelta(minutes=1)).isoformat()
            if n["status"] == "open":
                now_pnl = (price - n["entry"]) * n["side"] * n["n"] * MNQ_USD
                if self.after(15, 50):
                    self.once("nas_rem", f"⏰ NASDAQ in 10 Min schliessen ({ch(self.day, 15, 59)} Uhr) – Markt schliesst\naktuell ca. {usd(now_pnl)}")
                if self.after(15, 59):
                    n |= {"status": "closed", "pnl": now_pnl}
                    self.once("nas_exit", f"⏹ NASDAQ JETZT SCHLIESSEN ca. {fmt(price, 0)}\nErgebnis Lucid 50K: <b>{usd(now_pnl)}</b>")
        self.save()

    # ------------------------------------------------------------------ summary
    def summary(self):
        if not self.after(16, 0) or "summary" in self.s["sent"]:
            return
        g, n = self.s["gold"].get("pnl"), self.s["nas"].get("pnl")
        if g is None and n is None:
            self.once("summary", f"📊 {self.day:%d.%m.}: heute keine Trades")
            return
        tot_path = self.path.parent / "total.json"
        tot = json.loads(tot_path.read_text()) if tot_path.exists() else {"pnl": 0.0, "days": 0, "wins": 0, "trades": 0}
        res = [x for x in (g, n) if x is not None]
        tot = {"pnl": tot["pnl"] + sum(res), "days": tot["days"] + 1,
               "wins": tot.get("wins", 0) + sum(1 for x in res if x > 0), "trades": tot.get("trades", 0) + len(res)}
        tot_path.write_text(json.dumps(tot))
        lines = [f"📊 TAGESBILANZ {self.day:%d.%m.} (Lucid 50K, simuliert)"]
        if g is not None:
            lines.append(f"XAUUSD: {usd(g)}")
        if n is not None:
            lines.append(f"NASDAQ: {usd(n)}")
        lines += [LINE, f"Heute: <b>{usd(sum(res))}</b>",
                  f"Seit Start: {usd(tot['pnl'])}  ·  Treffer {tot['wins']}/{tot['trades']} ({100*tot['wins']/max(1, tot['trades']):.0f} %)"]
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


def run_replay(day):
    clock = Clock(ny_time(day, 0, 45))
    data = ReplayData(clock, day)
    out = []
    bot = Bot(data, clock, lambda m: out.append(f"[{clock.now():%H:%M} NY / {clock.now().astimezone(CH):%H:%M} CH]\n{m}\n"), day)
    bot.path = Path(os.getenv("TEMP", "/tmp")) / f"replay_{day}.json"
    bot.s = {"gold": {}, "nas": {}, "sent": []}
    global STATE_DIR
    STATE_DIR = Path(os.getenv("TEMP", "/tmp")) / "replay_state"
    while clock.now() < ny_time(day, 16, 5):
        bot.tick()
        clock.t = clock.now() + timedelta(minutes=1)
    print("\n".join(out))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--segment", type=int, choices=[1, 2, 3])
    ap.add_argument("--slot", choices=["edt", "est"])
    ap.add_argument("--replay")
    a = ap.parse_args()
    if a.replay:
        run_replay(date.fromisoformat(a.replay))
    else:
        run_live(a.segment, a.slot)
