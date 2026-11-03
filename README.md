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

## 🆓 ₹0 swing trading: free signal mode (start here)

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
   * **On a phone (GitHub setup):** GitHub app → Actions → *Record trade* → Run workflow
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
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in your browser and copy the number after `"chat":{"id":`.
4. In GitHub, add the secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, and the **variable** (Variables tab) `NOTIFY_PROVIDER` = `telegram`.

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
| Risk per trade, bull market | 2% | ₹200 |
| Risk per trade, neutral market | 1% | ₹100 |
| Risk per trade, bear market | 0 (no new trades) | — |
| Max position size | 50% | ₹5,000 |
| Max open positions | 2 | — |
| Max total open risk ("heat") | 4% | ₹400 |
| Daily loss limit | 2% | stops new entries for the day |
| Weekly loss limit | 4% | stops new entries for the week |
| Full stop | 15% below peak | bot halts at ₹8,500 until you review and resume |

Why the trial uses 2% and not 1%: on a ₹10,000 account, fixed charges hurt more. The DP charge alone is about ₹16 on every sell. With fewer, slightly larger positions, charges stay small compared with the expected profit. These limits switch automatically as the account grows:

| Account size | Profile | Risk per trade |
|---|---|---|
| Below ₹50,000 | micro | 2% |
| ₹50,000 to ₹5 lakh | small | 1.25% |
| Above ₹5 lakh | standard | 1% |

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
pytest -q        # 41 tests: risk rules, exits, costs, setups, paper broker, backtester, end-to-end engine, free signal mode
```
