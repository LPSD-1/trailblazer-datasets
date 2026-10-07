# The collector

## What it does

A few councils refuse GitHub's shared runners with a 403 but answer an
ordinary, single, fixed server (measured 7 October 2026):

- Dorset's GeoServer: its rights of way closures layer and its register of
  definitive map applications.
- Powys's byway order pages and the order documents they link to.

`tools/home_collector.py` reads only those sources
(`home-collected/collector.json`), every six hours, from one small server
in London, and commits what changed to `home-collected/`. The GitHub
Actions builds then read those snapshots instead of asking the councils.
Each snapshot is matched to the byways and published like any other
source. It is credited to the council, and the coverage table says
"collected directly from the council" with the date.

This is the owner's policy (7 October 2026): reading a council from this
one fixed, honestly named server after it refuses GitHub's shared runners
is allowed. It is not a disguise and not a rotation of addresses. If a
council refuses the server too, it is not asked from anywhere else, and a
real block (a 403 to the server, a bot challenge) is never worked around:
no proxies, no other addresses, no borrowed User-Agent, no headless
browser.

It disguises nothing. Every request goes through `tools/polite_http.py`,
exactly as in CI:

- the same User-Agent naming this repository;
- robots.txt obeyed, and the owner's recorded robots.txt decisions read at
  most weekly;
- the same pacing;
- read only;
- one fixed address. It never changes address, and a council that refuses
  the server is not asked from anywhere else.

Norfolk's and Wiltshire's pages were read from a home connection until 7
October 2026. They sit behind Cloudflare's bot challenge, which refuses
every data centre including this server, and none of them fed a published
order (Norfolk's are footpath and trail closures and a page about the TRO
process; Wiltshire's is a guide that links elsewhere), so they were
retired rather than kept on a machine at home.

It is light on the councils:

- It uses conditional requests where the council supports them.
- It writes a snapshot only when the content really changed: a page's text,
  a layer's features or a document's bytes, never a timestamp.
- It keeps no council order PDF. A document a page links to is recorded in
  `home-collected/index.json` by its SHA-256, size and date only, which is
  all the order register's check uses; a person transcribing it opens it at
  the council's URL.
- It writes every file to a temporary name and renames it, so a crash never
  leaves a half-written file.
- It keeps the last good snapshot when a council fails.
- It commits and pushes only when something changed, plus a small
  heartbeat once a day.
- It needs nothing but Python 3.9+ and git.

If the heartbeat is more than three days old, or any one source has been
failing for more than three days (`failing_since` in `index.json`), the
council orders workflow opens an issue labelled `home-collector` naming it,
so a stopped server, or one council refusing it, is noticed.

Each source says which machine reads it (`"machine": "server"`). The code
can share sources between more than one machine, each with its own
heartbeat (`heartbeat-<machine>.json`), but today every source is the
server's. If a push ever loses a race, the collector rebases onto the
winner's commit; if a rebase fails, it abandons it and resets its clone to
GitHub's copy, so a clash can never leave a clone stuck. That run then
stops, logs a warning that its changes were dropped, and pushes nothing;
the next run reads them again.

## Where it runs

- **Machine:** a small Linux server in London (Ubuntu 24.04). Security
  updates install themselves daily and it restarts at 04:00 UTC when one
  needs it. SSH is by key only.
- **Clone:** `~/trailblazer-collector`, sparse, holding only `tools/` and
  `home-collected/`.
- **Git:** pushes over SSH with a deploy key made on the server, which can
  write to this repository only (Settings > Deploy keys). Its private half
  never left the server.
- **Schedule:** cron, every 6 hours at 03:17, 09:17, 15:17 and 21:17 UTC,
  low priority:

  ```
  17 3,9,15,21 * * * cd $HOME/trailblazer-collector && nice -n 10 /usr/bin/python3 tools/home_collector.py --machine server >/dev/null 2>&1
  ```

- **Log:** `~/trailblazer-collector/collector.log`, rotating at 256 KB.

Run it by hand on the server:

```
cd ~/trailblazer-collector && python3 tools/home_collector.py --machine server
```

## How to stop it

Remove the cron line (`crontab -e`) and delete the deploy key in the
repository's settings. The builds keep using the last snapshots committed.
After three days, an issue says the collector has gone quiet.

## Changing what it reads

Edit `home-collected/collector.json` in the repository. Each URL must be
exactly the one the pipeline asks for, and `tools/test_home_collector.py`
checks that. Add only councils that refuse GitHub's servers and answer the
collector server (check with one request from the server first), and mark
them `"machine": "server"`. Anything CI can read, CI reads itself.
