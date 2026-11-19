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
RISK = float(os.getenv("RISK_USD") or "400")
MAX_MICROS = int(os.getenv("MAX_MICROS") or "10")
MGC_USD, MNQ_USD = 10.0, 2.0          # $ per 1.0 price move per micro contract
GOLD_TP_R, NAS_TP_R, NAS_STOP_ATR = 4.0, 3.0, 0.25
PRE_ALERT_R = 0.15                    # gold pre-alert when price is within 0.15 R of a level
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


# ===================================================================================== state + messages
class Bot:
    def __init__(self, data, clock, send, day):
        self.data, self.clock, self.send_fn, self.day = data, clock, send, day
        self.path = STATE_DIR / f"{day}.json"
        self.s = json.loads(self.path.read_text()) if self.path.exists() else {"gold": {}, "nas": {}, "sent": []}

    def save(self):
        STATE_DIR.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.s, indent=1))

    def once(self, key, text):
        if key in self.s["sent"]:
            return False
        self.send_fn(text)
        self.s["sent"].append(key)
        self.save()
        return True

    def late(self, t):
        """note for events found in older bars (e.g. after a segment hand-over gap)"""
        return " – verspätete Meldung" if self.clock.now() - t > timedelta(minutes=4) else ""

    def after(self, hh, mm, ss=0):
        return self.clock.now() >= ny_time(self.day, hh, mm, ss)

    # ------------------------------------------------------------------ gold
    def gold_setup(self):
        g = self.s["gold"]
        if g or not self.after(1, 0, 30):
            return
        if self.after(1, 30):  # GitHub started this segment too late: a stale order would be wrong
            self.s["gold"] = {"status": "off"}
            self.once("gold_late", "⚠️ GOLD: Bot ist heute zu spät gestartet (GitHub-Verzögerung) – keine Gold-Order heute")
            return
        est, mgc = gold_bars(self.data)
        d0 = ny_time(self.day, 0, 0)
        lo_t, hi_t = d0 - timedelta(hours=4), d0 + timedelta(hours=1)
        fut = mgc[(mgc.index >= lo_t) & (mgc.index < hi_t)]
        last_fut = fut.index.max() if len(fut) else lo_t
        rest = est[(est.index > last_fut) & (est.index < hi_t)] if est is not None else pd.DataFrame()
        bars = pd.concat([fut, rest])
        if len(bars) < 120:
            self.s["gold"] = {"status": "off"}
            self.once("gold_nodata", f"GOLD {self.day:%d.%m.}: zu wenig Kursdaten – heute kein Gold-Trade")
            return
        hi, lo = float(bars["high"].max()), float(bars["low"].min())
        width = (hi - lo) / ((hi + lo) / 2)
        if not 0.003 <= width <= 0.016:
            self.s["gold"] = {"status": "off"}
            self.once("gold_none", f"⚪ GOLD {self.day:%d.%m.}: heute KEIN Trade\n(Nacht-Range {width*100:.2f} %, erlaubt 0.30–1.60 %)")
            return
        rb, rs = min(hi - lo, 0.006 * hi), min(hi - lo, 0.006 * lo)
        nb, ns = micros(rb, MGC_USD), micros(rs, MGC_USD)
        legs = {
            "long": {"entry": hi, "sl": hi - rb, "tp": hi + GOLD_TP_R * rb, "n": nb, "r": rb},
            "short": {"entry": lo, "sl": lo + rs, "tp": lo - GOLD_TP_R * rs, "n": ns, "r": rs},
        }
        self.s["gold"] = {"status": "pending", "legs": legs, "checked": hi_t.isoformat(), "alerted": []}

        def leg(side, L):
            if L["n"] == 0:
                return f"{'🟢 BUY' if side == 'long' else '🔴 SELL'} STOP: überspringen (1 MGC = ${L['r']*MGC_USD:.0f} Risiko)"
            return (f"{'🟢 BUY' if side == 'long' else '🔴 SELL'} STOP  {L['n']} MGC  @ {fmt(L['entry'])}\n"
                    f"TP {fmt(L['tp'])}   ·   SL {fmt(L['sl'])}\n"
                    f"Risiko {usd(-L['n']*L['r']*MGC_USD)}  ·  Ziel {usd(L['n']*L['r']*GOLD_TP_R*MGC_USD)}")
        msg = (f"🟡 GOLD {self.day:%a %d.%m.}\n<b>OCO-ORDER – BEIDE ORDERS SETZEN</b>\n"
               f"(wird eine ausgelöst, wird die andere gelöscht)\n{LINE}\n{leg('long', legs['long'])}\n{LINE}\n"
               f"{leg('short', legs['short'])}\n{LINE}\nSchliessen spätestens <b>{ch(self.day, 14, 0)} Uhr</b>")
        self.once("gold_setup", msg)

    def gold_watch(self):
        g = self.s["gold"]
        if g.get("status") not in ("pending", "long", "short"):
            return
        est, _ = gold_bars(self.data)
        if est is None or est.empty:
            return
        checked = datetime.fromisoformat(g["checked"])
        end = ny_time(self.day, 14, 0)
        bars = est[(est.index >= checked) & (est.index < end)]
        price = float(est["close"].iloc[-1])
        for t, b in bars.iterrows():
            if g["status"] == "pending":
                L, S = g["legs"]["long"], g["legs"]["short"]
                up, dn = b["high"] >= L["entry"] and L["n"], b["low"] <= S["entry"] and S["n"]
                if up and dn:  # both in one minute: the level closer to the open came first
                    up = abs(b["open"] - L["entry"]) <= abs(b["open"] - S["entry"])
                    dn = not up
                if up or dn:
                    side = "long" if up else "short"
                    g["status"], g["side"], g["filled_at"] = side, side, t.isoformat()
                    leg = g["legs"][side]
                    self.once("gold_fill", f"{'🟢 GOLD LONG' if up else '🔴 GOLD SHORT'} AUSGELÖST @ {fmt(leg['entry'])} ({t.astimezone(CH):%H:%M} Uhr){self.late(t)}\n"
                              f"TP {fmt(leg['tp'])}  ·  SL {fmt(leg['sl'])}\n"
                              f"Falls deine Plattform kein OCO hat: die {'SELL' if up else 'BUY'} STOP-Order JETZT löschen.")
            if g["status"] in ("long", "short"):
                leg, sgn = g["legs"][g["status"]], 1 if g["status"] == "long" else -1
                hit_sl = b["low"] <= leg["sl"] if sgn > 0 else b["high"] >= leg["sl"]
                hit_tp = b["high"] >= leg["tp"] if sgn > 0 else b["low"] <= leg["tp"]
                if hit_sl or hit_tp:
                    px = leg["sl"] if hit_sl else leg["tp"]
                    pnl = (px - leg["entry"]) * sgn * leg["n"] * MGC_USD
                    self.s["gold"] |= {"status": "closed", "pnl": pnl}
                    when = f" ({t.astimezone(CH):%H:%M} Uhr){self.late(t)}"
                    self.once("gold_exit", (f"❌ GOLD STOP-LOSS getroffen @ {fmt(px)}{when}\nErgebnis Lucid 50K: <b>{usd(pnl)}</b>" if hit_sl else
                                            f"✅ GOLD TAKE-PROFIT erreicht @ {fmt(px)}{when}\nErgebnis Lucid 50K: <b>{usd(pnl)}</b> 🎉"))
                    break
            g["checked"] = (t + timedelta(minutes=1)).isoformat()
        # pre-alerts (current price close to a level, not yet triggered)
        if g["status"] == "pending":
            for side in ("long", "short"):
                L = g["legs"][side]
                dist = (L["entry"] - price) if side == "long" else (price - L["entry"])
                if L["n"] and 0 < dist <= PRE_ALERT_R * L["r"] and side not in g["alerted"]:
                    g["alerted"].append(side)
                    self.once(f"gold_pre_{side}",
                              f"⚠️ GLEICH {'LONG' if side == 'long' else 'SHORT'} – GOLD\nnoch ca. ${dist:.1f} bis {fmt(L['entry'])}\n"
                              f"{'BUY' if side == 'long' else 'SELL'} {L['n']} MGC  ·  TP {fmt(L['tp'])}  ·  SL {fmt(L['sl'])}")
        # reminders + time exit
        if self.after(13, 50):
            if g["status"] == "pending":
                self.once("gold_rem", f"⏰ GOLD: noch nicht ausgelöst – um {ch(self.day, 14, 0)} Uhr beide Orders löschen")
            elif g["status"] in ("long", "short"):
                leg, sgn = g["legs"][g["status"]], 1 if g["status"] == "long" else -1
                self.once("gold_rem", f"⏰ GOLD in 10 Min schliessen ({ch(self.day, 14, 0)} Uhr)\n"
                                      f"aktuell ca. {usd((price - leg['entry']) * sgn * leg['n'] * MGC_USD)}")
        if self.after(14, 0):
            if g["status"] in ("long", "short"):
                leg, sgn = g["legs"][g["status"]], 1 if g["status"] == "long" else -1
                pnl = (price - leg["entry"]) * sgn * leg["n"] * MGC_USD
                self.s["gold"] |= {"status": "closed", "pnl": pnl}
                self.once("gold_exit", f"⏹ GOLD JETZT SCHLIESSEN (Zeit) @ ca. {fmt(price)}\nErgebnis Lucid 50K: <b>{usd(pnl)}</b>")
            elif g["status"] == "pending":
                self.s["gold"]["status"] = "expired"
                self.once("gold_exit", "⏹ GOLD: heute nicht ausgelöst – beide Orders JETZT löschen")
        self.save()

    # ------------------------------------------------------------------ nasdaq
    def nas_levels(self, side, ratio, entry):
        qd = self.data.qqq_daily()
        pc = qd["close"].shift()
        tr = pd.concat([qd["high"] - qd["low"], (qd["high"] - pc).abs(), (qd["low"] - pc).abs()], axis=1).max(axis=1)
        risk = NAS_STOP_ATR * float(tr.rolling(14).mean().iloc[-1]) * ratio
        n = micros(risk, MNQ_USD)
        return {"side": side, "entry": entry, "sl": entry - side * risk, "tp": entry + side * NAS_TP_R * risk, "r": risk, "n": n}

    def nas_text(self, L, head):
        word = "BUY" if L["side"] > 0 else "SELL"
        return (f"{head}\n{LINE}\n{word} {L['n']} MNQ\n"
                f"TP {'+' if L['side'] > 0 else '−'}{L['r']*NAS_TP_R:.0f} Pkt ({round(L['r']*NAS_TP_R*4)} Ticks)  ≈ {fmt(L['tp'], 0)}\n"
                f"SL {'−' if L['side'] > 0 else '+'}{L['r']:.0f} Pkt ({round(L['r']*4)} Ticks)  ≈ {fmt(L['sl'], 0)}\n"
                f"Risiko {usd(-L['n']*L['r']*MNQ_USD)}  ·  Ziel {usd(L['n']*L['r']*NAS_TP_R*MNQ_USD)}\n{LINE}")

    def nas(self):
        n = self.s["nas"]
        if self.day.weekday() >= 5:
            return
        q = self.data.qqq()
        today = q[q.index.date == self.day]
        # prediction while the first 5-minute candle is still forming
        if self.after(9, 33, 40) and not self.after(9, 35, 30) and not n:
            part = today.between_time("09:30", "09:33")
            r = nq_ratio(self.data)
            if len(part) >= 3 and r:
                o, c = float(part["open"].iloc[0]), float(part["close"].iloc[-1])
                if abs(c / o - 1) < 0.0003:
                    self.once("nas_pre", "🔮 NAS-VORHERSAGE: noch unklar (Kerze fast flach) – Signal um 09:35 NY abwarten")
                else:
                    L = self.nas_levels(1 if c > o else -1, r, c * r)
                    if L["n"]:
                        self.once("nas_pre", self.nas_text(L, f"🔮 NAS-VORHERSAGE: wahrscheinlich <b>{'LONG' if c > o else 'SHORT'}</b>\n"
                                                              f"Bereit machen – Bestätigung kommt in ~1 Min ({ch(self.day, 9, 35)} Uhr)"))
        if self.after(9, 35, 5) and not n.get("status"):
            if self.after(9, 45):  # too late for a market entry at the backtested price
                self.s["nas"] = {"status": "off"}
                self.once("nas_late", "⚠️ NAS: Bot ist heute zu spät gestartet (GitHub-Verzögerung) – kein NAS-Trade heute")
                return
            first = today.between_time("09:30", "09:34")
            r = nq_ratio(self.data)
            if len(first) < 4 or not r:
                if self.after(9, 40):
                    self.s["nas"] = {"status": "off"}
                    self.once("nas_none", f"⚪ NAS {self.day:%d.%m.}: keine Kursdaten (Börse zu?) – kein Trade")
                return
            o, c = float(first["open"].iloc[0]), float(first["close"].iloc[-1])
            if o == c:
                self.s["nas"] = {"status": "off"}
                self.once("nas_none", "⚪ NAS: erste Kerze unverändert – heute kein Trade")
                return
            L = self.nas_levels(1 if c > o else -1, r, c * r)
            if L["n"] == 0:
                self.s["nas"] = {"status": "off"}
                self.once("nas_none", f"⚪ NAS: Signal {'LONG' if c > o else 'SHORT'}, aber Stop {L['r']:.0f} Pkt zu weit "
                                      f"(1 MNQ = ${L['r']*MNQ_USD:.0f} > ${RISK:.0f}) – überspringen")
                return
            self.s["nas"] = L | {"status": "open", "checked": ny_time(self.day, 9, 35).isoformat()}
            self.once("nas_signal", self.nas_text(L, f"{'🟢 LONG' if L['side'] > 0 else '🔴 SHORT'} JETZT – NASDAQ (Market-Order)")
                      + f"\nSL/TP gelten ab DEINEM Einstiegspreis · schliessen spätestens <b>{ch(self.day, 15, 59)} Uhr</b>")
        if n.get("status") == "open":
            bars = nq_bars(self.data)
            if bars is None or bars.empty:
                return
            checked = datetime.fromisoformat(n["checked"])
            price = float(bars["close"].iloc[-1])
            for t, b in bars[(bars.index >= checked) & (bars.index < ny_time(self.day, 15, 59))].iterrows():
                s = n["side"]
                hit_sl = b["low"] <= n["sl"] if s > 0 else b["high"] >= n["sl"]
                hit_tp = b["high"] >= n["tp"] if s > 0 else b["low"] <= n["tp"]
                if hit_sl or hit_tp:
                    px = n["sl"] if hit_sl else n["tp"]
                    pnl = (px - n["entry"]) * s * n["n"] * MNQ_USD
                    n |= {"status": "closed", "pnl": pnl}
                    when = f" ({t.astimezone(CH):%H:%M} Uhr){self.late(t)}"
                    self.once("nas_exit", (f"❌ NAS STOP-LOSS getroffen @ ca. {fmt(px, 0)}{when}\nErgebnis Lucid 50K: <b>{usd(pnl)}</b>" if hit_sl else
                                           f"✅ NAS TAKE-PROFIT erreicht @ ca. {fmt(px, 0)}{when}\nErgebnis Lucid 50K: <b>{usd(pnl)}</b> 🎉"))
                    break
                n["checked"] = (t + timedelta(minutes=1)).isoformat()
            if n["status"] == "open" and self.after(15, 50):
                self.once("nas_rem", f"⏰ NAS in 10 Min schliessen ({ch(self.day, 15, 59)} Uhr) – Markt schliesst\n"
                                     f"aktuell ca. {usd((price - n['entry']) * n['side'] * n['n'] * MNQ_USD)}")
            if n["status"] == "open" and self.after(15, 59):
                pnl = (price - n["entry"]) * n["side"] * n["n"] * MNQ_USD
                n |= {"status": "closed", "pnl": pnl}
                self.once("nas_exit", f"⏹ NAS JETZT SCHLIESSEN (Zeit) @ ca. {fmt(price, 0)}\nErgebnis Lucid 50K: <b>{usd(pnl)}</b>")
        self.save()

    # ------------------------------------------------------------------ summary
    def summary(self):
        if not self.after(16, 0) or "summary" in self.s["sent"]:
            return
        g, n = self.s["gold"].get("pnl"), self.s["nas"].get("pnl")
        if g is None and n is None:
            self.once("summary", f"📊 {self.day:%d.%m.}: heute keine Trades")
            return
        tot_path = STATE_DIR / "total.json"
        tot = json.loads(tot_path.read_text()) if tot_path.exists() else {"pnl": 0.0, "days": 0}
        day_pnl = (g or 0) + (n or 0)
        tot = {"pnl": tot["pnl"] + day_pnl, "days": tot["days"] + 1}
        tot_path.write_text(json.dumps(tot))
        lines = [f"📊 TAGESBILANZ {self.day:%d.%m.} (Lucid 50K, simuliert)"]
        if g is not None:
            lines.append(f"Gold: {usd(g)}")
        if n is not None:
            lines.append(f"NAS: {usd(n)}")
        lines += [LINE, f"Heute: <b>{usd(day_pnl)}</b>", f"Seit Start ({tot['days']} Tage): {usd(tot['pnl'])}"]
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
