# DVP Counterparty Settlement Risk Monitor

A daily risk report for a book of pending delivery-versus-payment (DVP) equity and bond trades. It
measures each counterparty's credit exposure until the trades settle, checks it against a
grid-based credit limit, ages settlement fails, flags wrong-way trades, and puts counterparties with
recent legal or leadership news on a watchlist.

The trade book is synthetic, however the market data is real (Yahoo Finance, Bank of Canada, FRED).
Sample output: [docs/sample_report.txt](docs/sample_report.txt).

## The idea in one example

We buy 10,000 shares at $50 from a counterparty, to settle tomorrow (T+1). Before settlement the
counterparty defaults, and the shares now trade at $52.

- Under **DVP**, cash and securities change hands together, so we never paid the $500,000. What we
  lose is the cost of buying the same shares elsewhere: **(52 − 50) × 10,000 = $20,000**. That's the
  *replacement cost*.
- Had we paid first (free of payment), the full **$500,000** would have been at risk: *principal
  risk*, which DVP removes.

So DVP exposure depends on **how far the price can move before settlement**, not on the size of the
trade. In the sample report, $139M of trades awaiting settlement carries $3.5M of exposure (2.5%).

Selling works the other way round: we lose if the price *falls*, because we'd have to sell elsewhere
for less.

## Method

For each open trade, with quantity *Q*, trade price *P₀*, current price *P* and multiplier *m* (1
for shares, 0.01 for bond face quoted per 100):

| Quantity | Formula |
|---|---|
| Replacement cost (signed) | buy: (P − P₀)·Q·m  sell: (P₀ − P)·Q·m |
| Current exposure | max(0, replacement cost) |
| Add-on (equity) | z · σₕ · P·Q |
| Add-on (bond) | price change for a z·σₕ adverse yield move, repriced exactly (≈ modified duration · P · z · σₕ) |
| Potential future exposure (PFE) | max(0, replacement cost + add-on) |

- **z = 1.645**: one-sided 95%. A trade already in our favour (negative replacement cost) absorbs
  part of the adverse move before it costs anything, which is why the add-on is added to the
  *signed* replacement cost.
- **σₕ** is the volatility over the remaining horizon *h*, in business days of the settlement
  location: 1 for a T+1 trade on its trade date, and 5 (assumed) for a failed trade whose
  settlement date is unknown. It comes from a **GARCH(1,1)** forecast (the `arch` package) fitted on
  up to three years of daily changes: σₕ² is the sum of the forecast daily variances, which is
  σ·√h for a flat forecast. EWMA (λ = 0.94) is the fallback if GARCH can't be fitted. Equities use
  log returns of dividend-adjusted closes. Marks use as-traded closes, so a dividend adjustment never
  creates a fake price move.
- **Bonds**: Yahoo Finance has no individual bond prices, so each bond is priced from its benchmark
  government yield (Bank of Canada for Canada, FRED for Treasuries) plus a fixed spread, using the
  street-convention price/yield formula. The yield's daily changes give σ.
- **Wrong-way risk**: selling a counterparty securities its own group issued (e.g. RBC shares to
  RBC). If it defaults, those securities fall at the same moment, so the exposure is taken as the
  **jump-to-default** loss: shares go to zero, senior bonds to a 40% recovery. Buying them from the
  counterparty is the right-way case: they get cheaper to replace.
- **Counterparty exposure** is the sum of its trades' PFE: no netting (each DVP trade settles on its
  own) and no diversification across prices. Both make the total conservative.
- **Limits** come from a grid of internal rating × counterparty type
  ([data/limit_grid.csv](data/limit_grid.csv)). Utilization of 80% or more is amber, 100% or more is
  an excess. A blank cell means no appetite, so any exposure is an excess. Excesses are tracked from
  the day they start (`reports/history.csv`).
- **Settlement fails** are aged in business days past the settlement date and bucketed as in
  Basel's treatment of unsettled DVP transactions (CRE70). The capital held is positive current
  exposure × factor:

  | Business days past settlement | 0–4 | 5–15 | 16–30 | 31–45 | 46+ |
  |---|---|---|---|---|---|
  | Factor | 0% | 8% | 50% | 75% | 100% |

- **Settlement calendars**: TSX/CDS for Canada, NYSE/DTC for US equities, and the US federal
  holidays plus Good Friday for US bonds. For example, on Columbus Day NYSE is open but the bond
  market isn't, and a Canadian trade on the Friday before Civic Holiday settles on Tuesday.
- **Watchlist**: counterparties with news in the last 30 days in the categories legal (lawsuits,
  investigations, fines, regulatory actions), leadership or deal. See
  [Counterparty news](#counterparty-news-from-attention-alerts).
- **No lookahead**: every number for a business date uses only data known by the end of that day.
  Each trade records both when it was due to settle and when it did, so any past day can be replayed
  with `--as-of`.

## The synthetic book

[dvp/make_book.py](dvp/make_book.py) generates 40 business days of institutional trades in 22
equities and 7 bonds (Canadian and US) with 14 counterparties. Trade prices are real closes plus a
small execution difference. Most trades settle T+1, a few by agreement T+3, and about 2.5% fail and
settle late.

The trades settle bilaterally, trade for trade. That's deliberate: exchange trades between dealers
are usually taken over by a central counterparty (NSCC in the US, CDS's net settlement service in
Canada), which then bears the credit risk. Bilateral DVP exposure mostly sits with trades that
settle one by one, such as institutional clients settling through custodians.

Some situations are planted so every section of the report has content: a BB hedge fund sized to
115% of its limit with a regulatory news item, an asset manager at 88% whose CIO resigned, fails
aged 2, 7 and 20 business days, and a sale of RBC shares to RBC. Counterparties with listed parents
(banks, insurers, an asset manager) are real names so that real news can be attached; their
internal ratings are illustrative, not assessments. The low-rated names are fictional.

## Running it

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 if needed).

```bash
uv sync
uv run python -m dvp run                      # report for the book's business date
uv run python -m dvp run --as-of 2026-09-15   # replay an earlier day
uv run python -m dvp make-book --seed 7       # regenerate the book (default: as of the last close)
uv run pytest && uv run ruff check .
```

`run` prints the report and saves `reports/<date>/report.txt` with `trades.csv` and
`counterparties.csv`. Market data is cached in `data/cache/` on the first run (about 20 seconds).
Pass `--refresh` to re-fetch it.

## Counterparty news from Attention Alerts

Attention Alerts is a separate project of mine. It fetches news and SEC
8-K filings for the stocks on a watchlist, and Claude Haiku sorts them into stories (category,
major/routine, one-line summary). This monitor can read its major `legal`, `leadership` and `deal`
stories for the counterparties' listed parents:

1. In Attention Alerts, add the counterparties to a watchlist so its worker fetches their news,
   e.g. in `.env`: `OWNER_WATCHLIST=RY:TSX,TD:TSX,BNS:TSX,MFC:TSX,SLF:TSX,JPM:NYSE,GS:NYSE,BLK:NYSE`
2. `uv sync --extra attention-alerts` (installs the Postgres driver)
3. Point the monitor at its database (read-only):
   `ATTENTION_ALERTS_DSN=postgresql://postgres:postgres@127.0.0.1:5432/attention uv run python -m dvp run`

Without it, the watchlist uses [data/counterparty_events.csv](data/counterparty_events.csv), which
holds sample events for the fictional counterparties. If the database is unreachable, the report
still runs and says so under warnings.

## Limitations

- The trade book, ratings and limits are synthetic or illustrative; the bonds are illustrative
  issues priced off real benchmark yields at a constant spread.
- Price moves are assumed normal over the horizon, with GARCH capturing volatility clustering but
  not fat tails.
- Exposure isn't netted or diversified (conservative). FX volatility on USD exposures, accrued
  interest, and changes in bond spreads are ignored.
- Failed trades assume a fixed 5-day horizon. The calendars approximate CDS, DTC and SIFMA
  holidays.
- Marks are daily closes, not intraday prices.

## Layout

```
dvp/
  book.py         trades, counterparties, securities; loading and validation
  calendars.py    settlement calendars, T+n and business-day counts
  market.py       Yahoo Finance, Bank of Canada Valet, FRED; parquet cache; no-lookahead helpers
  volatility.py   GARCH(1,1) with EWMA fallback, multi-day horizon
  bonds.py        price from yield
  exposure.py     replacement cost, add-ons, PFE, wrong-way (pure functions)
  engine.py       measures every open trade as of a business date
  limits.py       limit grid, per-counterparty totals and status
  fails.py        fail aging, Basel factors
  news.py         counterparty events: CSV and Attention Alerts
  report.py       text report, CSVs, history
  make_book.py    synthetic book generator
data/             reference data, limit grid, trade book (market data cache is not committed)
tests/            pytest, with fake market data (no network)
```
