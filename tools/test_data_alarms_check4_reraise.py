#!/usr/bin/env python3
"""Check 4 accepts a soft step whose failure a later step re-raises.

    python tools/test_data_alarms_check4_reraise.py

Exit 0: every case below holds. Exit 1: at least one does not. Exit 3: the
harness could not reach a verdict (PREMISE), which is not a pass.

THE DEFECT. tools/hunt_data_pipeline_alarms.py check 4 held every
`continue-on-error: true` step to a later step that raises an issue on
`steps.<id>.outcome == 'failure'`. order-register.yml's `held` step uses
the other honest shape: a later step runs on exactly that condition and
ends in `exit 1`, which fails the job, and the job's `if: failure()` step
then raises its issue. Check 4 flagged it, so the hunt went red on correct
workflows and the alarm suites stopped lane publishing.

THE RULE HELD HERE. A soft step is also told when a LATER step in its job
(1) runs on exactly `steps.<id>.outcome == 'failure'` (or `!= 'success'`),
nothing else in the `if:`; (2) is itself not continue-on-error, and its
script ends in a bare, unconditional `exit N`, N from 1 to 255, that
nothing before it can skip or turn into an argument; and (3) is followed
in the job by a step on exactly `if: failure()` that runs `gh issue`.
Every case below but the first breaks one of those and must stay flagged.

WHAT IS RUN. The real hunt, as a subprocess, over a copy of the real
workflows with order-register.yml rewritten (HUNT_WORKFLOWS points it
there), through test_data_alarms_check4_scope.py's harness.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_data_alarms_follow_up_fires_on_failure as base  # noqa: E402
import test_data_alarms_check4_scope as scope  # noqa: E402

REG = "order-register.yml"
HELD = "List approved orders held back from riders"
RERAISE_NAME = "      - name: Fail the run if the held-back list could not be built\n"
RERAISE_IF = RERAISE_NAME + "        if: steps.held.outcome == 'failure'\n"
RERAISE_EXIT = "could not be built\"\n          exit 1\n"
ISSUE_NAME = "      - name: Raise an issue if this failed\n"
ISSUE_IF = ISSUE_NAME + "        if: failure()\n"


def text():
    t = scope.real(REG)
    for anchor in (RERAISE_IF, RERAISE_EXIT, ISSUE_IF, "      - name: "
                   + HELD + "\n"):
        if t.count(anchor) != 1:
            raise base.Premise("%s has %d copies of %r, not one"
                               % (REG, t.count(anchor), anchor))
    return t


def block(t, name_line):
    """(start, end) of the step that starts at `name_line`: up to the next
    step at the same indent, or the end of the job."""
    s = t.index(name_line)
    e = t.find("\n      - ", s + 1)
    e = len(t) if e < 0 else e + 1
    # Keep the comment lines above the next step with the next step.
    while True:
        prev = t.rfind("\n", s, e - 1)
        line = t[prev + 1:e - 1]
        if prev > s and (line.strip().startswith("#") or not line.strip()):
            e = prev + 1
        else:
            break
    return s, e


def move_before(t, name_line, before_line):
    """`t` with the step at `name_line` moved to just above `before_line`."""
    s, e = block(t, name_line)
    step = t[s:e]
    rest = t[:s] + t[e:]
    at = rest.index(before_line)
    # Above the comment block that introduces `before_line`, if any.
    while True:
        prev = rest.rfind("\n", 0, at - 1)
        line = rest[prev + 1:at - 1] if at > 0 else ""
        if line.strip().startswith("#"):
            at = prev + 1
        else:
            break
    return rest[:at] + step.rstrip("\n") + "\n\n" + rest[at:]


def sub(t, old, new):
    if t.count(old) != 1:
        raise base.Premise("%s has %d copies of %r" % (REG, t.count(old), old))
    return t.replace(old, new)


def no_issue(t):
    """`t` with every `gh issue` in the failure-issue step made an echo."""
    s, e = block(t, ISSUE_NAME)
    if "gh issue" not in t[s:e]:
        raise base.Premise("the failure-issue step runs no gh issue")
    return t[:s] + t[s:e].replace("gh issue", "echo issue") + t[e:]


def run(t):
    return scope.hunt(extra_files={REG: t})


def real_shape_wrong():
    """The unmodified order-register job: green, the held step re-raised."""
    rc, out = scope.hunt()
    hits = scope.check4(out, REG, HELD)
    said = [l for l in out.splitlines() if REG in l and HELD[:30] in l
            and "re-raised" in l]
    if rc == 0 and not hits and said:
        return ""
    return "the real order-register job: exit %d, re-raised line %s\n%s" % (
        rc, bool(said), out)


def flagged_wrong(label, t):
    """'' when the hunt flags the held step under check 4 for this text."""
    rc, out = run(t)
    if rc == 1 and scope.check4(out, REG, HELD):
        return ""
    return "%s: expected exit 1 with a check-4 FAIL on %r, got exit %d:\n%s" \
        % (label, HELD, rc, out)


def cases():
    t = text()
    exit_line = "          exit 1\n"
    with_exit = RERAISE_EXIT

    def reraise_if(cond):
        return sub(t, RERAISE_IF, RERAISE_NAME + "        if: %s\n" % cond)

    def reraise_body(before, last="exit 1"):
        """The re-raise step's script as `before` lines then `last`."""
        lines = "".join("          %s\n" % b for b in before)
        return sub(t, with_exit, "could not be built\"\n" + lines
                   + "          %s\n" % last)

    out = [
        # (1) The condition.
        ("the re-raise only echoes and never exits",
         sub(t, with_exit, "could not be built\"\n")),
        ("the re-raise is on another step's outcome",
         reraise_if("steps.other.outcome == 'failure'")),
        ("the re-raise is on a step id the held id is a prefix of",
         reraise_if("steps.held2.outcome == 'failure'")),
        ("negated", reraise_if("${{ !(steps.held.outcome == 'failure') }}")),
        ("negated without brackets", reraise_if("!steps.held.outcome")),
        ("cancelled", reraise_if("steps.held.outcome == 'cancelled'")),
        ("success", reraise_if("steps.held.outcome == 'success'")),
        ("conclusion, which is 'success' under continue-on-error",
         reraise_if("steps.held.conclusion == 'failure'")),
        ("failure() && ..., which never runs on a green job",
         reraise_if("failure() && steps.held.outcome == 'failure'")),
        ("cancelled() && ...",
         reraise_if("cancelled() && steps.held.outcome == 'failure'")),
        ("a further condition that can keep it from running",
         reraise_if("steps.held.outcome == 'failure' && "
                    "github.event_name == 'schedule'")),
        ("an || that is not the outcome alone",
         reraise_if("steps.held.outcome == 'failure' || "
                    "steps.held.outcome == 'success'")),
        ("no if at all", sub(t, RERAISE_IF, RERAISE_NAME)),
        ("the soft step has no id, the re-raise names an empty one",
         sub(sub(t, "        id: held\n", ""), RERAISE_IF, RERAISE_NAME
             + "        if: steps..outcome == 'failure'\n")),
        # (2) The exit.
        ("exit 0", reraise_body([], "exit 0")),
        ("exit 256, which the shell reports as 0", reraise_body([], "exit 256")),
        ("exit 1 behind ||", reraise_body(["true ||"])),
        ("exit 1 behind &&", reraise_body(["false &&"])),
        ("exit 1 piped into", reraise_body(["echo x |"])),
        ("exit 1 as an argument after a continued line",
         reraise_body(["echo \\"])),
        ("exit 1 inside a here-document", reraise_body(["cat <<EOF"])),
        ("an exit 0 before it", reraise_body(["exit 0"])),
        ("an exit 0 in a trap before it", reraise_body(["trap 'exit 0' EXIT"])),
        ("a return before it", reraise_body(["return 0"])),
        ("an exec before it", reraise_body(["exec true"])),
        ("exit 1 with something after it on the line",
         reraise_body([], "exit 1 || true")),
        ("exit 1 then a command after it", sub(
            t, with_exit, "could not be built\"\n" + exit_line
            + "          echo done\n")),
        ("the re-raise is itself continue-on-error", sub(
            t, RERAISE_IF, RERAISE_IF + "        continue-on-error: true\n")),
        ("the re-raise script is folded, so its lines join into one",
         sub(t, RERAISE_IF + "        run: |\n",
             RERAISE_IF + "        run: >\n")),
        ("the re-raise is a uses: step", sub(
            t, RERAISE_IF + "        run: |\n",
            RERAISE_IF + "        uses: actions/github-script@v7\n"
            "        with:\n          script: |\n")),
        ("the re-raise carries uses: beside its run", sub(
            t, RERAISE_IF, RERAISE_IF + "        uses: ./x\n")),
        # Order.
        ("the re-raise comes BEFORE the soft step",
         move_before(t, RERAISE_NAME, "      - name: " + HELD + "\n")),
        ("the issue step comes BEFORE the re-raise",
         move_before(t, ISSUE_NAME, RERAISE_NAME)),
        # (3) The job's failure-issue step.
        ("no failure-issue step in the job", sub(
            t, ISSUE_IF, ISSUE_NAME + "        if: always() && false\n")),
        ("the issue step's if is !failure()", sub(
            t, ISSUE_IF, ISSUE_NAME + "        if: ${{ !failure() }}\n")),
        ("the issue step is failure() && something",
         sub(t, ISSUE_IF, ISSUE_NAME + "        if: failure() && "
             "github.event_name == 'schedule'\n")),
        ("the issue step raises no issue", no_issue(t)),
    ]
    return out


def passes_wrong(label, t):
    """'' when the hunt is green and reads the held step as re-raised."""
    rc, out = run(t)
    said = [l for l in out.splitlines() if REG in l and HELD[:30] in l
            and "re-raised" in l]
    if rc == 0 and not scope.check4(out, REG, HELD) and said:
        return ""
    return "%s: expected exit 0 with the held step re-raised, got exit %d:" \
        "\n%s" % (label, rc, out)


def passing():
    """Equivalent spellings of the same shape, each still accepted."""
    t = text()
    return [
        ("the re-raise if wrapped in ${{ }}", sub(
            t, RERAISE_IF, RERAISE_NAME + "        if: ${{ "
            "steps.held.outcome == 'failure' }}\n")),
        ("the re-raise on != 'success'", sub(
            t, RERAISE_IF, RERAISE_NAME + "        if: "
            "steps.held.outcome != 'success'\n")),
        ("the issue step's if wrapped in ${{ }}", sub(
            t, ISSUE_IF, ISSUE_NAME + "        if: ${{ failure() }}\n")),
        ("exit 255 after comments", sub(
            t, RERAISE_EXIT, "could not be built\"\n          # why\n"
            "          exit 255\n          # done\n")),
    ]


def all_wrong():
    wrong = []
    w = real_shape_wrong()
    print("%-4s %s" % ("FAIL" if w else "ok", w or "the real shape passes"))
    if w:
        wrong.append(w)
    for label, t in passing():
        w = passes_wrong(label, t)
        print("%-4s %s" % ("FAIL" if w else "ok", w or label))
        if w:
            wrong.append(w)
    for label, t in cases():
        w = flagged_wrong(label, t)
        print("%-4s %s" % ("FAIL" if w else "ok", w or label))
        if w:
            wrong.append(w)
    return wrong


def test_check4_reraise():
    wrong = all_wrong()
    assert not wrong, "\n".join(wrong)


def main():
    try:
        return 1 if all_wrong() else 0
    except base.Premise as e:
        print("PREMISE  %s" % e)
        return 3


if __name__ == "__main__":
    sys.exit(main())
