# Synthetic early-warning replay

This is a small, deliberately constructed demonstration of the scoring and
the production control-room threshold rule. It is **not a historical evaluation
and does not establish that OntologyEX predicts failures**.

Reproduce the full result, including each decision time and the observation
timestamps visible at that time:

```bash
python evals/early_warning_replay.py --out /tmp/ontologyex-early-warning-replay.json
python -m unittest discover -s tests -p 'test_early_warning_replay.py'
```

The [fixture](datasets/synthetic-early-warning.json) fixes a 500 ms threshold,
readings, decision times and independent fixture outcome labels in eight fictional
payment-service timelines. The one-sample baseline warns on a fresh value above
500 ms. The two-sample rule uses `control_room.build_state` and warns only after
two distinct, consecutive fresh values from the same source exceed 500 ms.
Both see the same readings and threshold. Samples at future times and decisions
at or after the labeled failure are excluded. A warning is credited as a hit
only if it occurs before that failure; lead time is the difference. For a labeled
no-failure case, a warning is a false alarm within the fixture's follow-up.

| Outcome | One sample | Two samples |
| --- | ---: | ---: |
| Hits before failure | 2 | 1 |
| Failures without a warning | 1 | 2 |
| False alarms | 2 | 1 |
| No alarm, no failure | 1 | 2 |
| Insufficient telemetry | 2 | 2 |

In the persistent-breach failure case, the two-sample rule fires four minutes
before the fictional failure; the one-sample rule fires five minutes before.
In the single-spike failure case, the two-sample rule misses it. In the brief
spike without failure, the one-sample rule produces an extra false alarm. There
are no claims about how often these situations occur in real businesses.

The default decision window is minutes 0–5, with a labeled failure at minute 6
or no labeled failure through minute 10. An explicit stale-data case starts its
decisions at minute 4. Observations expire after 90 seconds. An unwarned case
must have fresh samples at each decision after an initial one-decision warm-up
for the two-sample rule; otherwise it is insufficient data, not a clean result
or a miss. A warned case can be scored while also marking partial coverage.
All incident and no-incident labels are provided by the fixture; there is no
independent incident source here. Dependency paths and user response are tested
separately in the browser flow, not scored by this replay.

Before drawing conclusions about a client, use time-stamped observations and
independently verified incident labels, freeze thresholds and topology before
the test period, include quiet periods and gaps, and compare against the alerts
the team already uses. The present script accepts only this synthetic fixture;
an ingestion and evaluation workflow for real historical cases remains work to do.
