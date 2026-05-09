---
name: trade-thesis
description: Run a structured trade thesis on a stock ticker. Use when the user asks "thesis on TICKER", "trade thesis on TICKER", "should I buy TICKER", "/trade-thesis TICKER", or similar phrasing. Performs an analyst pass (quote, technicals, short interest, squeeze score, news), bull/bear debate, risk-gate via the user's Pre-Trade Scorecard, and outputs a 🟢 BUY / 🟡 WATCH / 🔴 PASS verdict with a suggested vehicle if BUY. Advisory only — never calls options_buy or options_sell.
---

# Trade Thesis Workflow

Borrows the multi-agent structure from TauricResearch/TradingAgents (analyst → bull/bear → risk gate → verdict) but runs it inline, single-pass, using existing MCP tools. No new framework. Output goes to Slack — keep it terse.

## Activation

Trigger when the user says any of:
- "trade thesis on TICKER"
- "thesis on TICKER" / "thesis TICKER"
- "should I buy TICKER"
- "/trade-thesis TICKER"
- "run thesis on TICKER"
- "what do you think of TICKER" (only if the conversation is clearly about trading)

If the user gives multiple tickers, run sequentially — one Slack message per ticker.

## NO-FABRICATION RULE

Every number in the output MUST come from a tool call. If a tool fails, surface that inline and downgrade the verdict to 🟡 WATCH with reason "insufficient data: <which tool failed>". Do not estimate, infer, or backfill values.

## Step 1 — Analyst pass (parallel)

Call all of these in a single batch:

- `stock_quote(TICKER)`
- `stock_technicals(TICKER)`
- `short_interest(TICKER)`
- `squeeze_score(TICKER)`
- `WebSearch` "TICKER earnings date 2026"
- `WebSearch` "TICKER news this week" (limit to last 7 days)
- `WebSearch` "TICKER analyst price target rating" — only if `market_cap > $2B` from quote

Pin these values for downstream use (verbatim from tool output, no rounding beyond display):

- price, %chg, volume vs avg, 52w hi/lo, market cap, P/E
- SMA20/50/200, RSI(14), MACD line/signal/histogram, Bollinger upper/middle/lower
- SI%, days-to-cover, SI MoM trend, FINRA short ratio if available
- squeeze_score (0–100) and component breakdown
- next earnings date + BMO/AMC + days away
- top 1–3 news catalysts in the last 7 days (one line each)

## Step 2 — Bull case (3–5 bullets)

Strongest case for being long, grounded ONLY in Step 1 data. Each bullet must reference a specific number or fact from Step 1. No generic platitudes ("strong company", "AI tailwinds" without specifics).

## Step 3 — Bear case (3–5 bullets)

Same rules. Strongest case for staying out or being short. Specifically check for and surface, when applicable:

- Extended technicals (>20% above SMA50, RSI > 70, price piercing upper Bollinger)
- IV crush risk (earnings within DTE window with elevated IV)
- Negative SI MoM trend
- Broken trend (close below SMA20 or SMA50)
- Obvious valuation stretch (P/E vs sector peers — note if extreme)

## Step 4 — Debate / reconcile (2–3 sentences)

Which side has the better evidence right now? Be honest. If it's a coin flip, say so. Identify the SINGLE PIVOT FACT — the one thing that, if it changes, flips the verdict. Examples: "the May 20 earnings print", "a daily close above $X", "VIX dropping below 18".

## Step 5 — Risk gate (Pre-Trade Scorecard)

Apply the user's stored Pre-Trade Scorecard (memory hash `1964132f`, tags: `U011E9TPW84,trading,framework,scorecard`). Walk each row, output PASS / FAIL / N/A with a one-phrase reason.

Specifically check:

- RSI(14) in 40–65?
- Price not piercing upper Bollinger?
- Catalyst inside a reasonable option expiry window? (45–75 DTE preferred)
- IV rank ≤ 50%? — only pull `options_chain` for a >=45 DTE expiry if Steps 1–4 are leaning bullish; otherwise skip
- SPY/VIX regime gate — only for high-beta momentum longs. Fetch `stock_quote("SPY")` and `stock_quote("^VIX")` if relevant. HARD GATE: SPY < 50SMA OR VIX > 22 → no high-beta momo trade.
- DTE ≥ 30 (≥45 preferred) achievable on a sensible expiry?
- Position correlation — flag if it would be the 4th high-beta momo name without context (the user can override)

## Step 6 — Verdict

One of:

- 🟢 **BUY** — high or medium confidence, all hard gates pass
- 🟡 **WATCH** — thesis is alive but entry is wrong (extended, IV elevated, gate failed) OR data is incomplete
- 🔴 **PASS** — thesis fails on bear-case fundamentals or technicals

Confidence: low / medium / high.

If 🟢 BUY, suggest a vehicle (advisory only — do NOT call `options_buy`):

- 45–75 DTE call (per user framework)
- Strike: ATM or OTM 5–10% based on conviction
- Stop level: underlying close below SMA20 or recent swing low (specific price)
- Position size in R: default 1R, drop to 0.5R if any non-hard scorecard row failed

If any HARD scorecard gate fails (DTE rule, EV ≤ 0 if computable, P(touch) < 35% if computable, regime hard gate when applicable) — downgrade to 🟡 WATCH. Never issue 🟢 BUY past a hard gate.

## Step 7 — Output (Slack-formatted, terse, <3000 chars)

Use this exact structure:

```
`TICKER` — 🟢/🟡/🔴 VERDICT (confidence)

📊 Snapshot: $X.XX (+/-X.X%) | RSI XX | SMA20 $X | SI X.X% | DTC X.Xd | sq-score XX
📅 Earnings: YYYY-MM-DD (X days, BMO/AMC)
📰 Catalysts: <up to 3 one-liners>

🐂 BULL
• …
• …

🐻 BEAR
• …
• …

⚖️ Pivot: <single fact that flips the verdict>

🚦 Scorecard: <pass/fail summary, e.g. "5/9 pass — fails RSI(72), IV rank, regime">

🎯 If BUY:
  Vehicle: TICKER $STRIKE C YYYY-MM-DD @ $X.XX (DTE XX, IV XX%, OI X)
  Size: 1R or 0.5R
  Stop: underlying close below $X.XX (SMA20)
  Target: $X.XX (option ~$X.XX, +XX%)
```

If 🟡 WATCH, replace the "If BUY" block with a "Why not BUY now" block listing 1–3 concrete reasons + a cleaner setup (e.g. "wait for post-earnings IV reset", "use a defined-risk spread instead").

Sources line at the end: 1–3 markdown links to the WebSearch sources used. Slack-format as `<url|title>`.

## Output rules

- One ticker per response unless the user explicitly listed multiples.
- Total response under ~3000 chars for Slack readability.
- No preamble ("Let me…", "I'll run…", "Sure!"). Lead with the ticker line.
- No methodology recap — the user knows what the workflow does.
- Prices: 2 decimals. IV: integer percent. Percentages: 1 decimal.
- If a tool returns an error, surface it inline ("⚠️ short_interest tool failed") and continue with what you have.
- NEVER call `options_buy` or `options_sell` — the verdict is advisory.
- NEVER issue 🟢 BUY past a hard scorecard gate failure.

## Version

v1, adopted 2026-05-08 in lieu of integrating TauricResearch/TradingAgents. Companion memory entry: hash `acee2ad6` (tags: `U011E9TPW84,trade_thesis_workflow`).
