# Trading signals: XAUUSD + NASDAQ (free, GitHub Actions + Telegram)

`watcher.py` runs every trading day in 3 GitHub Actions segments (about 06:50 – 22:05 Swiss time).

## Strategy (GitHub variable `STRATEGY`)
| Value | Winners | Idea | Backtest Nov 2023 – Nov 2026 (Lucid 50K, $300 per trade) |
|---|---|---|---|
| `balanced` (**default**) | ~60 % | XAUUSD London breakout + NASDAQ strong first 5-min candle; TP 0.25×ATR, SL 0.3×ATR | +$18,000, evaluation passed 65 % / failed 30 % |
| `hw` | ~70 % | same entries; TP 0.15×ATR, SL 0.3×ATR | +$8,600, passed 38 % / failed 51 % |
| `classic` | ~40 % | London breakout TP 4R, ORB5 TP 3R (biggest wins) | highest profit per risk, long losing streaks |

Higher win rates mean smaller targets, so trading costs eat most of the profit (see TradingLab/research_hw.py).

## Messages
- ~07:00 **XAUUSD OCO order**: BUY STOP and SELL STOP, each with TP / SL / quantity, separated by lines.
  If neither fills by 14:00, delete both. Close an open position by 20:00 at the latest.
- ⚠️ **GLEICH** when the price is about $3 away from a level, then 🟢/🔴 **ausgelöst** when it fills.
- ~15:34 🔮 NASDAQ prediction, ~15:35 🟢/🔴 **JETZT** with TP/SL prices and points.
- ✅ TP / ❌ SL with the $ result on Lucid 50K; ⏰ close reminders; 📊 daily balance with hit rate.

Prices are the Lucid futures prices (MGC for XAUUSD, MNQ for NASDAQ). "Menge" is the number of micro contracts.

## Data (free)
- QQQ in real time × ratio to NQ futures. Measured error: about 1.6 points.
- Coinbase PAXG in real time + basis to MGC. Measured error: about $1.

Yahoo futures data is 10 min delayed, so it is only used for the conversion. Your broker fills the orders at the exact levels.

## Setup
- Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- Variables (optional): `STRATEGY`, `RISK_USD` (default 300), `MAX_MICROS` (default 10).
- The repository is public, which gives unlimited free Action minutes.
- Test: Actions → Run workflow → `ping`, or `replay` + date (the whole simulated day appears in the log).
