#!/usr/bin/env bash
# Rehearse the cutover rollback on a scratch clone. Publishes nothing, touches
# no remote, and is deleted at the end.
#
# Condition 3 of docs/CUTOVER-REHEARSAL.md: "Rehearse the rollback in section 6
# on a scratch fork and record how long it took. An untested rollback is not a
# rollback."
set -u

SRC=/c/Users/lucas/Desktop/trailblazer-datasets
WORK=/c/Users/lucas/AppData/Local/Temp/rollback-rehearsal
rm -rf "$WORK" 2>/dev/null
mkdir -p "$WORK"

echo "== 1. scratch clone (no remote push possible) =="
t0=$(date +%s)
git clone --quiet --no-hardlinks "$SRC" "$WORK/repo" 2>&1 | tail -2
cd "$WORK/repo" || exit 1
git remote remove origin 2>/dev/null
echo "   remotes: $(git remote | wc -l) (0 = cannot push anywhere)"
BEFORE=$(git rev-parse --short HEAD)
echo "   HEAD before the simulated cutover: $BEFORE"
echo "   containers: $(ls containers/*.tbmap 2>/dev/null | wc -l)"
echo "   packages:   $(ls packages/*.tbpack 2>/dev/null | wc -l)"
echo "   clone took $(( $(date +%s) - t0 ))s"

echo
echo "== 2. simulate the cutover commit =="
# Exactly what the Publish step does: the old trees are replaced wholesale.
rm -rf containers packages
mkdir -p containers packages
for r in midlands north south-east south-west wales east-anglia; do
  printf 'not a real container, a stand-in for the shape' > "containers/ways-$r.tbmap"
  printf 'sig' > "containers/ways-$r.tbmap.sig"
done
printf 'not a real container' > containers/ways-overview.tbmap
python - <<'PY'
import io, json
m = {"schema": 1, "generated": "2026-09-24T18:00:00Z", "authorities": 149,
     "packages": [{"package": "ways", "region": r, "area": r,
                   "laneCount": 5000, "file": "x.tbpack", "bytes": 1,
                   "sha256": "a" * 64}
                  for r in ["midlands", "north", "south-east",
                            "south-west", "wales", "east-anglia"]]}
io.open("manifest.json", "w", encoding="utf-8").write(json.dumps(m))
io.open("catalogue.json", "w", encoding="utf-8").write(
    json.dumps({"schema": 2, "countries": [], "note": "post-cutover"}))
PY
git add -A >/dev/null
git -c user.email=r@e -c user.name=rehearsal commit -q -m "Simulated cutover: one dataset, not five"
CUTOVER=$(git rev-parse --short HEAD)
echo "   cutover commit: $CUTOVER"
echo "   containers now: $(ls containers/*.tbmap 2>/dev/null | wc -l)"

echo
echo "== 3. the six-hour problem: a generated file lands on top =="
python - <<'PY'
import io, json
c = json.load(io.open("catalogue.json", encoding="utf-8"))
c["generated"] = "2026-09-24T20:23:00Z"
c["note"] = "rebuilt by traffic-orders.yml, as it does every six hours"
io.open("catalogue.json", "w", encoding="utf-8").write(json.dumps(c))
PY
git add -A >/dev/null
git -c user.email=r@e -c user.name=rehearsal commit -q -m "Traffic orders: rebuild the catalogue"
echo "   HEAD is now $(git rev-parse --short HEAD), one commit past the cutover"

echo
echo "== 4. does the OBVIOUS runbook work? git revert the cutover =="
if git -c user.email=r@e -c user.name=rehearsal revert --no-edit "$CUTOVER" >/dev/null 2>&1; then
  echo "   git revert SUCCEEDED (the doc predicted a conflict)"
  git -c user.email=r@e -c user.name=rehearsal revert --abort 2>/dev/null
  git reset --hard HEAD >/dev/null 2>&1
else
  echo "   git revert FAILED, as the rehearsal predicted:"
  git status --porcelain | grep -E '^(UU|DU|UD|AA)' | head -4 | sed 's/^/     /'
  git revert --abort 2>/dev/null
fi

echo
echo "== 5. the documented rollback =="
t1=$(date +%s)
# THE STEP THE DOCUMENT GETS WRONG. `git checkout <commit> -- <tree>`
# RESTORES the old files and does not REMOVE the new ones, so the first
# rehearsal ended with 116 containers: 109 old plus 13 ways-* left behind.
# The trees have to be cleared first.
rm -rf containers packages
git checkout "$CUTOVER"^ -- containers packages manifest.json 2>&1 | tail -2
echo "   step 1 restored: $(ls containers/*.tbmap 2>/dev/null | wc -l) containers, $(ls packages/*.tbpack 2>/dev/null | wc -l) packs"
if [ -f tools/rebuild_catalogue.sh ]; then
  bash tools/rebuild_catalogue.sh manifest.json catalogue.json >/dev/null 2>&1 \
    && echo "   step 2 catalogue rebuilt: $(wc -c < catalogue.json) bytes" \
    || echo "   step 2 REBUILD FAILED - this is the finding"
fi
git add -A >/dev/null
git -c user.email=r@e -c user.name=rehearsal commit -q -m "Roll back the cutover"
t2=$(date +%s)
echo "   rollback took $(( t2 - t1 ))s"

echo
echo "== 6. is the result actually the old build? =="
echo "   containers: $(ls containers/*.tbmap 2>/dev/null | wc -l)  (109 expected)"
echo "   packages:   $(ls packages/*.tbpack 2>/dev/null | wc -l)  (105 expected)"
echo "   manifest schema: $(python -c "import json,io;print(json.load(io.open('manifest.json',encoding='utf-8')).get('schema'))" 2>/dev/null)"
echo "   ways-* left behind: $(ls containers/ways-* 2>/dev/null | wc -l)  (0 expected)"
echo "   git status clean: $([ -z "$(git status --porcelain)" ] && echo yes || echo NO)"

cd /c/Users/lucas/Desktop
rm -rf "$WORK"
echo
echo "== scratch clone deleted =="
