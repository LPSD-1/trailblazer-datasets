#!/usr/bin/env python3
"""The alarm hunt refuses follow-up `if:` forms that never run on a failure.

    python tools/test_data_alarms_follow_up_refuses_what_never_runs.py

Companion to test_data_alarms_follow_up_fires_on_failure.py (same harness,
same exit codes: 0 green, 1 a case wrong, 3 PREMISE). The features-7
verifier made `fires_on_failure` in hunt_data_pipeline_alarms.py refuse four
more shapes that look like a follow-up but never run when the soft step
fails, and nothing held them:

* `failure() && ...` / `... && cancelled()`: continue-on-error keeps the job
  successful, so failure() is false and the step never runs;
* `!((...))`: a negation behind two brackets;
* `!(always() && ...)`: the reference inside a negated group.

And two that DO run must stay green: `failure() || ...` and an unrelated
negation `!inputs.dry_run && ...`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_data_alarms_follow_up_fires_on_failure as base  # noqa: E402

CASES = [
    ("failure() && never runs under continue-on-error", base.TRO, base.TRO_IF,
     "if: failure() && steps.cutage.outcome == 'failure'", base.TRO_STEP),
    ("&& cancelled() never runs on a failure", base.TRIPS, base.TRIPS_IF,
     "if: steps.trips.outcome == 'failure' && cancelled()", base.TRIPS_STEP),
    ("negated behind two brackets", base.TRO, base.TRO_IF,
     "if: ${{ !((steps.cutage.outcome == 'failure')) }}", base.TRO_STEP),
    ("inside a negated group", base.TRO, base.TRO_IF,
     "if: ${{ !(always() && steps.cutage.outcome == 'failure') }}",
     base.TRO_STEP),
    # Must stay green: these do run when the step fails.
    ("failure() || still fires", base.TRO, base.TRO_IF,
     "if: failure() || steps.cutage.outcome == 'failure'", None),
    ("an unrelated negation still fires", base.TRO, base.TRO_IF,
     "if: ${{ !inputs.dry_run && steps.cutage.outcome == 'failure' }}", None),
]


def test_premise():
    base.premise()


def test_never_running_follow_ups_are_refused():
    base.premise()
    wrong = [w for w in (base.check(*c) for c in CASES) if w]
    assert not wrong, "\n".join(wrong)


def main():
    try:
        base.premise()
    except base.Premise as e:
        print("PREMISE  %s" % e)
        return 3
    bad = 0
    for c in CASES:
        try:
            wrong = base.check(*c)
        except base.Premise as e:
            print("PREMISE  %s" % e)
            return 3
        print("%-4s %s" % ("FAIL" if wrong else "ok", wrong or c[0]))
        bad += bool(wrong)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
