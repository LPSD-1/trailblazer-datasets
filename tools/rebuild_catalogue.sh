#!/usr/bin/env bash
# Rebuild catalogue.json. THE only way any workflow may do it.
#
#     tools/rebuild_catalogue.sh <lane manifest> <output>
#
# WHY THIS EXISTS
# ---------------
# catalogue.json is rebuilt from scratch every time, so anything NOT passed on
# the command line is deleted from it. Three different workflows rebuilt it,
# each with its own subset of flags, and each therefore quietly deleted what
# the others had published:
#
#   * the monthly lane refresh passed --lanes only, so the first refresh after
#     imagery was published would have wiped every satellite pack;
#   * the DAILY imagery job passes --satellite but not --trips, so it deleted
#     the ready-made trips within a day of them appearing;
#   * the same job passes no --routing-mirror-base, so every mirrored routing
#     tile's URL collapsed to a bare filename and 404'd against the Pages root.
#
# Each of those was found separately, and the first fix - adding the missing
# flags to one workflow - is what produced the second and third. The bug is not
# any one missing flag. It is that the set of flags is written down in three
# places and only ever corrected in one.
#
# So: one script, every index, every time. A workflow that publishes a new kind
# of data adds it HERE and every job picks it up.
#
# Every index it reads is COMMITTED to the repository, not left in dist/. That
# is what makes this work from any job's checkout: the built trips pack used to
# live in gitignored dist/, so the daily imagery job could not have found it
# even if it had asked.
set -euo pipefail

LANES="${1:?usage: rebuild_catalogue.sh <lane manifest> <output>}"
OUT="${2:?usage: rebuild_catalogue.sh <lane manifest> <output>}"

BASE_URL="${BASE_URL:-https://lpsd-1.github.io/trailblazer-datasets/}"
REPO_URL="${REPO_URL:-https://github.com/lpsd-1/trailblazer-datasets}"

ARGS=(--lanes "$LANES" --base-url "$BASE_URL" --out "$OUT")

# Each index is passed only when it exists, because a job that runs before the
# one which creates it must not fail - but a MISSING index and a DELETED index
# look the same from here, which is why verify_catalogue.py exists.
[ -f satellite/index.json ] && ARGS+=(--satellite satellite/index.json)
[ -f routing/index.json ] && ARGS+=(
  --routing-mirror routing/index.json
  --routing-mirror-base "$REPO_URL/releases/download/routing/"
)
[ -f trips/gb.tbtrips ] && ARGS+=(--trips trips/gb.tbtrips)
# NOT YET. The packs are built and committed; publishing them is held back
# until an app that can read them is in riders' hands.
#
# `PackKind.parse` returns null for a kind it does not know, and `Pack.fromJson`
# turns that into "The data index lists something this version cannot read" for
# the WHOLE catalogue — so one unknown pack does not degrade gracefully, it
# takes every download on every older install with it. Publishing `names`
# before the app shipped did exactly that, and `real_catalogue_test.dart`
# caught it by parsing the live file with the app's own reader.
#
# Uncomment when a build that knows `names` is out.
# [ -d names ] && ARGS+=(--names names)

echo "rebuilding catalogue with: ${ARGS[*]}"
python tools/build_catalogue.py "${ARGS[@]}"
