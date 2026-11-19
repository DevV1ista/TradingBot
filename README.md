# Trading signals (free, GitHub Actions + Telegram)

`watcher.py` runs every trading day in 3 GitHub Actions segments, from 06:50 to 22:05 Swiss time. It sends:

| When (CH) | Message |
|---|---|
| ~07:00 | 🟡 **GOLD OCO order**: BUY STOP and SELL STOP with TP/SL, separated by lines |
| any time | ⚠️ **GLEICH LONG/SHORT**: price is close to a gold level, get ready |
| any time | 🟢/🔴 **ausgelöst**: the gold order filled |
| ~15:34 | 🔮 **NAS prediction**: the first candle is still forming |
| ~15:35 | 🟢/🔴 **LONG/SHORT JETZT**: market order MNQ, TP/SL in points + ticks |
| any time | ✅ TP / ❌ SL hit, with the $ result on a Lucid 50K account |
| 19:50 / 21:50 | ⏰ close reminders; 20:00 / 21:59 close now |
| 22:00 | 📊 daily balance + running total |

(The Swiss times shift by 1 hour in the weeks when US and EU daylight-saving dates differ; the messages always show the correct time.)

## Data
Free sources:
- Yahoo QQQ (real-time) × ratio to NQ futures. The ratio comes from Yahoo futures, which are 10 min delayed. Measured error: about 1.6 points.
- Coinbase PAXG (real-time) + basis to MGC futures. Measured error: about $1.

Your broker fills the orders at the exact levels; the alerts are informational.

## Setup
- Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- Variables (optional): `RISK_USD` (default 400), `MAX_MICROS` (default 10).
- The repository must be **public** for unlimited free Action minutes.
- Test: Actions → trading signals → Run workflow → `ping`, or `replay` with a past date. The replay prints the whole simulated day in the log.
