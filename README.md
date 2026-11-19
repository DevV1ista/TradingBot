# Trading signals: XAUUSD + NASDAQ for FTMO (free, GitHub Actions + Telegram)

`watcher.py` runs every trading day in 3 GitHub Actions segments (about 06:50 – 22:05 Swiss time) and
tracks a simulated FTMO account that follows every signal exactly.

## Setup (GitHub → Settings → Secrets and variables → Actions)
- Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- Variables (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `BROKER` | `ftmo` | `ftmo` = CFD prices + lots + FTMO tracking · `lucid` = futures + micros |
| `STRATEGY` | `classic` | `classic` (~40 % winners, big wins) · `balanced` (~60 %) · `hw` (~70 %) |
| `ACCOUNT_SIZE` | `100000` | FTMO account size |
| `RISK_PCT` | `0.5` | risk per trade in % of the account |
| `LOT_USD_XAU` | `100` | $ per $1 gold move per lot (FTMO XAUUSD: 100 oz) |
| `LOT_USD_US100` | `1` | $ per index point per lot. **Check US100.cash → Specification in MT5** |

## Messages
- ~07:00 **XAUUSD OCO order** (BUY STOP / SELL STOP with TP, SL, lots); ⚠️ **GLEICH** about $3 before a level; 🟢/🔴 **ausgelöst**.
- ~15:34 🔮 NASDAQ prediction (only when the candle is already clear); ~15:35 🟢/🔴 **JETZT**.
  No trade when QQQ and the Nasdaq-100 index disagree: such a near-flat first candle is a coin flip.
- ✅ TP / ❌ SL / ⏹ time exit, each with the result and the FTMO account block:
  account number, phase, balance before → after, distance to the target, loss limit, and payout status.
- 22:00 📊 daily balance: today, account status, all-time trades / hit rate / payouts / fees.

### Account tracking rules (FTMO 2-step)
- Phase 1: +10 %. Phase 2: +5 %. Both need at least 4 trading days.
- Max daily loss: 5 %. Max loss: 10 % of the start balance.
- Funded: a payout is possible 14 days after the start or after the last payout. You get 80 % of the profit, and the first payout also refunds the fee.
- Phase changes, new accounts and payouts take effect at the daily summary.
- The bot assumes you request every possible payout and buy a new challenge after a loss.
- To restart the tracking, delete `state/account.json`.

## Data (free, real-time)
- NASDAQ: QQQ 1-min × ratio to the Nasdaq-100 index (^NDX). Measured error: about 2 points.
- XAUUSD: Coinbase PAXG 1-min + basis to the Swissquote spot quote. Measured error: about $1.

Your broker fills the orders at the exact levels; the alerts are informational.

## Test
Actions → trading signals → Run workflow:
- `ping` sends a Telegram test message.
- `replay` + date simulates a past day; the output appears in the log.
