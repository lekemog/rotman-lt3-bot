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
| **Work a block out over** (unwind) | 0, the old behaviour | Works the block along a straight line to flat over N ticks. On schedule it rests an order at the touch on its own side. Behind schedule it takes only the shortfall. Full speed returns when time is short or the next good tender is live |
| **Auto patience** (entry timing) | 0, the old behaviour | Auto waits for an edge that is large against the mid's volatility over the time left, and takes any eligible tender on its last tick. Review is not held back |
| **Work the exit** preset | Staged for you to apply | 15-tick worked exit, refill 75, adverse move at most 2¢, 8-tick market closeout |
| `--book-depth` flag | 50 orders a side, as before | A deeper book request for when large tenders are refused as uncovered |
| Saved profiles from the earlier version load with the new settings at 0 | | Your tuned `strategy.json` keeps working unchanged |

Nothing changes in how the desk behaves until you apply a setting, apart from the faster chaining.

## Review is earning more than Auto

Take that at face value for the competition: **run Review**. It is the mode you have seen make more money on the live feed, and nothing here has been tested there.

Review can only approve a tender that Auto would also take, so the difference has two possible sources. You approve later, when the book is better. Or you skip the thin ones. Both are the free-option idea: a tender costs nothing to watch for 30 ticks, the price has no drift, so a thin edge is worth more watched than taken, and a fat one should be taken at once.

Be careful with the evidence. One heat's P&L moves by $10,000 or more on the price path alone, so a few heats of Review against a few of Auto cannot separate skill from luck. Three ways to test it:

1. **On your own recordings.** `python3 review.py` now prints your entries against the same tenders taken at their first eligible tick. If your Review entries are clearly ahead, the edge is timing. This uses data you already have.
2. **On tenders that ran their full window.** Record one practice heat in monitor mode. `review.py` replays Auto at once, with patience 1 and with patience 2 on every tender that stayed on screen.
3. **In simulation.** `python3 rotman_lt3_backtest.py --heats 200` now carries two patience policies.

The simulation result is mixed, and you should know that before turning patience on:

| 1,400 synthetic heats per policy | Mean P&L | 5th percentile | Losing heats |
| --- | ---: | ---: | ---: |
| Accept at once | $22,320 | –$14,016 | 11.3% |
| Patience 1 | $24,611 | $0 | 4.4% |
| Patience 2 | $24,672 | $0 | 1.6% |

Almost all of that gain comes from one scenario, where the price jumps against each tender a few ticks after it arrives. In a calm market patience 1 adds 2%. On a thin book it loses 3%. A replay on simulated monitor recordings, which values each entry as an instant sweep, put patience 1 about 15% behind accepting at once because it let four of eighteen tenders go. So patience protects against tenders that turn bad quickly and gives up some that were fine. It is a defence more than a profit engine.

**Auto patience** is in the desk, off by default, for the case where you cannot click: the second laptop, or a heat where you are busy. The offer card now shows the best stressed figure each tender has reached, which is the number to time a Review approval against.

## Speed

Last year's winning rounds averaged about $80,000 and the word from those rooms is that speed decides it. That agrees with the soak test below: every slow option lost when prices moved after acceptance.

- **The desk now polls faster while something is live.** Every 0.2 seconds on Windows, against 0.55 before. A MacBook keeps 0.55 unless you pass `--busy-interval`, because its server is rate-limited. Each child order waits on one poll, so a 70,000-share TAME unwind of seven orders is seconds shorter. `--busy-interval` sets it.
- **A rate limit no longer disarms the desk.** RIT's rate-limit reply means it acted on nothing, so the desk waits as told, sends the request again, polls 0.1 seconds slower for the rest of the session and stays armed. Before, one such reply paused entries until someone re-armed.
- **The MacBook path is the slow one.** It talks to Toronto over the internet and is rate-limited by the server. If either of you can run the Windows RIT Client, that laptop will be faster.
- **Speed and Review pull against each other.** A click costs seconds. If Review has earned more for you, keep it, but approve the fat tenders the moment they appear and save the waiting for thin ones.
- **Leave the worked exit and Auto patience at 0.** They are the opposite of fast.

A search of public code and forums found one LT3 project, an advisory script that prices a tender by walking the book for the full quantity, as this desk already does. There was no public write-up of a winning approach.

Three habits carried over from the market-making bot you shared: check every assumption about rules and timing against the live feed before trusting it, read the position from the exchange and never from your own count, and size so that being wrong is survivable. The desk already does the second. The first is the practice heat. The third is the position you take off within seconds.

## Manual traders making silly trades

The room will have people trading by hand, and some will fat-finger a market order or dump a block in one go. That reaches you in three ways.

- **A swept book while you are unwinding.** Someone clears the bids just as the desk needs to sell. The stock is still worth what it was, and the book refills within a few ticks, but a full-size order sent into that hole sells 10 to 40 cents too cheap. The **hole guard** catches this: if the best quote the desk would hit is more than 1% through the median mid of the last ten ticks, it rests an order on its own side of the spread and waits. It gives up waiting when the exit time budget runs short. A real price move stops looking like a hole after about five ticks, because the median catches up.
- **A swept book while a tender is live.** The entry model already prices the exit off the displayed book, so a tender looks bad while the hole is there and good again when it refills. Nothing to change. In Review, do not approve on the tick a book has just been hit. Wait for the "best seen" figure to come back.
- **Their mistakes are your fills.** A careless buyer lifting offers will hit a resting sell order at a good price. The desk can only collect this while it holds tender inventory. Trading on their mistakes without inventory is the speculation the case penalises, so the desk never does it.

Their losses also move you up directly. Ranks are relative, so every manual trader who gives money away is one more team behind you, provided the desk does not join them.

## What the final soak test showed

The desk was run through about 130 simulated heats with your recorded settings and three alternatives. No heat faulted. One, with everything switched on in a late liquidity drought, ended with stock on the book.

| Mean P&L per heat | Your settings | Capture more | Capture more + guard | Everything on |
| --- | ---: | ---: | ---: | ---: |
| Normal | $33,164 | $33,086 | $33,086 | $32,406 |
| Thin book | $10,293 | $29,135 | $29,103 | $30,586 |
| Price jumps against each tender | $18,188 | $23,288 | $23,964 | $6,895 |
| Overlapping tenders | $33,862 | $33,845 | $33,845 | $33,934 |
| Late depth drought | $27,393 | $27,711 | $27,711 | $28,232 |
| Erratic manual traders | $31,192 | $32,417 | $33,435 | $33,282 |

"Everything on" adds the 15-tick worked exit, Auto patience 1 and a 5¢ sweep band. The synthetic market is an assumption, so read the pattern and ignore the dollars.

- **Capture more with the guard is never materially worse than your settings and is far better on a thin book.** It is the profile to take into the competition.
- **The guard costs nothing when the book is orderly** and adds about $1,000 a heat, with a better worst case, when traders are erratic.
- **The worked exit and patience are a bet on the market being kind after you accept.** When the price moved against each tender they lost two thirds of the P&L. Leave both at 0 unless a live practice heat says otherwise.

## The worked exit, and what it costs

This is the largest lever available and the least proven. Do the sums before trusting it.

- **What it buys.** A full 25,000-share order pays the spread, walks several price levels and pays the 2¢ fee on every share. A 3,000-share slice touches only the best level. A resting order that gets hit earns the spread. If working a block saves 5¢ a share, that is $15,000 on a 300,000-share heat. That is the right size to explain much of the gap to the best trader.
- **What it costs.** Stock held for longer moves with the market. The risk is about `shares × σ × √(ticks ÷ 3)`. For 75,000 shares over 15 ticks with σ at 3¢ a tick that is about $5,000 either way per tender. It has no expected cost, because the price has no drift, but it widens the spread of results. Over 40 ticks it is $8,000, which is why the preset uses 15.
- **Why take the trade.** You are profitable in every run and well short of the top. A wider spread of results with a higher average is what moves a rank from the top third towards the top three. Staying as you are protects a position that does not win a prize.
- **What is unproven.** How often a resting order is hit in the real book, and whether RIT charges the fee or pays a rebate on it. One recorded heat answers both: `review.py` prints the exit cost against the mid.

## Plan for tonight and the morning

The practice server is up until the heats. Change one thing per heat so each result means something.

1. **Pull, then run `python3 -m unittest`.** Run one heat on your existing settings with `--record`. This confirms the faster chaining on the live feed. Watch that each fill is followed at once by the next order and the desk ends flat.
2. **Run `python3 review.py`.** Look at two numbers: the median exit cost against the mid, and the list of tenders not taken with their reasons.
3. **If tenders are being refused** for "Heat closeout window", "Stressed P&L below acceptance threshold" or "Stressed depth cannot cover the block" while showing a positive sweep, apply **Capture more** and run a heat.
4. **If the exit cost is several cents**, apply **Work the exit** and run a heat. Keep it if the exit cost falls by more than a cent or two and every block finishes flat. If resting orders are almost never hit, set the work window back to 0 and try a sweep band of 3 instead.
5. **Stop changing things after the last practice heat that went well.** Take that profile into the competition on both laptops.

If there is time for only one practice heat, run it with **Capture more** and **Erratic traders** applied, with `--record`, and read `review.py` afterwards. Take that profile into heat one. Leave the work window and Auto patience at 0. Use the morning heat's recording to decide whether anything changes for the afternoon.

Run whichever entry mode has earned more on the live feed. You report that Review has, so use Review.

## Settings I would start from

| Setting | You used | Start from | Why |
| --- | ---: | ---: | --- |
| Depth removed | 6% | 6% | Yours, tuned on live fills |
| Adverse move | 3¢ | 2¢ | With refill at 50 the model is still stricter than a plain one-sweep at 1¢ on single-order blocks |
| Book refill | 0 | 50, or 75 with a worked exit | Stops multi-order blocks being priced as one instant sweep |
| Minimum edge | 0 | 0 | Rank scoring rewards every positive bet |
| Expiry buffer | 2 | 2 | |
| Minimum time left | 8 | 8 | |
| Budget per order | 6 | 4 | Faster chaining roughly halves the time each order needs |
| Exit reserve | 8 | 8 | |
| Market closeout | 0 | 8 | Open stock at the close is penalised. This is insurance and costs nothing when the desk is already flat |
| Hole guard | 0 | 100 | Free in an orderly book, protective in an erratic one |
| Auto patience | 0 | 0 | Mixed evidence. Review does this by eye |
| Work a block out over | 0 | 0, or 15 only after a live comparison heat | Large upside, and it lost badly in simulation when prices moved against each tender |
| Sweep band | 0 | 0 | The fallback if resting orders are not hit |

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
