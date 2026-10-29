# Operations: what the business actually does

The code says what a business *can* do. Its records say what it *does*: the payments it
takes, the refunds it gives, the disputes it fights, the payouts that reach the bank, the
subscriptions that start and end, the invoices that go unpaid. OntologyEX reads those
records and keeps **weekly aggregates per currency only**. No row, customer or identifier is
ever kept or shown.

![Operations: what needs a person, the last four complete weeks, payments and refunds by week, and why](media/control-room-operations.png)

Open `/operations` in the control room (`operate.py up`, or `control_room.py --repo REPO`).
The **command center** ranks what needs a person (Operations category). Agents ask through
`operations_status`.

## Give it the records

Drop exports in `REPO/.ontologyex/operations/` (or pass `--operations DIR`). The page picks up a
new or changed file on its next refresh.

| Records | Read from |
|---|---|
| Payments, refunds, disputes, payouts, subscriptions, invoices | Stripe dashboard exports (CSV), recognized by file name and columns |
| The same, as API objects | Stripe API lists saved as JSON (`{"object": "list", "data": [...]}`) |
| Another processor (PayPal, Paddle, Square, …) | its CSV export, described once in `operations.json` |
| A live account | a read-only pull that writes aggregates only (below) |

`operations.json` in the same folder describes a file the columns do not identify:

```json
{"files": [{"match": "paypal*.csv", "stream": "payments", "date_format": "%m/%d/%Y",
            "columns": {"amount": "Gross", "currency": "Currency", "created": "Date",
                        "status": "Status", "fee": "Fee"},
            "status_map": {"Completed": "succeeded", "Denied": "failed"}}]}
```

A file that is not recognized is listed with its column names (never its values) and what to
add. A file whose dates or currencies cannot be read says how many rows were not counted.

### Pull with a read-only key

```bash
STRIPE_API_KEY=rk_live_... python ontology-extraction/scripts/operations.py pull stripe \
  --out REPO/.ontologyex/operations --days 180
```

- **Restricted key only.** Create a restricted key in the Stripe dashboard with **read**
  access to charges, refunds, disputes, payouts, subscriptions and invoices. A secret key
  (`sk_…`) can change the account, so it is refused unless you pass `--allow-secret-key`.
- **Aggregates only.** Each page is aggregated as it arrives and discarded; the only file
  written is `stripe-api.ontologyex-aggregate.json`, with no IDs, emails or rows.
- **One answer per week.** Where a pull and an export cover the same week, the pull is kept and
  the overlap is reported.
- **It goes over the network**, so an agent asks before running it.

## What is read, and what is not

- From each file only the columns a stream needs: amounts, dates, statuses, reasons, plans and
  intervals. Customer names, emails, IDs, card details, descriptions and other free text are
  never read.
- A reason or plan that looks like an identifier or an email is counted as "other".
- Amounts and breakdowns of a week with fewer than three records are **withheld** (counts are
  still shown), so a total never stands for one person's payment. `operations.py report
  --min-cell N` changes the threshold.

## What it shows

Per currency, with the last four **complete** weeks against the four before (the week in
progress is shown, never compared):

- payments taken, failure rate and why payments failed;
- refund rate and why refunds were given;
- disputes, their rate, how many are open, and why customers disputed;
- payouts and failed payouts;
- active subscriptions, monthly recurring revenue (each plan's price brought to a month), started
  and ended;
- open and uncollectible invoices.

### Signals

| Signal | When |
|---|---|
| `DISPUTES_OPEN` | a dispute was open or awaiting a response when exported (an unanswered dispute is lost) |
| `PAYOUT_FAILED` | a payout failed in the last four weeks |
| `REFUND_RATE_RISING` | refunds are at least 2% of payments and at least 1.5 times the rate before |
| `REVENUE_DOWN` | payments taken fell more than 20% |
| `DISPUTE_RATE_HIGH` | disputes are at least 0.75% of successful payments, a common warning level |
| `CHURN_RISING` | at least 3 subscriptions ended and at least 1.5 times as many as before |
| `FAILED_PAYMENTS_HIGH` | 15% or more of at least 20 attempts failed |
| `INVOICES_UNCOLLECTIBLE` | invoices were marked uncollectible |
| `SUBSCRIPTIONS_PAST_DUE` | subscriptions were past due when exported |

### Code and operations

The records are compared with the business model and the code, by the words they are named
with, so each finding is inferred:

- **Happens, not in the sources**: the records show refunds, disputes, payouts or the like, and
  nothing in the model or code is named for them. They are handled somewhere the sources do not
  show: by hand, in a supplier's dashboard, or in code outside this repository.
- **Built, not seen**: the model names something (say, refunds) and 8 or more weeks of records
  show none.
- **Seen and modeled**: both; the matched business items are linked.

## Limits

- The records are what the processor exported. A processor not exported is not seen, and
  figures are as reported by the processor, not reconciled with the bank or the books (that is
  the next step: accounting and spend).
- Recurring revenue is rebuilt from each subscription's start, end, price and interval: it is
  an estimate, not the processor's own figure.
- Matching records to the model is by words, so a finding says where to look, not what is so.
