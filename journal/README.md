# Journal (free signal mode)

The bot keeps your swing-trading records here as plain files:

| File | What it holds |
|---|---|
| `my_positions.csv` | Your open positions, with stop, target and trailing state |
| `closed_trades.csv` | Every finished trade, with P&L after charges and the R-multiple |
| `plans.json` | The latest trade instructions; `took` reads stop and target from here |
| `account.json` | Equity, peak, drawdown and losing streak, used by the circuit breakers |

You can fix a wrong fill price by editing `closed_trades.csv`.
