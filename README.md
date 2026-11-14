# Rotman LT3 Trading Desk

Local browser dashboard and tender execution for the **RITCxDublin Liability Trading** simulation. Python 3.10 or newer. The dashboard and backtest use the standard library.

## Start a rehearsal

```powershell
git clone https://github.com/lekemog/rotman-lt3-bot.git
cd rotman-lt3-bot
py -3 dashboard.py
```

On a MacBook, run `python3 dashboard.py`. The desk opens at `http://127.0.0.1:8765`. Under **Rehearsal setup**, select a scenario and restart the heat. **Review** is the initial selection. Arm it and approve an eligible offer to see acceptance, fills and inventory reconciliation. Select **Auto** before arming to let the bot accept eligible tenders automatically.

The **Strategy** editor applies a validated profile between polling iterations. Changes apply to the next decisions and child orders. Submitted orders keep their original terms. Profiles and activity are stored under `.rotman/`. The P&L card shows completed tenders using observed fill VWAP and the configured fee model. Use the RIT blotter and scoreboard for official figures.

## Connect to RIT

| Computer | Practice monitor | Connection |
| --- | --- | --- |
| Windows | `py -3 dashboard.py --windows --record` | Start the RIT Client, join the session and enable its local REST API. Enter the Client API key when prompted. Local API port defaults to `9999`. |
| MacBook | `python3 dashboard.py --mac --record` | Use the RIT Mac app or web client for the blotter. The bot connects to Rotman's DMA server. Enter your RIT trader ID and password when prompted. The published Dublin practice port is `16540`. |

Use `--rit-port PORT` for a custom Windows local API port or the Mac DMA port assigned for the event. Rotman documents HTTP for DMA. Use `--dma-https` if the organisers supply a TLS-enabled DMA port. Terminal credentials stay in process memory. The dashboard itself listens only on localhost. Rotman recommends the Windows Client REST API when available.

During practice, compare tender direction, size, expiry and fees with the RIT dialog and transaction log. Verify both connections on the practice session. Once the organisers permit API order submission, append `--enable-orders`, confirm permission in the dashboard, and arm the selected entry mode from a flat account. Use one desk instance per trader. Re-arm after a heat change or feed outage.

The desk polls every 0.55 seconds when nothing is live, and faster while a tender or inventory is on the clock: every 0.2 seconds on Windows and 0.4 on a MacBook. Set these with `--interval` and `--busy-interval`. If RIT answers with a rate limit, the desk stays armed, waits as told, sends the refused request again and polls 0.1 seconds slower from then on. Use `--book-depth N` to request more than 50 orders per side if `review.py` shows large tenders refused because displayed depth could not cover the block. Use `--state-dir` for a separate rehearsal or practice profile. Use `--no-open` to open the local URL yourself. The original `--rit` flag remains a Windows alias.

### Measure the Mac connection before a heat

Run the read-only probe while an LT3 practice heat is active. It prompts for RIT credentials in the terminal and reports the initial clock, account and books, and clock recheck timings.

```sh
python3 rotman_lt3.py probe dma --samples 10
```

Compare the fresh-snapshot counts and timings on Wi-Fi and a phone hotspot if the desk is slow. A paused or stopped heat cannot test active-feed freshness. The Mac reader fetches account, tenders and both books concurrently between two clock checks. It keeps the 1.5-second and one-tick freshness checks and honours DMA rate-limit backoff.

## Strategy and controls

- **Entry.** Score the whole block against executable bids or asks, fees, depth loss and an adverse move. For each tender, the minimum time left is the greater of the fixed floor and `closeout reserve + ticks per child × minimum child orders`. The desk accepts at most one tender at a time and waits for RIT to report the resulting signed position.
- **Review or Auto.** Review accepts only a tender you approve, after checking the next fresh book against the active strategy. Skip removes a specific offer from this desk for the heat. Auto accepts qualifying offers without per-offer input. Pause before switching modes.
- **Adjust the strategy.** Defensive entry stages higher liquidity and price stress. Earlier exits stages a longer exit buffer. Review the proposed changes and select **Apply changes**. Use **Fine-tune strategy** for individual settings.
- **Unwind.** Use a single inventory-reducing child order at a time, capped at 25,000 shares for CRZY or 10,000 for TAME. Poll the order by ID, cancel a resting remainder, and reconcile the final fill with the position before submitting again. The default is marketable limits with a one-cent price cushion. A market closeout window is available in the strategy editor and starts disabled.
- **Pause entries.** Stop accepting tenders and continue managing confirmed inventory.
- **Take over in RIT.** Stop entries and new unwind orders. Request cancellation of the desk's known working child when RIT data is available. Check the blotter and manage any remaining inventory in RIT. A request already in flight may complete before the desk can stop.
- **Review in RIT.** After an ambiguous order response or an inventory mismatch, inspect the RIT position and blotter. Reset is available once the account is flat and working orders have cleared. The desk does not retry an ambiguous submission.

The entry defaults include a 10% depth reduction, one-cent adverse move and four-tick tender expiry buffer. The initial unwind budget is six ticks per minimum child plus a ten-tick reserve. These settings were chosen for rehearsal and need calibration on recorded RIT fills. In particular, the synthetic tests showed significant residual inventory during a late liquidity drought even with the longer entry budget.

### Settings added after the practice runs

All five start at 0, which leaves the desk behaving as it did in the runs below. [STRATEGY.md](STRATEGY.md) explains when to turn them on.

- **Book refill between orders.** The entry model prices the exit as separate maximum-size orders, with this percentage of consumed depth back before each one. At 0 it prices one sweep of the whole block.
- **Sweep band per order.** A child order takes only the liquidity displayed within this many cents of the touch. The next poll prices a fresh book. Full-size orders return when the exit time budget runs short. At 0 every child takes the full order size.
- **Work a block out over.** The desk works the block along a straight line to flat over this many ticks. On or ahead of the line it rests a maximum-size order at the touch on its own side, one cent inside the spread when there is room, for two seconds. Behind the line it takes only the shortfall from the other side. It returns to full-speed unwinding when the exit time budget runs short, when the next acceptable tender is live, and in the market closeout window. At 0 the block is unwound at once.
- **Auto patience.** Auto takes an eligible tender only once its stressed profit exceeds this many standard deviations of the mid over the ticks still available, and takes any eligible tender on its last acceptable tick. Volatility is measured from the current heat. Review is never held back by it. The offer card shows what Auto is holding for and the best stressed figure the tender has shown. **Review-style timing** stages a patience of 1.
- **Hole guard.** When the best quote the desk would hit is more than this many hundredths of a percent through the median mid of the last ten ticks, another trader has probably swept that side. The desk rests an order on its own side of the spread until the book refills, and goes back to hitting quotes when the exit time budget runs short. **Erratic traders** stages a guard of 100 (1%) and an 8-tick market closeout. The rehearsal scenario *Erratic manual traders* shows it working.
- **Work the exit** stages a 15-tick worked exit, a refill of 75%, an adverse move of at most 2¢ and an 8-tick market closeout.
- **Capture more** stages a refill of 50%, an adverse move of at most 2¢, 4 ticks per order and a market closeout in the last 8 ticks for you to review and apply.

A child order that has filled and reconciled with the position is now followed by the next one on the same poll.

## Review a recorded session

```sh
python3 review.py
```

Reads `.rotman/snapshots.jsonl`, `activity.jsonl` and `strategy.json` from a session run with `--record`. For each tender it prints the premium against the mid, the profit from one immediate sweep, whether the desk took it and why not, and for completed tenders the exit cost against the mid. It then lists the declined tenders that the Capture more settings would accept, and compares your entries with entering the same tenders at their first eligible tick. Tenders that stayed on screen for their whole window are replayed under Auto patience 1 and 2. A heat recorded in monitor mode gives the most tenders for that replay. Use `--state-dir` for another folder.

## Verify or replay

```powershell
py -3 -m unittest -v
py -3 rotman_lt3.py self-test
py -3 rotman_lt3_backtest.py --heats 250
py -3 rotman_lt3.py replay .rotman/snapshots.jsonl
```

The automated tests use a local RIT HTTP mock. The backtest generates books, tender arrivals, latency and partial fills. Historical validation requires real practice snapshots and the transaction log. `--record` saves dashboard snapshots for comparison with that log.

## Results

| Run | P&L | Field average | Best | Traders |
|---|---:|---:|---:|---:|
| 1 | 37,970.76 | 26,332.62 | 80,373.92 | 6 |
| 3 | 57,412.43 | 21,740.32 | 124,734.39 | 14 |
| 4 | 24,769.50 | 8,530.67 | 31,930.04 | 12 |
| 5 | 43,771.12 | -7,879.06 | 106,902.75 | 11 |
| 6 | 51,136.40 | 18,455.36 | 126,184.00 | 11 |
| 7 | 43,832.80 | 23,192.95 | 99,878.00 | 12 |
| 8 | 34,848.59 | 17,911.79 | 117,015.22 | 12 |

## Settings used

| Setting | Value |
|---|---|
| Depth removed (%) | 6 |
| Adverse move ($/share) | 0.03 |
| Minimum edge ($/share) | 0 |
| Expiry buffer (ticks) | 2 |
| Minimum time left (ticks) | 8 |
| Budget per order (ticks) | 6 |
| Exit reserve (ticks) | 8 |
| Limit price cushion ($) | 1 |
| Market closeout window (ticks, 0 disables) | 0 |
| Market fee ($/share) | 0.02 |
| Tender fee ($/share) | 0 |

## Rotman references

- [RITCxDublin event page](https://www.rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/finance-research-and-trading-lab/events/ritcx/ritcx-cards/tcd/)
- [Liability Trading case brief](https://rotmanfrtl.github.io/RITCx-Liability%20Trading%20Case.pdf)
- [RIT Client REST API 1.0.5](https://rit.306w.ca/RIT-REST-API/1.0.5/)
- [RIT DMA REST API 1.0.5](https://rit.306w.ca/RIT-DMA-API/1.0.5/)
