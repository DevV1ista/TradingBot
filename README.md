# Trading signals (free, GitHub Actions + Telegram)

GitHub runs `signal_bot.py` on schedule. You need no PC and no server.

| Signal | Swiss time | What you do |
|---|---|---|
| Gold London breakout | ~07:00 | place the BUY STOP and SELL STOP (1 MGC each) with SL/TP as OCO; at 20:00 cancel what is left and close |
| NAS ORB5 | ~15:35 | market order MNQ with SL/TP in ticks as sent; close at 21:59 if still open |

(The Swiss times shift by 1 hour in the weeks when US and EU daylight-saving dates differ; each message shows the correct time.)

## Setup (once)
1. Repository → **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
2. Same page, tab **Variables** (optional): `RISK_USD` (default 400) and `MAX_MICROS` (default 10).
3. **Actions** tab → "trading signals" → **Run workflow** with job `ping`. A test message should arrive.

## Notes
- Data source: Yahoo Finance, free and unofficial. QQQ is used for the ORB direction (it matched the Nasdaq CFD on 19 of 20 test days). GC futures are used for gold, which is the same price as MGC.
- GitHub can start scheduled jobs a few minutes late. The bot starts about 20 min early and waits. If it is still too late, it sends "no signal today" instead of a stale signal.
- A private repository has 2,000 free Action minutes per month. This bot needs about 1,000.
- Backtest and full analysis: see the TradingLab folder on the PC.
