# SwingBot: risk-first automated swing trading (NSE)

An automated swing trading bot for Indian stocks (NSE cash/delivery). It runs on a fixed daily routine:

1. After the market closes, it analyses every stock in its universe.
2. It picks only the best setups.
3. It sizes each trade with strict risk rules.
4. It places orders through your **Groww** or **Zerodha** account.
5. It protects every position with a broker-side stop-loss.
6. It reports everything to you on **WhatsApp**.

> ⚠️ **Read this first.**
> * No trading system wins every trade or guarantees profit. This one is built to **protect capital first** and to survive losing streaks.
> * The bot has been tested here on simulated data only. It has **not yet been backtested on real NSE history or connected to a real broker account**. Follow the go-live checklist below in order. Skipping steps is how people lose money with bots.
> * This is software, not investment advice. Trade only money you can afford to lose.

The full design (strategy, risk rules, why each one exists) is in **[docs/SWING_TRADING_BOT_PLAN.md](docs/SWING_TRADING_BOT_PLAN.md)**.

---

## 📊 Current status (4 Nov 2026): research results and the paper test

Every strategy was tested on real NSE data, after all Zerodha charges.

| Strategy | 2015–2022 (development) | 2023 → Nov 2026 (unseen data) | Verdict |
|---|---|---|---|
| Original swing bot | −0.18R per trade, −8.6% a year | — | ❌ loses money |
| Simplified swing (momentum, 2–20 days) | about break-even | — | ❌ below NIFTY |
| C: NIFTY trend ETF (10-month average) | 3.7% a year | 6.4% a year | ❌ below NIFTY |
| **B: Monthly momentum rotation (top 10)** | 25% a year, worst drop 27% | **14.5% a year, worst drop 28%** | ⚠️ beat NIFTY (6.8%) but failed 3 of 5 rules |
| NIFTY 50 buy & hold | 10.1% a year, worst drop 38% | 6.8% a year, worst drop 16% | benchmark |

R = the amount risked on one trade.

**What B failed:**
- worst drop above 20%
- its 2023–26 result reached only 58% of the 2015–22 result
- fewer than 100 trades
- in 2023–26 it beat randomly picked stocks by only about 1.5% a year

So B is **not validated for real money**.

**Machine learning (Upgrade 5, trained 2015–2022, tested on 10,455 trades from 2023–2026):**
- **AUC:** logistic regression 0.58, gradient boosting 0.58, the bot's score 0.54. The models rank trades slightly better than the score, and they did so in every test year.
- **After charges:** the models' best picks made only +0.01R to +0.05R a trade, against +0.02R for the score's best picks. That is nowhere near the +0.10R needed.
- **Why:** the models mainly learned to prefer tight stops. Target 1 is closer with a tight stop, so it gets hit more often (32% vs 24%), but charges eat a bigger share of a small move.
- **Verdict: not adopted.** The bot keeps the simple score. Details: `journal/ml_report.txt`.

**What the daily message is now:** a **forward paper test of Option B**, using the exact backtested rules. The bot runs a ₹10,000 paper portfolio by itself (`journal/paper_rotation.json`), so you have nothing to record. Each weekday's message shows:
- the market filter
- the holdings
- profit/loss against NIFTY since the start
- the drawdown check

The first review is after 3 months. If B beats NIFTY without breaching the 20% drawdown limit, we can discuss real money. Otherwise we stop.

**Second daily message: "Top 5 swing watchlist" (learning/paper only).** It lists the 5 highest-scoring liquid uptrend stocks, with at most 2 per sector, and gives watch-above and stop levels for each. Every list is saved in `journal/top5_history.csv`. After 10 trading days the bot reports how those picks actually did against NIFTY, so you can judge the list on real evidence.

To get a deep dive on any stock: GitHub → Actions → **Analyze stock** → type the NSE symbol(s). The report arrives on Telegram.

**Message style.** Telegram messages use a minimal terminal style:
- uppercase section headings and thin separators
- colour from status dots, since Telegram bots can't colour text: 🟢 positive / OK, 🔴 negative / risk, 🟡 caution, 🔵 entry, ⚪ neutral
- decisions, alerts and the disclaimer sit in Telegram quote blocks, which have a coloured bar
- aligned monospace tables, at most 32 characters wide so nothing wraps on a phone
- each topic arrives as its own message: market dashboard, Option B, watchlist, prediction tracker
- a long watchlist is split only between stock cards, never inside a table
- if Telegram ever rejects the formatting, the same words are re-sent as plain text
- WhatsApp gets the same text with WhatsApp markup

Each watchlist stock card has these sections: header, trading levels, technical data, fundamentals, historical performance, news & risk, final decision.

**FINAL DECISION** is derived from the existing rules; no new trading logic was added:
- **SKIP:** breaks a hard rule. The stop is wider than the 8% rule, or one share costs more than the per-stock limit.
- **WATCH ONLY:** the market filter is OFF, or uncertainty is HIGH (2+ conflicting signals).
- **BUY WATCH:** the market filter is ON and there is at most one conflict. Buy only above the entry, with the stop-loss order placed at once. Paper only.

**Daily messages, in order:**
1. 🌍 **Market dashboard:**
   - NIFTY trend and 52-week distance
   - breadth: % of NIFTY 500 stocks above their 200/50-day averages, advancing vs declining
   - India VIX and Bank NIFTY
   - strongest and weakest sectors
   - global cues: S&P 500, Nasdaq, Brent, USD/INR, gold, US 10Y
   - a plain "what it means" line
2. 📁 **Option B paper portfolio**
3. 📊 **Top 5 swing watchlist.** For each pick:
   - 🔥 **volume quality:** today's volume vs its 50-day average, and a verdict of ACCUMULATION, NEUTRAL or DISTRIBUTION. The verdict combines four measures:
     - Chaikin money flow
     - strong up-days vs strong down-days
     - the On-Balance Volume trend
     - up-volume vs down-volume
   - 📦 **delivery %** from NSE's daily bhavcopy, best effort. NSE often blocks cloud servers, and when it does the message says so instead of guessing.
   - 🎲 **historical odds:** how often similar past setups (same type, score band and market trend, NIFTY 500 since 2015) hit Target 1 before the stop
   - 🏢 **fundamentals:** market cap, P/E, ROE, D/E, profit growth, with flags
   - 📰 **news**
   - **uncertainty (LOW / MEDIUM / HIGH):** lists every conflicting signal for the pick: money flowing out, no historical edge, a fundamental red flag, results or legal news, or a data source that couldn't be checked. LOW means no conflicts, which is still not a guarantee.
4. 🔍 **Prediction tracker.** Every watchlist pick is a prediction: "if it trades above the entry, Target 1 comes before the stop". **Every session** the bot re-checks *every* past prediction against the real candles, using the same rules as the odds table:
   - statuses: waiting, not triggered, active, ✅ worked, ❌ failed (stop), ⏳ time-out after 20 sessions
   - a stock picked again while its earlier prediction is still open is not counted twice
   - the message shows today's changes, the open predictions with their current R, and a scorecard of hit rate and average R before and after charges, compared with what history predicted
   - everything is saved in `journal/predictions.csv`

   It starts with **🚨 UNCERTAINTY ALERTS** whenever something makes the predictions less trustworthy:
   - **Data:** stale or missing price data; news, delivery %, fundamentals or the odds table unavailable
   - **Open predictions:** a stop hit by a gap, a prediction near its stop, results or legal news on an open prediction, the market filter turning OFF
   - **Results so far:** 4+ stop-outs in a row; results statistically worse than history; a hit rate above 75%, which is too good to trust and points to bad data
   - **Market:** India VIX high or jumping, NIFTY moving ±2% in a day, the Option B paper drawdown passing 10%

   If the tracker itself ever fails, you get a 🔴 alert instead of silence.

**Mistake tracker.** Record every real trade with **Actions → Record trade** and fill in the optional *reason*, *emotion* and *followed plan* boxes. The bot checks the trade against its own rules and replies on Telegram with "📒 TRADE RECORDED" and a mistake check:
- **Buy checks:** off-plan stock, market filter OFF, chased above the entry, bought before the trigger, no stop or too-wide stop, risk above 1%, oversized position, revenge trade, overtrading, emotional entry
- **Sell checks:** stop not honoured, early exit out of fear, held too long

Every Saturday the **Weekly review** workflow sends a discipline score, your most common mistakes and one focus for next week. Trades are logged in `journal/trade_log.csv`.

**Machine-learning test (Upgrade 5).** Actions → **ML test** trains two simple models (logistic regression and small gradient-boosted trees) on 2015–2022. It then tests them on 2023 onwards, years the models never saw, and compares them with simply ranking by the bot's score, after charges. A model is used only if it passes all of these rules, which were written before the test:
- AUC ≥ 0.55
- its top-20% picks beat the score's top 20% by at least +0.10R
- those picks are profitable after costs
- its daily top-5 beats the score's daily top-5

The report is saved to `journal/ml_report.txt`.

The odds table (`journal/base_rates.csv`) is rebuilt every Saturday by the **Refresh odds** workflow. The odds are historical frequencies, not a prediction. They include today's-list survivorship bias and exclude charges.

To run your own research: GitHub → Actions → **Research** (`kind: portfolio` or `swing`). For each test it reports:
- top trades
- year-by-year returns
- a random-picks control
- a clean-data rerun

---

## 🆓 ₹0 swing trading: free signal mode

You don't need a paid server or a paid broker API. In **signal mode**:
1. The bot does all the analysis and risk management.
2. It sends you the exact orders on WhatsApp.
3. You place them in the Zerodha or Groww app (about 2 minutes a day).

It uses the same strategy, scoring and risk rules as the automatic mode.

| Cost item | Free option | Cost |
|---|---|---|
| Brokerage | **Zerodha: ₹0 on delivery trades.** Swing trades are delivery trades. (Groww charges ₹5–20 per order.) | ₹0 |
| Broker API | Not needed: you place the orders yourself | ₹0 |
| Server / static IP | Not needed: runs on **GitHub Actions** (free) or your own laptop | ₹0 |
| Market data | Yahoo Finance end-of-day data | ₹0 |
| WhatsApp alerts | CallMeBot | ₹0 |
| Govt taxes and DP charge | STT 0.1% each side, stamp duty, exchange fees, about ₹16 DP charge per sell. **No broker can waive these.** | about ₹25 on a ₹4,000 trade |

### What a daily message looks like

```
SWING SIGNALS - data to Wed 03 Nov
Market: BULL | breadth 87%
Equity ₹10,228.79 (from peak -3.4%) | risk profile micro

YOUR POSITIONS (1)
- TATASTEEL 24 sh @ ₹165.20, now ₹171.90 (+1.20R), stop ₹165.80
   >> MODIFY your GTT: stop trigger ₹165.80, stop limit ₹157.51, target ₹183.00

NEW TRADES FOR NEXT SESSION
1) HAL - pullback, score 86/100
   STEP 1 (after 9:30 AM): Stop-Limit BUY, Delivery/CNC, qty 1, trigger ₹4,222.86, limit ₹4,243.97.
          Day order - expires by itself if not triggered.
   STEP 2 (as soon as it fills): GTT OCO SELL 1 - stop trigger ₹4,077.78, target ₹4,658.10.
   STEP 3: record it -> took HAL 1 <fill price>
   Risk ₹145, reward:risk 3.0, est. charges ₹26
```

(Illustration only; the prices are made up.)

### Your daily routine

1. **About 4:45 PM:** the WhatsApp message arrives.
2. **Next morning after 9:30:** place the Stop-Limit BUY it lists. If the price never reaches the trigger, nothing happens and the order expires at the close.
3. **When it fills:** immediately set the **GTT OCO**, which holds the stop-loss and the target together. In Kite this is *GTT → OCO*.
4. **Record the fill:**
   * **On a laptop:** `python -m swingbot took HAL 1 4225`
   * **On a phone (GitHub setup):** GitHub app → Actions → *Record trade* → Run workflow. Answer the *emotion* and *followed plan* questions honestly; the mistake check and the weekly review depend on them.
5. **Exits:**
   * Stop-loss and target exits happen by themselves through your GTT. The bot notices them from the price data and updates your journal.
   * When the message says **SELL at tomorrow's open** (trend break, time stop, results ahead), sell at market after 9:15. Then record it: `sold HAL <price>`.
   * When it says **MODIFY your GTT**, raise the stop in the app. Never move a stop down.

### If WhatsApp (CallMeBot) doesn't reply

CallMeBot is a free shared bot, and it **changes its phone number from time to time**.
* Open **https://www.callmebot.com/blog/free-api-whatsapp-messages/** in your browser and use the number shown there.
* Send the activation message again. If there is no reply within 2 minutes, try again after 24 hours.
* `CALLMEBOT_API_KEY` must be the number it sends back to you. Every run now prints CallMeBot's reply in the log, and the run turns red if the key is wrong.

**Free, reliable backup: Telegram**
1. In Telegram, open **@BotFather**, send `/newbot`, and copy the **token** it gives you.
2. Send any message (for example "hi") to your new bot.
3. In GitHub, add the secret `TELEGRAM_BOT_TOKEN` and the **variable** (Variables tab) `NOTIFY_PROVIDER` = `telegram`.
4. Run the workflow within 24 hours of sending "hi". The bot finds your chat ID by itself and remembers it in `journal/telegram.json`. A `TELEGRAM_CHAT_ID` secret is optional.

### Option 1: run it free on GitHub (no PC needed)

1. Merge this branch into your repo's default branch. Scheduled workflows only run there.
2. In the repo, go to **Settings → Secrets and variables → Actions** and add two secrets:
   * `WHATSAPP_PHONE` (for example `+9198XXXXXXXX`)
   * `CALLMEBOT_API_KEY` (see WhatsApp setup below)
3. Done. `.github/workflows/swing-signals.yml` runs Monday to Friday at 4:45 PM IST and saves your journal in `journal/`.
   * Free GitHub accounts include enough minutes for this: about 2 minutes a day.
   * GitHub can start scheduled runs 10–30 minutes late.

### Option 2: run it free on your laptop

```bash
pip install pandas numpy PyYAML requests yfinance
python -m swingbot signals          # run once after 3:30 PM; schedule it daily:
                                    #   Windows: Task Scheduler -> scripts\run_signals.bat (Mon-Fri 4:45 PM)
                                    #   Linux/Mac: cron -> scripts/run_signals.sh
python -m swingbot journal          # your positions and results
```

Before trusting any signal, run the backtest on real history (`python -m swingbot backtest --start 2015-01-01 --capital 1000000`). Only follow the signals if it says **READY FOR PAPER TRADING**.

### Fully automatic and still free?

That is possible, with two catches:
* **Broker API:** Zerodha's *Kite Connect Personal* API is free for orders and GTT. It needs **one login tap every morning**.
* **Static IP:** since April 2026, SEBI requires API orders to come from a **registered static IP**. The only ₹0 route is Oracle Cloud's *Always Free* VM with a reserved IP, which works but is often out of capacity in Indian regions.

Groww's free API plan excludes live data (₹499/month for everything). The bot doesn't need live data, so the free plan should be enough, but check its current terms in the app.

Use signal mode now, and switch to the automatic mode (the rest of this README) if you get a free static-IP server.

---

## Your configuration (decided with you)

| Item | Setting |
|---|---|
| Brokers | **Zerodha recommended for ₹0 trading** (no delivery brokerage, free API). Groww is supported too: automatic login, but brokerage is charged on every order |
| Trial capital | **₹10,000**. The bot never commits more than this plus its own profits, even if your account holds more |
| Automation | Fully automatic |
| Stocks | Liquid NIFTY 500 stocks; only the highest-scoring setups are traded |
| Alerts | WhatsApp |

**Risk settings for the ₹10,000 trial (the "micro" profile):**

| Rule | Value | In rupees |
|---|---|---|
| Risk per trade, bull market | 1% (your choice) | ₹100 |
| Risk per trade, neutral market | 0.5% | ₹50 |
| Risk per trade, bear market | 0 (no new trades) | — |
| Max position size | 50% | ₹5,000 |
| Max open positions | 2 | — |
| Max total open risk ("heat") | 2% | ₹200 |
| Daily loss limit | 2% | stops new entries for the day |
| Weekly loss limit | 4% | stops new entries for the week |
| Full stop | 15% below peak | bot halts at ₹8,500 until you review and resume |

On a ₹10,000 account, fixed charges hurt more: the DP charge alone is about ₹16 on every sell. So the bot skips a trade when charges would eat more than 10% of the expected reward. The limits switch automatically as the account grows:

| Account size | Profile | Risk per trade |
|---|---|---|
| Below ₹50,000 | micro | 1% (max 2 positions) |
| ₹50,000 to ₹5 lakh | small | 1% (max 5 positions) |
| Above ₹5 lakh | standard | 1% (max 8 positions) |

Hard ceilings in the code (2% risk per trade, 50% per position, 8% total heat) cannot be overridden by the config file.

---

## How it trades (one day)

| Time (IST) | What the bot does |
|---|---|
| 08:45 | Logs in to the broker and compares its records with your real holdings. Checks that **every position has a live stop-loss at the broker**. |
| 09:17 | Sells positions flagged for exit last night (trend break, time stop, results tomorrow). Also sells anything that opened below its stop. |
| 09:30 | Places tonight's planned trades as stop-limit BUY orders. Each one fills only if the price rises above the trigger, and never above the limit price. |
| Every 2 min | When a buy fills, it **immediately places an OCO order**: a stop-loss and a target together at the broker. That stop keeps working even if the bot or server goes down. It also watches the kill switch and the daily-loss limit. |
| 15:20 | Cancels unfilled entries for the day. |
| 16:30 | Downloads fresh data and reads the market regime. Raises trailing stops. Scans about 130 to 500 stocks. Picks tomorrow's trades. Sends your **WhatsApp daily report**. |

**Entry setups:**
* **Pullback in an uptrend:** price dips to the 20 EMA / 50 SMA on light volume, then turns back up.
* **Base breakout:** price breaks out of a tight 3 to 10 week range on 1.5× or more volume, near its 52-week high.

**Exits:**
* stop-loss
* stop moved to breakeven at +1R (R = the amount risked on the trade)
* trailing stop (chandelier, or a close below the 20 EMA)
* target at the nearest resistance, or 3R
* time stop after 10 days with no progress
* exit the day before quarterly results
* stops tightened in a bear market

---

## Setup guide

### 1. Get a server with a static IP (SEBI rule)

Since **1 April 2026**, SEBI requires API orders to come from a **static IP registered with your broker**. Rent a small Linux VPS in India with a static IP; 1 GB RAM is enough. Examples are AWS Lightsail Mumbai or DigitalOcean Bangalore with a reserved IP. Then register that IP in your broker's API settings.

### 2. Install

```bash
git clone <this repo> && cd New-Projects-
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config/.env.example .env      # then edit .env
```

### 3. Broker API access

**Groww (recommended):**
* In the Groww app or web, open *Trading APIs* and create an API key of type **TOTP**.
* Put the key and its TOTP secret into `.env`.
* The bot logs itself in every morning, so no manual step is needed. That is why it suits full automation.
* Check the current API subscription price in the Groww app.

**Zerodha:**
* Create a Kite Connect app. The free *Personal* plan is enough: orders, GTT and portfolio access.
* Set the redirect URL, then put the API key and secret into `.env`.
* Kite needs **one login tap every morning**. The bot sends you the link on WhatsApp. After logging in, run `python -m swingbot kite-login <request_token>`.
* Until you log in, your existing stop-losses still protect you; the bot just won't open new trades that day.

### 4. WhatsApp alerts (free option)

1. Save **+34 644 66 32 62** in your phone and send it the message: `I allow callmebot to send me messages`.
2. Put the API key it replies with into `.env` as `CALLMEBOT_API_KEY`.
3. Put your number into `.env` as `WHATSAPP_PHONE=+91XXXXXXXXXX`.
4. Run `python -m swingbot test-whatsapp`.

### 5. Universe

```bash
python -m swingbot update-universe      # NIFTY 500 list from NSE (optional; a ~135-stock default list is included)
```

---

## Go-live checklist (do these in order)

**Step 1: Backtest on real data.** Use a large notional capital so there are enough trades to judge the strategy fairly:

```bash
python -m swingbot backtest --start 2015-01-01 --capital 1000000
python -m swingbot backtest --start 2015-01-01 --capital 10000     # see how the ₹10k profile behaves
```

Continue only if the report says **READY FOR PAPER TRADING**:
* at least 200 trades
* profit factor of at least 1.5
* expectancy of at least +0.25R per trade
* max drawdown of 20% or less
* 95th-percentile Monte Carlo drawdown of 30% or less
* risk-adjusted return better than buying and holding the index

If it fails, **do not trade it live**. Tell me, and we'll look at which setups or rules are losing.

**Step 2: Paper trade for 1 to 3 months.** Set `mode: paper` and `broker: paper` in `config/settings.yaml`, then run:

```bash
python -m swingbot run
```

It uses real market data and simulated orders. Compare the WhatsApp reports and `python -m swingbot status` with what you'd expect.

**Step 3: Run the broker preflight.** This is read-only and places no orders. Set `broker: groww` (or `zerodha`), then run:

```bash
python -m swingbot preflight
```

Check that `parsed_cash` and `parsed_holdings` match your app exactly. Groww doesn't publish its response formats, so this is where any mismatch shows up.

**Step 4: Go live with ₹10,000.**
1. Set `mode: live` and `live_capital_limit: 10000`.
2. Set `SWINGBOT_CONFIRM_LIVE=YES` in `.env`.
3. Run it as a service (see below).
4. Watch the first week closely.

**Step 5: Scale slowly.** Add capital only after 1 to 2 months where live results match paper trading.

### Run as a service (auto-restart)

```bash
sudo cp scripts/swingbot.service /etc/systemd/system/   # edit the paths/user inside first
sudo systemctl enable --now swingbot
journalctl -u swingbot -f
```

---

## Commands

| Command | What it does |
|---|---|
| `python -m swingbot scan` | Today's best setups with full score breakdown and the risk-engine verdict (no orders) |
| `python -m swingbot backtest [--synthetic]` | Historical test with metrics and the go-live acceptance check |
| `python -m swingbot run` | Start the bot (paper or live, as configured) |
| `python -m swingbot status` | Equity, positions, stops, plans, recent events |
| `python -m swingbot kill` | **KILL SWITCH:** no new orders; stop-losses stay active |
| `python -m swingbot kill --flatten` | Kill switch plus sell every position **the bot** opened (never your own shares) |
| `python -m swingbot resume` | Turn trading back on after a kill or the 15% drawdown halt (asks you to confirm) |
| `python -m swingbot preflight` | Read-only broker check |
| `python -m swingbot job <name>` | Run one scheduled job now (for testing) |
| `python -m swingbot took SYM QTY PRICE [--emotion fomo --followed-plan no --send]` | Record a buy plus the mistake check |
| `python -m swingbot sold SYM PRICE [--reason "stop hit" --send]` | Record a sell plus the mistake check |
| `python -m swingbot weekly-review [--send]` | Weekly discipline review |
| `python -m swingbot ml-test [--send]` | Machine-learning out-of-sample test vs the simple score |

## Safety features

* **Broker-side stops on every position.** If placing the OCO fails, the bot uses a day stop-loss order instead. If that also fails, it exits at market. It never holds an unprotected position.
* **Never touches your own investments.** It only manages shares it bought and skips stocks you already hold.
* **Capital cap.** It never commits more than `live_capital_limit` plus its own profits.
* **At-most-once orders.** The intent to trade is recorded before every order, so a crash or restart can't place the same order twice.
* **Circuit breakers:**
  * daily, weekly and monthly loss limits
  * full halt at 15% drawdown
  * half size after 3 losses in a row
  * half size when equity is below its 20-trade average
  * half size when the earnings calendar is unavailable
* **Kill switch:** the `state/KILL` file blocks every new order. It turns on automatically after 5 consecutive job failures.
* **Stale-data guard:** no new trades if today's data didn't arrive.
* **Live latch:** live mode refuses to start without `SWINGBOT_CONFIRM_LIVE=YES`.

## Honest limitations

* **Costs vs. a ₹10,000 trial.** The VPS (about ₹400 to ₹800 a month) and any broker API fee can cost more than a ₹10,000 account earns. Treat the trial as **proving the system works with real orders**, not as a way to make money. The economics improve with more capital.
* **Data.** Market data comes from Yahoo Finance by default (free and unofficial). The bot validates it and stops if it's stale. You can switch to `data.provider: groww`.
* **Earnings dates.** These are fetched from NSE on a best-effort basis. If that fails, the bot trades at half size. You can add dates manually in `config/events.csv`.
* **Charges.** The cost settings are approximate. Check them against your contract notes and update `costs:` in `settings.yaml`.

## Tests

```bash
pytest -q        # 97 tests: risk rules, exits, costs, setups, backtester, engine, signal mode, odds, volume, mistakes, ML, prediction tracker
```
