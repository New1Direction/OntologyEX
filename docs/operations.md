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

### Spend: bank, card, bills and cloud

The same folder takes where the money goes, kept as **monthly** aggregates:

| Records | Read from | Kept |
|---|---|---|
| Bank, card or ledger lines | any statement CSV with a date, an amount (or debit and credit) and a description, payee or category | money in and out, out by category, by known supplier, and deposits from payment processors |
| Vendor bills | a CSV with bill date, vendor, amount and due date or status | billed, open and overdue, by known supplier |
| Cloud costs | AWS cost and usage reports, Google Cloud billing exports, Azure cost exports | cost by provider and service |

- **Supplier names.** A supplier is named only when the payee or description names one
  OntologyEX knows: the catalog the supplier map uses, with the forms banks print, like
  `AWS EMEA` or `GOOGLE *CLOUD`. Any other payee is counted under its category and never named:
  a person on payroll, a shop, or a customer refunded. Names too common to tell apart from a shop
  or a word (Square, Segment, Neon and a few others) are not matched in bank lines.
- **Bank exports need a currency.** Most have no currency column, so give the file one in
  `operations.json`. Card exports often list charges as positive amounts; add
  `"amounts": "out_positive"` for those.
- **Pull from AWS** with the `aws` command line and the credentials it is already set up with.
  `ce:GetCostAndUsage` is read-only, though AWS charges a small fee per request:

  ```bash
  python ontology-extraction/scripts/operations.py pull aws --out REPO/.ontologyex/operations --months 6
  ```

![Spend: money in and out by month, where it went, cloud by service, suppliers paid against suppliers used](media/control-room-spend.png)

What spend adds:
- **Suppliers paid against suppliers used.** The bank and bills are compared with the supplier map
  from the code:
  - *paid, not in the code*: a tool people use, a service set up outside the repository, or one
    nobody needs any more;
  - *in the code, not paid*: a free plan, paid from another account or card, or not set up for
    production;
  - payment processors are left out of both, since their fees come out of payouts.
- **Payouts against the bank.** Processor payouts, by the month they landed, are compared with
  deposits from payment processors in the bank lines, over the months both cover except the last.
  It is rough, since a payout can land a few days later.
- **Cloud against what comes in.** The last complete month's cloud cost as a share of payments
  taken.
- **Signals:**

| Signal | When |
|---|---|
| `PAYOUTS_NOT_IN_BANK` | payouts exceed processor deposits by more than 2% (and 50) |
| `DEPOSITS_WITHOUT_PAYOUTS` | processor deposits exceed payouts by that much |
| `BILLS_OVERDUE` | a bill is past its due date and not marked paid |
| `SPEND_RISING` | money out in the last complete month is over 1.3 times the three months before |
| `CLOUD_COST_RISING` | cloud costs in the last complete month are over 1.25 times the month before, and up by 100 or more |
| `NEW_SUPPLIER_PAID` | a known supplier was paid for the first time in the last complete month |

Cloud costs describe services, not people, so they are never withheld. Bank lines and bills
follow the small-count rule below.

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

- The records are what was exported. A processor or account not exported is not seen. Payouts
  are reconciled with processor deposits in the bank lines roughly (by month), not line by line,
  and nothing here is reconciled with the books.
- Recurring revenue is rebuilt from each subscription's start, end, price and interval: it is
  an estimate, not the processor's own figure.
- Matching records to the model is by words, so a finding says where to look, not what is so.
