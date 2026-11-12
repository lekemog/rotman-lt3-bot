# Rotman LT3 Trading Desk

Local browser dashboard and automated tender execution for the **RITCx Dublin 2026 Liability Trading** case. Python 3.10 or newer, standard library only. Order control uses the Windows RIT Client REST API.

Read [STRATEGY.md](STRATEGY.md) first. It holds the rules that matter, the reasoning behind every default and the practice plan.

## Start a rehearsal

```powershell
git clone https://github.com/lekemog/rotman-lt3-bot.git
cd rotman-lt3-bot
py -3 dashboard.py
```

On systems with a `python3` launcher, use `python3 dashboard.py`. The desk opens at `http://127.0.0.1:8765`. Pick a scenario, restart the heat and click **Arm auto-trade**. The rehearsal market is synthetic, so it shows how the desk behaves and says nothing about how RIT fills.

## Connect to RIT

1. Start the Windows RIT Client, join the LT3 practice or competition session and enable its Client REST API. Note the API port and key from the **API** icon in the status bar.
2. Record a heat in monitor mode. Nothing is sent to RIT.

   ```powershell
   py -3 dashboard.py --rit --record
   ```

3. Check that each tender's side, size and price match the RIT dialog, and that the fee and order caps on the Working Orders card match the brief.
4. Once the organisers permit API orders and the RIT **API Orders** indicator is on, start with order control. Run one desk per trader.

   ```powershell
   py -3 dashboard.py --rit --enable-orders --record
   ```

5. Tick the API Orders box and choose a mode. Then run `py -3 calibrate.py` on what was recorded.

Use `--rit-port` if the RIT Client is not on port `9999`, `--interval` to change the 0.4-second poll, `--state-dir` to keep practice and rehearsal files apart and `--no-open` to open the page yourself.

## Controls

| Control | Tenders | Unwind |
| --- | --- | --- |
| **Arm auto-trade** | Accepts every tender that passes the model | Automatic |
| **Unwind only** | You accept or decline in RIT | Automatic, for whatever position RIT reports |
| **Stop & cancel** | None | None. The desk cancels its own working children and sends nothing more |

The desk stays in its mode across heats and through feed outages, so check the mode badge before each heat. After **Stop & cancel**, any inventory is yours to trade in RIT until you press one of the other two buttons.

## How it trades

- **Entry.** Each tender is scored as the change in book value if it is accepted: its cash, plus the modelled exit value of the new position, minus that of the position already held. The exit is walked child by child through the displayed book, with a share of displayed depth removed, a price slip and a partial refill between children. Fees and order caps come from RIT when it reports them.
- **Netting and pairs.** A tender that offsets inventory is valued at the market exit it saves and is accepted without waiting. Two live tenders on opposite sides of one ticker are accepted as a pair when together they clear the model.
- **Patience.** A profitable tender with a thin edge is held until late in its 30-tick window. The required edge is `patience × σ × √(ticks left)`, where σ is the per-tick move of the mid measured in the current heat. The last decision tick accepts anything above the minimum edge.
- **Unwind.** The reported RIT position is the inventory. One child per ticker works at a time, always reducing the position, capped at the RIT order limit. A child takes the displayed liquidity within the sweep band of the touch, rests any remainder briefly at the last price it took, then is cancelled and repriced. Children widen to full sweeps when the time budget runs short and become market orders in the closeout window.
- **Uncertainty.** If an order request gets no definite answer, the desk cancels every working order in that ticker, waits for a quiet and stable position, and carries on from what RIT reports. It waits and shows an alert while an order it did not send is working in a ticker.

Profiles and activity are stored under `.rotman/`. The NLV card is RIT's own figure. Use the RIT scoreboard for official results.

## Verify, test and calibrate

```powershell
py -3 -m unittest
py -3 rotman_lt3.py self-test
py -3 rotman_lt3.py demo
py -3 backtest.py --heats 40
py -3 calibrate.py
py -3 rotman_lt3.py replay .rotman/snapshots.jsonl
```

The unit tests run the desk against a local mock of the RIT REST API and against the synthetic market. `backtest.py` runs the same desk code over synthetic heats to compare profiles. `calibrate.py` measures volatility, spread, depth, sweep cost, tender edge through the window and realised unwind value against the model from a recording.

## Files

| File | Purpose |
| --- | --- |
| `rotman_lt3.py` | Tender parsing, entry model, child order pricing, RIT reader, terminal advisor |
| `execution.py` | RIT order venue and the desk state machine |
| `simulator.py` | Synthetic market for rehearsal and backtests |
| `dashboard.py`, `web/` | Local dashboard |
| `backtest.py`, `calibrate.py` | Profile comparison and practice-session measurement |

## Rotman references

- [RITCx Dublin event page](https://www.rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/finance-research-and-trading-lab/events/ritcx/ritcx-cards/tcd/)
- [Liability Trading case brief](https://rotmanfrtl.github.io/RITCx-Liability%20Trading%20Case.pdf)
- [RIT Client REST API 1.0.5](https://rit.306w.ca/RIT-REST-API/1.0.5/)
