# RITCx Dublin 2026 strategy

Competition day is Saturday 14 November 2026. The case runs at 10:45 and at 13:30. This page reads the practice results in the README and sets out what to change, in what order, before the first heat.

## What the seven practice runs say

| | Our desk | Field average | Best trader |
| --- | ---: | ---: | ---: |
| Mean P&L | $41,963 | $15,469 | $98,145 |
| Worst run | $24,770 | –$7,879 | $31,930 |
| Runs with a loss | 0 of 7 | | |

- **The process is sound.** Seven runs, seven profits, never below $24,000, and 2.7 times the field average. The field average went negative in run 5 while the desk made $43,771. Nothing in these numbers argues for a rebuild the night before.
- **The desk captures about 43% of the best trader's P&L**, and that share is stable (41% to 47% in five of seven runs, with one at 78% and one at 30%). A gap that steady is structural. Luck would move it around.
- **Ranks are what score.** Team P&L is ranked per heat and ranks are averaged over the two heats. A steady $42,000 probably sits in the top third of a 12-trader room. A prize needs the top three teams of the whole field, so the gap to the best trader is the thing to work on. A zero-loss record also says the settings have room to take more.

The README table cannot say where the gap comes from. Three candidates, in the order I would bet on them:

1. **Exit cost.** The desk sells a 25,000-share order through the displayed book in one hit. A trader who works the same block in smaller pieces lets the book refill between them. On 300,000 shares a heat, 5¢ a share is $15,000.
2. **Tenders refused.** The settings in use price the exit as one sweep of the whole block with a 3¢ adverse move, and require 6 ticks per order plus an 8-tick reserve. That refuses thin tenders and late ones. A 70,000-share TAME block is refused after tick 250, and the case brief's own "accept" example is refused at any time.
3. **The benchmark is inflated.** "Best" is the top of 11 to 14 traders each run, so it always includes someone who held inventory through a lucky move or traded speculatively. The competition penalises that. Some of the gap cannot and should not be closed.

`review.py` separates the first two from your own recordings in about a minute. See the plan below.

## What changed in the desk

Each change is small and sits on the code that produced the results above.

| Change | Default | Effect |
| --- | --- | --- |
| A child order that has filled and reconciled is followed by the next on the same poll | Always on | One poll per filled order where there were two. The same checks run in the same order. A 70,000 TAME unwind takes about half the time, which cuts the risk carried and lets the time budget shrink |
| **Book refill between orders** (entry model) | 0, the old behaviour | Prices the exit as separate maximum-size orders, with this share of consumed depth back before each. At 50 the brief's accept example passes with a 1¢ adverse move, and its decline example still fails |
| **Sweep band per order** (unwind) | 0, the old behaviour | A child takes only what is displayed within this many cents of the touch, then the next poll prices a fresh book. Full-size sweeps return automatically when the time budget runs short |
| **Capture more** preset | Staged for you to apply | Refill 50, adverse move at most 2¢, 4 ticks per order, market closeout in the last 8 ticks |
| Saved profiles from the earlier version load with the new settings at 0 | | Your tuned `strategy.json` keeps working unchanged |

Nothing changes in how the desk behaves until you apply a setting, apart from the faster chaining.

## Plan for tonight and the morning

The practice server is up until the heats. Change one thing per heat so each result means something.

1. **Pull, then run `python3 -m unittest`.** Run one heat on your existing settings with `--record`. This confirms the faster chaining on the live feed. Watch that each fill is followed at once by the next order and the desk ends flat.
2. **Run `python3 review.py`.** Look at two numbers: the median exit cost against the mid, and the list of tenders not taken with their reasons.
3. **If tenders are being refused** for "Heat closeout window", "Stressed P&L below acceptance threshold" or "Stressed depth cannot cover the block" while showing a positive sweep, apply **Capture more** and run a heat.
4. **If the exit cost is several cents**, set the sweep band to 3 and run a heat. Keep it if the exit cost falls and unwinds still finish well inside the budget. Drop it if unwinds drag.
5. **Stop changing things after the last practice heat that went well.** Take that profile into the competition on both laptops.

If there is time for only one heat tonight, do step 1 and step 3 together. Of the three changes, Capture more is the one I would take into the competition without a comparison heat, because the zero-loss record shows the entry rule has slack. The sweep band needs its comparison heat first.

## Settings I would start from

| Setting | You used | Start from | Why |
| --- | ---: | ---: | --- |
| Depth removed | 6% | 6% | Yours, tuned on live fills |
| Adverse move | 3¢ | 2¢ | With refill at 50 the model is still stricter than a plain one-sweep at 1¢ on single-order blocks |
| Book refill | 0 | 50 | Stops multi-order blocks being priced as one instant sweep |
| Minimum edge | 0 | 0 | Rank scoring rewards every positive bet |
| Expiry buffer | 2 | 2 | |
| Minimum time left | 8 | 8 | |
| Budget per order | 6 | 4 | Faster chaining roughly halves the time each order needs |
| Exit reserve | 8 | 8 | |
| Market closeout | 0 | 8 | Open stock at the close is penalised. This is insurance and costs nothing when the desk is already flat |
| Sweep band | 0 | 0, then 3 after a comparison heat | Unmeasured |

## How to think about one tender

1. **What am I paid?** The premium is the gap between the tender price and the mid, on your side.
2. **What does the exit cost?** The 2¢ fee, plus what crossing the book costs against the mid.
3. **What is left?** Premium minus both. In the brief's example book, 50,000 at $9.90 leaves 1.4¢ a share, and the brief accepts it. At $9.97 it leaves minus 5.6¢, and the brief declines.
4. **How long am I exposed?** Risk from the mid alone is about `shares × σ × √(ticks to unwind ÷ 3)`. It has no expected cost because the price has no drift, and it shrinks with speed.
5. **Once accepted, the tender price is history.** Every order is priced from the current book. Waiting to get back to break-even is a bet on direction, and the case says there is none.

## What *The Theory of Poker* adds

| Idea | In this case |
| --- | --- |
| Expectation | Take every tender whose modelled exit clears the fee. Judge the rule over many tenders and never by one fill |
| Pot odds | The premium is the pot. Fee plus exit cost is the price of the call |
| Effective odds | Price the whole unwind. The first order always looks profitable |
| Reverse implied odds | A thin premium on a huge block has a capped gain and an open loss while you hold the stock |
| The free card | A tender costs nothing to watch for 30 ticks. In Review mode, a thin edge can wait for a better book. A fat one should be taken at once |
| Value of deception | A large resting order shows the market what you must do. Short-lived marketable orders show little |
| Reading hands | The brief warns that displayed bids can be fake. The model removes a share of displayed depth before pricing |
| The fundamental theorem | You are not betting against the other traders. Their mistakes lift your rank as long as you make none |

## Heat-day checklist

- Same profile on both laptops. One desk per trader.
- Start the desk before the heat opens and arm from a flat account. Re-arm after every heat change and after any feed outage.
- Watch the position card and the fault banner. If the desk asks for a review in RIT with stock on the book, flatten by hand with maximum-size marketable orders. Under 20 ticks left, use market orders.
- Between the two heats, run `review.py` on the morning recording and change at most one setting.

## Still unknown

- Where the gap to the best trader comes from. Your recordings answer it. The README table does not.
- How fast the real book refills, which decides both the refill setting and whether the sweep band helps.
- Whether API orders are permitted in the competition heats. If they are not, the desk is a decision screen and the hand-trading rules above apply.
