# Rotman LT3 Trading Desk

Local browser dashboard and automated tender execution for the **RITCxDublin Liability Trading** simulation. Python 3.10 or newer. The dashboard and backtest use the standard library. RIT order control uses the Windows RIT Client REST API.

## Start a rehearsal

```powershell
git clone https://github.com/lekemog/rotman-lt3-bot.git
cd rotman-lt3-bot
py -3 dashboard.py
```

On systems with a `python3` launcher, use `python3 dashboard.py`. The desk opens at `http://127.0.0.1:8765`. Select a rehearsal scenario, start a fresh heat and click **Arm entries** to see simulated tender acceptance, fills and inventory reconciliation.

The **Strategy** editor applies a validated profile between polling iterations. Changes apply to the next decisions and child orders. Submitted orders keep their original terms. Profiles and activity are stored under `.rotman/`. The P&L card shows completed tenders using observed fill VWAP and the configured fee model. Use the RIT blotter and scoreboard for official figures.

## Connect to RIT

1. Start the Windows RIT Client, join the LT3 practice or competition session, and enable its Client REST API. Check the local API port in the client. Rotman recommends the Client REST API for competition use.
2. Start monitor mode and enter the **Client API key** in the terminal prompt. The key stays in process memory.

   ```powershell
   py -3 dashboard.py --rit --record
   ```

3. During practice, compare the tender direction, size and expiry with the RIT dialog. Compare the estimated fees and completed tenders with the RIT transaction log. Adjust the fee model if needed.
4. Once the organisers permit automated order submission and the RIT **API Orders** indicator is enabled, run the order-capable mode. Use one desk instance per trader and reserve manual trading for taking over from the desk.

   ```powershell
   py -3 dashboard.py --rit --enable-orders --record
   ```

5. Check the session in the dashboard, confirm the API Orders checkbox and arm when positions and working orders are flat. Re-arm after a heat change or feed outage.

Use `--rit-port` if the RIT Client uses a port other than `9999`. Use `--state-dir` for a separate rehearsal or practice profile. Use `--no-open` if you want to open the local URL yourself.

## Strategy and controls

- **Entry.** Score the whole block against executable bids or asks, fees, depth loss and an adverse move. For each tender, the minimum time left is the greater of the fixed floor and `closeout reserve + ticks per child × minimum child orders`. The desk accepts at most one tender at a time and waits for RIT to report the resulting signed position.
- **Unwind.** Use a single inventory-reducing child order at a time, capped at 25,000 shares for CRZY or 10,000 for TAME. Poll the order by ID, cancel a resting remainder, and reconcile the final fill with the position before submitting again. The default is marketable limits with a one-cent price cushion. A market closeout window is available in the strategy editor and starts disabled.
- **Pause entries.** Stop accepting tenders and continue managing confirmed inventory.
- **Stop & cancel.** Stop entries and new unwind orders. Request cancellation of the desk's known working child when RIT data is available. Check the blotter and manage any remaining inventory in RIT. A request already in flight may complete before the desk can stop.
- **Review in RIT.** After an ambiguous order response or an inventory mismatch, inspect the RIT position and blotter. Reset is available once the account is flat and working orders have cleared. The desk does not retry an ambiguous submission.

The entry defaults include a 10% depth reduction, one-cent adverse move and four-tick tender expiry buffer. The initial unwind budget is six ticks per minimum child plus a ten-tick reserve. These settings were chosen for rehearsal and need calibration on recorded RIT fills. In particular, the synthetic tests showed significant residual inventory during a late liquidity drought even with the longer entry budget.

## Verify or replay

```powershell
py -3 -m unittest -v
py -3 rotman_lt3.py self-test
py -3 rotman_lt3_backtest.py --heats 250
py -3 rotman_lt3.py replay .rotman/snapshots.jsonl
```

The automated tests use a local RIT HTTP mock. The backtest generates books, tender arrivals, latency and partial fills. Historical validation requires real practice snapshots and the transaction log. `--record` saves dashboard snapshots for comparison with that log.

## Rotman references

- [RITCxDublin event page](https://www.rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/finance-research-and-trading-lab/events/ritcx/ritcx-cards/tcd/)
- [Liability Trading case brief](https://rotmanfrtl.github.io/RITCx-Liability%20Trading%20Case.pdf)
- [RIT Client REST API 1.0.5](https://rit.306w.ca/RIT-REST-API/1.0.5/)
