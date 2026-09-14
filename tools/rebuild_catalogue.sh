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
# Published as of 13 Sep 2026, once BOTH halves were in a build riders have.
#
# It was held back for a fortnight of an evening because publishing it early
# took the whole catalogue down: `PackKind.parse` returned null for a kind it
# did not know and `Pack.fromJson` turned that into "The data index lists
# something this version cannot read" for the ENTIRE index, so one unknown pack
# cost every download on every install. `real_catalogue_test.dart` caught it by
# parsing the live file with the app's own reader.
#
# Two things had to be true before this line came back:
#   * the app knows the kind — `PackKind.names`, merged;
#   * and an unknown kind no longer refuses the index. It is skipped now, and
#     named on the Downloads screen, so the NEXT new kind cannot do this again.
[ -d names ] && ARGS+=(--names names)
# Ground height, for hill shading and 3D ground. Held back from the live
# catalogue by exactly the rule the block above records: the app must ship
# `PackKind.height` FIRST. That is now true of the build in progress, and an
# unknown kind is skipped rather than refused since the names incident, so the
# risk this line carries is a pack riders on an older build cannot see - not a
# catalogue none of them can read.
[ -f height/index.json ] && ARGS+=(--height height/index.json)
# Traffic regulation orders. Rebuilt several times a day by its own workflow,
# which is precisely why it has to be listed HERE: the monthly lane refresh
# rebuilds this catalogue from scratch, and a kind it does not pass is a kind
# it deletes. That is the whole reason this file exists.
#
# The index is committed; the sealed pack is a release asset, because three
# megabytes several times a day would be a gigabyte a year of git history.
[ -f tro/index.json ] && ARGS+=(--tro tro/index.json)

echo "rebuilding catalogue with: ${ARGS[*]}"
python tools/build_catalogue.py "${ARGS[@]}"
