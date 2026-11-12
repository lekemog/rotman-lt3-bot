# RITCx Dublin 2026 strategy

Competition day is Saturday 14 November 2026 at Trinity Business School. The case runs twice, at 10:45 and at 13:30. Practice servers stay up until then.

## The rules that decide the result

| Rule | Value | Consequence |
| --- | --- | --- |
| Heat | 300 ticks, 5 tenders, 30 ticks to answer each | Few bets per heat. One bad block decides a heat |
| Securities | CRZY and TAME, random walk with zero drift, 10 to 15% moves in a heat | No direction to predict. Time holding inventory is pure risk |
| Limits | 100,000 net and 250,000 gross across both tickers | A long in one ticker and a short in the other offset on the net limit |
| Order size | 25,000 CRZY and 10,000 TAME per order | A 70,000 TAME block needs seven orders. A CRZY block of that size needs three |
| Fee | 2¢ per share traded in the market | Break-even is the tender premium minus 2¢ minus the cost of crossing the book |
| Front-running | No trading against a tender before accepting it | The desk only ever sends orders that reduce a position it already holds |
| Speculation | Only trade to unwind accepted tenders, and finish flat | Open inventory at the close is penalised. The desk switches to market orders for the last 12 ticks |
| Scoring | Team P&L is the sum of both members. Teams are ranked per heat and the ranks are averaged | Both of you need a working desk. A blow-up in one heat cannot be repaired by a big win in the other |

Ranks are averaged, so the size of a win is worth nothing beyond the places it gains. The target for each heat is a solid positive number with no disaster. The teams you will beat are the ones that take a bad block, freeze on a 75,000-share position or finish a heat with stock on the book.

## How to think about one tender

Work this out once by hand and the model's output will never surprise you.

1. **What am I paid?** The premium is the gap between the tender price and the mid, on your side. Buy 50,000 at 9.90 with the mid at 9.99 pays 9¢ a share.
2. **What does the exit cost?** The fee is 2¢. Crossing the book costs half the spread plus whatever you walk through. In the brief's example book, 50,000 shares sell at an average of 9.934, which is 5.6¢ under the mid.
3. **What is left?** 9¢ minus 5.6¢ minus 2¢ is 1.4¢ a share, or $690 on the block. The brief calls this tender an obvious accept. At 9.97 the same sums give minus 5.6¢, and the brief declines it.
4. **How wrong can the book be?** Depth can vanish and the mid can move before the last child fills. Risk from the mid alone is roughly `shares × σ × √(ticks to unwind ÷ 3)`. For 75,000 shares, σ of 3¢ a tick and four ticks, that is about $2,600 either way. It has no expected cost, because the walk has no drift, and it shrinks with speed. So the unwind should be fast.
5. **Is the book really that thin?** Sweeping 75,000 shares in one instant pays for depth that will be back seconds later. Three children one tick apart each meet a partly refilled book. The model's `refill` setting is the share of consumed depth assumed back between children. At 0 it prices one giant sweep and rejects the brief's own accept example. At 50 it accepts that example and still declines the 9.97 one.

The desk does these sums on every poll and shows them as **Sweep now** and **Model** in the tender queue.

## Three edges most teams will miss

**Netting.** If you are long 60,000 CRZY and an institution bids for 50,000 CRZY, that tender saves a 50,000-share market exit and its fee. It is worth far more than the same tender arriving on a flat book. The desk values every tender against the inventory it holds, and accepts an offsetting tender at once.

**Pairs.** A buy tender and a sell tender live at the same time in one ticker lock a spread between two institutions with no market exit. Either leg can look poor alone. The desk accepts the first on the strength of the pair and the second on the next poll.

**The window is a free option.** You have 30 ticks to answer and nothing to pay for waiting. If the mid moves your way you accept a better trade. If it moves against you, you decline and lose nothing. A deep edge gains little from waiting, so take it. A thin edge gains a lot. The desk holds a profitable tender until its edge exceeds `patience × σ × √(ticks left)`, and that bar falls to zero at the last decision tick. This fails in one situation: if many traders receive the same tender at the same moment, the first to unwind takes the liquidity and the edge decays while you wait. The practice plan below measures which world you are in.

## What *The Theory of Poker* adds

Sklansky's book is about decisions under incomplete information where the price of the bet is known. That is a tender.

| Idea | In the book | In this case |
| --- | --- | --- |
| Expectation | Judge a play by its average result over many repetitions | Accept every tender whose modelled exit clears the fee. Never judge a decision by the one fill it produced |
| Pot odds | Call when the pot pays more than the odds against you | The premium is the pot. Fee plus sweep cost is the price of the call |
| Effective odds | Count every bet you will pay to see the hand through | Price the whole unwind. The first child always looks profitable |
| Reverse implied odds | A hand that wins small pots and loses big ones | A thin premium on a huge block. The gain is capped at the premium and the loss is open while you hold the stock |
| The free card | Seeing another card without paying is worth real money | The 30-tick window. Take the free look when the edge is thin |
| Win the big pots right away | With a strong hand and a large pot, do not get clever | A fat premium on a deep book is accepted at once and swept. Patience is for marginal hands |
| Position | Acting last means acting with more information | Deciding late in the window, and unwinding after the book has refilled |
| Value of deception | Do not show your hand unless you are paid for it | A resting 25,000-share order tells the market what you must do. Children rest for under a second, then are pulled |
| Reading hands | Bets carry information and can be bluffs | The brief warns that displayed bids can be fake. The model removes 10% of displayed depth before pricing |
| Ante and tight play | The ante sets how loose you can afford to play | The 2¢ fee is the ante. With thin premiums, play tight |
| The fundamental theorem | You gain when opponents play differently from how they would with full information | You are not betting against the other traders. Their mistakes leave you higher in the ranking as long as you make none |

Two ideas from the book do not transfer. Bluffing has no counterpart, because placing orders to mislead is the speculative trading the case penalises. Sunk cost has no place either: once a tender is accepted its price is history, and the desk prices every child from the current book and never waits to get back to break-even.

## Defaults and why

| Setting | Default | Reasoning |
| --- | --- | --- |
| Depth removed | 10% | Displayed size is not all real, and other traders hit the same book |
| Adverse move | 1¢ | One tick of slip on the whole exit |
| Refill per child | 50% | Accepts the brief's accept example and declines its decline example. Unmeasured until practice |
| Minimum edge | 0 | Rank scoring rewards taking every positive bet. Raise it if practice fills come in under the model |
| Patience | 1 σ | In simulation, 1 σ beat immediate acceptance in calm and volatile markets and matched it when prices jumped against each tender. 2 σ lost a third of the P&L in that last case |
| Expiry buffer | 3 ticks | Room for a slow poll before the tender disappears |
| Budget per child | 3 ticks, plus a 10-tick reserve | A block that cannot be unwound before the closeout window is refused |
| Sweep band | 5¢ | A child takes what is near the touch and lets the book refill before walking further |
| Rest time | 800 ms | Long enough for refill to trade against the remainder, short enough to hide the size |
| Market closeout | Last 12 ticks | A penalty for open stock is worse than any spread |

## What the simulation showed

`backtest.py` runs the real desk over synthetic heats. The market in it is an assumption, so read the direction of each result and ignore the dollar figures.

- Pricing the exit child by child accepted 4.4 tenders a heat on a thin book where the one-sweep rule accepted 1.7, and nearly tripled the mean P&L there. In deep books the two rules performed alike.
- Patience of 1 σ added about 5% to mean P&L in a calm market and about 30% in a volatile one, with a much better worst case, because marginal tenders were taken at better moments or left alone.
- When the price jumped against each tender a few ticks after it arrived, 1 σ matched immediate acceptance and 2 σ fell well behind. That is the crowded-tender case.
- When liquidity dried up a few ticks after a late tender, waiting meant unwinding into the drought. Patience finished with stock on the book in 4 heats of 30, against 2 for immediate acceptance. Late in a heat, time is no longer free.
- The sweep band and the closeout window made no measurable difference in the synthetic book, which is dense and refills quickly. They are insurance against a thinner real book.

## Practice plan before Saturday

Each step answers one question the code cannot answer from here.

1. **Monitor one heat** with `dashboard.py --rit --record`. Check each tender's side against the RIT dialog. Check the fee and order caps on the Working Orders card. Check whether the **API Orders** indicator in RIT is on. If API orders are off for this case, the desk is a decision screen and you trade by hand.
2. **Watch five or more tenders run to expiry** without trading, then run `calibrate.py`. The tender section shows how each edge moved through its window. A median drift near zero means waiting is free, so keep patience at 1. A clearly negative drift means tenders are shared and raced, so set patience to 0.
3. **Trade two or three heats in Unwind only.** Accept by hand in RIT and let the desk flatten. `calibrate.py` prints realised value against the model for every unwind. If realised runs more than 1¢ a share under the model, lower refill or raise the depth haircut. If it runs above, the model is turning down money.
4. **Trade two heats armed**, one on each of your logins. Confirm both desks finish flat and that the NLV card matches the RIT scoreboard.
5. **Rehearse the failure**: press Stop & cancel with inventory on, flatten by hand in RIT, then press Unwind only. Do it once so it is not new on the day.

## Heat-day checklist

- RIT Client logged in on both laptops, API enabled, API key copied, one desk per trader.
- Start with `py -3 dashboard.py --rit --enable-orders --record` before the heat opens. The desk waits for the case to go active.
- Mode badge reads ARMED on both desks, or UNWIND ONLY if you are deciding by hand.
- Positions flat and no working orders at the start.
- During the heat, watch the alert bar and the position cards. Do not place manual orders in a ticker the desk is unwinding. It waits for any order it did not send.
- If the desk looks wrong, press Stop & cancel and flatten in RIT with market orders. With under 20 ticks left, flatten first and investigate afterwards.
- Between heats, run `calibrate.py` on the morning recording and change at most one setting.

## Trading by hand

Use these rules if API orders are not permitted, or if the desk is stopped.

- Accept when the tender queue shows ACCEPT. If the screen is down: add the fee to the cost of walking the book for the full block, and accept only if the premium is larger.
- Unwind in maximum-size orders, one after another, using marketable limits a few cents through the touch. Do not wait for the price to come back.
- With an offsetting tender on screen, stop selling and take the tender.
- With 15 ticks left, use market orders until flat.

## Open questions

- Whether API order entry is enabled for the competition. The event page publishes display-only support scripts and recommends the Client REST API, which suggests algorithms are expected, but only the RIT indicator settles it.
- Whether every trader receives the same tenders at the same time. This decides the patience setting.
- How quickly the real book refills after a sweep. This decides refill.
- Whether a tender acceptance carries its own fee. Compare the NLV change on an accepted tender with the model.
- The Client REST API needs the Windows RIT Client. On a Mac the desk can only be used on a Windows machine or VM. The terminal advisor `rotman_lt3.py watch dma` reads the DMA port from any system and places no orders.
