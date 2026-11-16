# Client transactions pipeline

Loads the May–Oct 2026 PMS/AIF transaction export into PostgreSQL, cleans it, and serves four things over a FastAPI app: distributor summaries, client history with a running portfolio value, a data quality report, and the "net outflows this month" business question.

```
app/
  cleaning.py       all the cleaning rules (pure Python, no DB)
  ingest.py         CLI: CSV -> clean -> Postgres
  queries.py        SQL behind the API
  portfolio.py      running portfolio value logic
  net_outflows.py   the business question (CLI + used by the API)
  main.py           FastAPI routes
schema.sql          tables, indexes, the clean_transactions view
tests/              unit tests for cleaning and valuation
```

## Setup

Needs Python 3.10+ and a running PostgreSQL (tested on 16).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

createdb itus_txn
cp .env.example .env               # then edit DATABASE_URL in .env

python -m app.ingest data/client_transactions_may_oct_2026.csv
uvicorn app.main:app --reload
```

The connection string comes from `DATABASE_URL`, read from `.env` or the environment (the environment wins). Hosted Postgres works too: put its URL in `.env` with `?sslmode=require` and skip `createdb`. The ingest only drops and recreates its own six tables and one view, and the whole load is well under 1 MB.

The ingest prints a summary of what it found. It rebuilds every table from scratch in a single transaction, so it's safe to re-run, and a failed run leaves the previous load in place.

Open http://localhost:8000/docs to try the endpoints.

Run the tests with `pytest` (no database needed).

## Endpoints

| Endpoint | What it returns |
|---|---|
| `GET /distributors/summary?start=&end=` | Per distributor: clients, total invested, total redeemed, net flows, current value. Dates optional, default is the whole file. |
| `GET /clients/{client_id}/transactions` | The client's transactions in date order, with holdings and portfolio value after each one, plus value today and any of their rows that were excluded (with the reason). |
| `GET /data-quality/issues?action=&issue_type=` | Every issue found during ingestion: CSV row number, the problem, and what was done. Includes counts by type. |
| `GET /reports/net-outflows?month=YYYY-MM&format=text` | The business question. `format=text` gives the plain-English version. |

The business question also runs as a script, which is probably the easiest way to hand it to someone:

```bash
python -m app.net_outflows                 # latest month in the data (October 2026)
python -m app.net_outflows --month 2026-08
```

## Schema

```
raw_transactions     every CSV line as text, keyed by line number. Never modified.
distributors         one row per distributor
clients              client_id, name, fund_type, distributor_id
securities           stock_code, stock_name
transactions         cleaned rows, typed, with is_excluded + exclusion_reason
data_quality_issues  one row per problem: source_row, field, issue_type, action, before/after
clean_transactions   view = transactions WHERE NOT is_excluded
```

Why it's shaped this way:

- **Raw and clean are kept separate.** Every cleaning decision in here is a judgment call, and some of them will be wrong. Keeping the raw text means anyone can check what came in against what we did with it, and `data_quality_issues.source_row` points straight at the original line.
- **Distributor and fund type sit on the client, not on each transaction.** In this file every client has exactly one distributor and one fund structure across all their rows. That matches how onboarding works: a client comes in through a distributor into a PMS or an AIF. Storing it once stops the two from drifting apart. The trade-off: if a client ever moves distributor, past flows would get re-attributed to the new one. If that happens in practice, the fix is to put `distributor_id` on the transaction as well.
- **Bad rows are excluded, not deleted.** Suspected duplicates and rows that don't add up stay in `transactions` with `is_excluded = true` and a reason. Undoing a decision is a one-line UPDATE, not a re-run. Every report reads from the `clean_transactions` view so nobody has to remember the filter.
- **The original transaction type is kept.** BUY/PURCHASE and SELL/REDEMPTION are stored as sent, with a separate `direction` column (+1 money in, −1 money out) that the reports use. If the business decides SELL (a trade inside the portfolio) and REDEMPTION (the client taking money out) should be counted differently, nothing has been lost.
- **Money is `NUMERIC`, never float.** Totals are recalculated as quantity × price to the paisa.
- **Indexes** cover the access patterns: (client_id, trade_date) for client history, trade_date for monthly reports, (stock_code, trade_date) for price lookups.

## Data cleaning decisions

The file has 293 rows. After cleaning: **277 clean transactions**, 8 loaded but excluded from reports, 8 not loaded (repeated IDs). 113 issues logged across 99 rows. All of them are in `/data-quality/issues`.

Each issue's `action` is one of:
- **fixed**: we're confident about the correction and the row is used
- **flagged**: used, but a human should confirm
- **excluded**: kept in the database but left out of every report

| What I found | Rows | What I did | Why |
|---|---|---|---|
| Four date formats: `2026-07-23`, `25-07-2026`, `21/05/2026`, `09/21/2026` | 177 non-ISO | Normalised to ISO dates | The dashed format is always day-first (23 rows have a day > 12, none have a month > 12). Slashed dates come in both orders, and the mix shows up under every distributor, so it isn't one feed with its own convention. |
| Slashed dates that could be read either way, where only one reading falls inside May–Oct | 31 | Took the reading inside the period (**fixed**) | The file says it covers May–Oct. `03/08/2026` is either 8 March or 3 August; only August belongs. |
| Slashed dates where both readings fall inside May–Oct (e.g. `05/07/2026`) | 25 | Assumed DD/MM (**flagged**) | No way to tell from the row itself. DD/MM is the Indian convention, and it's the majority 2:1 among slashed dates that *can* be told apart (50 vs 24). See the surprises section: this one matters. |
| Date outside the period (`2027-02-15`) | 1 | **Excluded** | A future date in a historical export. Can't be trusted, and it also had a negative quantity. |
| Same `transaction_id` appearing twice | 8 | Kept one copy, didn't load the other | 7 were exact copies. For `TXN-10223` one copy had a blank client_id, so I kept the complete one. |
| Same client, date, stock, side, quantity and price to the paisa, but a **different** `transaction_id` | 5 | Kept the lower ID, **excluded** the other | Two genuine orders filling at exactly the same price on the same day is possible but unlikely. The giveaway: all five copies are `TXN-10281`–`10285`, the last five IDs in the file. It looks like a batch that got replayed. Excluded rather than deleted in case some are real. |
| `client_id` blank, `NULL` or `N/A` | 9 | Recovered 8 by matching name + fund type + distributor (**fixed**); the 9th was the incomplete duplicate above | Name alone isn't safe: there are two different Sanjay Desais (PMS-014 at Centrum, PMS-024 at Waterfield) and two Lakshmi Raos. If a match had been ambiguous, the row would have been dropped, not guessed. |
| Distributor spelled `A. Rathi Wealth` and `Anand Rathi Wealth` | 22 | Merged into `Anand Rathi Wealth` (**fixed**) | Almost certainly the same firm. No client appears under both spellings, which points to two systems naming it differently. **Needs confirming with the business.** |
| Leading/trailing spaces in names and distributors | 5 | Trimmed | Otherwise `"Sanctum Wealth  "` counts as a separate distributor. |
| Negative quantity on a SELL or REDEMPTION | 3 | Made positive (**fixed**) | The transaction type already says which way the money moved; the minus sign is a sign convention from some upstream system. (One of these is the 2027 row, which is excluded anyway.) |
| Negative quantity on a BUY (`TXN-10153`, −20 Maruti) | 1 | **Excluded** | A buy of −20 contradicts itself. It could be a reversal of an earlier buy or a typo, and the two guesses move the client's holdings in opposite directions. |
| `total_value` ≠ quantity × price | 3 | Recalculated (**fixed**) | In each case the price sits inside that stock's normal range and the quantity is a standard lot size (every quantity in the file is one of 5, 10, 15 … 200). The total is the odd one out. E.g. `TXN-10179`: 150 × 3,210.50 = 4,81,575, but the file says 12,43,665.78. |
| `TXN-10075`: code `HDFCBANK`, name "Infosys Ltd", price 6,709.59 | 1 | **Excluded** | The price fits neither HDFC Bank (~1,500–1,800) nor Infosys (~1,400–1,650). There's no way to know which security this was. The rule: if only the name is wrong and the price fits the code, keep the code; if code, name and price all disagree, exclude. |
| BUY vs PURCHASE, SELL vs REDEMPTION | 293 | Not changed. Stored as sent, plus `direction` | Treated as inflow/outflow pairs for the flow numbers, because that's what the brief's "purchases minus redemptions" implies. Both fund types use all four words, so there's no structural pattern to go on. |

Checks that found nothing in this file but run anyway: client_id prefix vs fund_type mismatch, one client_id with two different names, unknown transaction types, zero or missing quantity/price, malformed client IDs.

### How "current value" is calculated

There's no market price feed in the exercise, so:

- A stock's price on a given day is **the last price anyone in the file traded it at, on or before that day**. That's the best price signal the data has.
- **Current value** = units each client bought (net of sales) during the period × latest price.
- If a client sold more of a stock than they bought in the period, they must have held it from before May. That opening balance isn't in the file, so the position counts as zero instead of a negative holding, and it's flagged (`opening_balance_missing` in client history, `positions_missing_opening_balance` in the summary).

So current value here means "what the money that came in during May–Oct is worth now". It is **not** total AUM, and it shouldn't be read as AUM.

## What surprised me / what the business team should know

1. **The October answer depends on 25 dates we had to guess.** With the guessed dates read as DD/MM, Sanctum, Anand Rathi and Avendus had net outflows in October. Read as MM/DD, it's Avendus, IIFL, Motilal Oswal and Sanctum, and Anand Rathi flips to a net *inflow*. The report checks this automatically and says so when the list would change. Some of these rows are large: `TXN-10021` (₹21.3L Maruti buy) is either 10 Aug or 8 Oct, and `TXN-10271` (₹17.1L) is either 9 Oct or 10 Sep. Getting the source systems to send ISO dates would remove the guessing.

2. **This isn't a complete ledger.** 88 of the 239 client–stock positions go negative over the period, and 44 of 54 clients sold something they never bought in this file. The file starts mid-stream. Without opening balances, holdings and AUM can't be worked out from this file alone.

3. **The duplicate IDs cluster at the end.** All five re-posted trades are TXN-10281 to 10285. If that's a replay from an upstream job, the job may be re-sending on retries, and it's worth checking whether other exports have the same problem.

4. **Client names aren't unique.** Two Sanjay Desais, two Lakshmi Raos, and Rekha Pillai holds both a PMS (PMS-012) and an AIF (AIF-012) account. Any report or reconciliation that joins on name will merge different people.

5. **August was the only month with net outflows overall** (−₹16.6L across all distributors; every other month was positive). Five of the eight distributors were net negative that month.

6. **Outflows are concentrated.** The 10 largest sells/redemptions make up 45% of all outflow value (out of 103 outflow transactions). A handful of client decisions swing the monthly picture, which makes the date question above more important.

7. **Three distributors are net negative over the whole six months:** Motilal Oswal (−₹17.5L), IIFL (−₹5.1L) and Avendus (−₹0.7L). For Motilal it's mostly two large exits: a ₹21.1L Maruti redemption in May and a ₹16.8L UltraTech sale in August.

## What I'd build next

**Incremental, idempotent loads with a review loop for flagged rows.** Right now every run wipes and rebuilds from one file. That's fine for a single export, but in production files arrive daily and overlap. Someone in ops will also want to correct a flagged date or approve a suspected duplicate, and a full rebuild would wipe that correction. Concretely:

- an `ingest_batches` table (file name, checksum, row counts, run time) with `batch_id` on raw and clean rows
- upsert on `transaction_id` so re-sending a file doesn't create duplicates
- a `manual_overrides` table (transaction_id, field, value, who, when) applied after the automatic rules, so human decisions survive reloads
- the data quality endpoint grows an "open / resolved" status so the flagged list shrinks over time

Close second: a real end-of-day price feed (e.g. the NSE bhavcopy) in a `daily_prices` table, so current value isn't using the last trade in the file as a stand-in for a market price.

## Assumptions to confirm with the business

- `A. Rathi Wealth` and `Anand Rathi Wealth` are the same distributor.
- BUY ≈ PURCHASE and SELL ≈ REDEMPTION for flow purposes. If a SELL is a trade inside the portfolio rather than money leaving, net flows should only count REDEMPTION, and that's a one-line change to the `direction` mapping.
- "This month" means the latest month with data (October 2026), since the file ends 31 Oct.
- The 25 ambiguous dates are DD/MM.
