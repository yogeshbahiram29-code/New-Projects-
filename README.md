# Swing Trading Bot

An automated swing trading system that analyses stocks in depth before every trade and puts risk management first.

**Current stage:** design plan, waiting for review. No trading code yet.

👉 Read the full plan: [docs/SWING_TRADING_BOT_PLAN.md](docs/SWING_TRADING_BOT_PLAN.md)

## How it works

1. Filter the market down to liquid, tradeable stocks
2. Read the market regime (bull / neutral / bear); no new longs in bear markets
3. Analyse and score each stock 0–100 (trend, relative strength, volume, volatility, support/resistance, events)
4. Look for proven entry setups (trend pullback, base breakout, oversold-in-uptrend)
5. Risk engine with veto power: 1% risk per trade, minimum 2:1 reward-to-risk, 6% max portfolio heat, loss limits and a kill switch
6. Place broker-side stop-losses immediately, then manage exits (breakeven, partial profits, trailing stops, time stops)
7. Journal every trade and report via Telegram

> This is a software tool, not investment advice. Trading involves risk of loss.
