# The home collector

## What it does

A few councils answer an ordinary home connection but refuse GitHub's
servers with a 403 (measured 7 October 2026):

- Dorset's GeoServer: its rights of way closures layer and its register of
  definitive map applications.
- Norfolk's notices and traffic regulation order pages.
- Wiltshire's rights of way page.
- Powys's byway order pages and the order documents they link to.

The pipeline never tries to get past that refusal. Instead,
`tools/home_collector.py` runs every six hours on two machines that share
the work (`home-collected/collector.json` says which reads what). It reads
only those sources and commits what changed to `home-collected/`:

- **An always-on server** (Oracle Cloud, London) reads Dorset and Powys,
  which answer it. Those sources say `"machine": "server"`.
- **The PC at home** reads Norfolk and Wiltshire. Both sit behind
  Cloudflare's bot challenge, which refuses data centres (measured from the
  server on 7 October 2026: HTTP 403), so only a home connection reaches
  them.

Each machine reads only its own sources and keeps its own heartbeat
(`heartbeat.json` for the PC, `heartbeat-server.json` for the server). They
share `index.json`, but each touches only its own entries, and the server
runs three hours after the PC, so they don't push at the same moment. The GitHub Actions builds then read those
snapshots instead of asking the councils. Each snapshot is matched to the
byways and published like any other source. It is credited to the council,
and the coverage table says "collected directly from the council" with the
date.

It disguises nothing. Every request goes through `tools/polite_http.py`,
exactly as in CI:

- the same User-Agent naming this repository;
- robots.txt obeyed, and the owner's recorded robots.txt decisions read at
  most weekly;
- the same pacing;
- read only.

It is cheap to run:

- It uses conditional requests where the council supports them.
- It writes a snapshot only when the content really changed: a page's text,
  a layer's features or a document's bytes, never a timestamp.
- It writes every file to a temporary name and renames it, so a crash never
  leaves a half-written file.
- It keeps the last good snapshot when a council fails.
- It commits and pushes only when something changed, plus a small
  heartbeat once a day.
- It needs nothing but Python 3.9+ and git.

If either machine's heartbeat is more than three days old, the council
orders workflow opens an issue labelled `home-collector` naming which one,
so a switched-off machine is noticed even while the other carries on.

If the two ever push at once, the loser rebases onto the winner's commit.
If a rebase fails, the collector abandons it and resets its clone to
GitHub's copy, so a clash can never leave a clone stuck. The snapshots from
that run are read again next time.

## Where it runs now

### The server (Dorset, Powys)

- **Machine:** Oracle Cloud Always Free, London (Ampere, Ubuntu 24.04).
  Security updates install themselves daily and it restarts at 04:00 UTC
  when one needs it. SSH is by key only.
- **Clone:** `~/trailblazer-collector`, sparse, holding only `tools/` and
  `home-collected/`.
- **Git:** pushes over SSH with a deploy key made on the server, which can
  write to this repository only (Settings > Deploy keys, "Oracle collector
  (London)"). Its private half never left the server.
- **Schedule:** cron, every 6 hours at 03:17, 09:17, 15:17 and 21:17 UTC,
  three hours after the PC's runs, low priority:

  ```
  17 3,9,15,21 * * * cd $HOME/trailblazer-collector && nice -n 10 /usr/bin/python3 tools/home_collector.py --machine server >/dev/null 2>&1
  ```

- **Log:** `~/trailblazer-collector/collector.log`, rotating at 256 KB.

### The PC at home (Norfolk, Wiltshire)

- **Machine:** Windows, the owner's PC.
- **Clone:** a sparse clone at `C:\Users\lucas\trailblazer-collector`,
  holding only `tools/` and `home-collected/`. It is separate from any
  working copy.
- **Scheduled task:** `TrailBlazer home collector`. It runs as the current
  user every 6 hours, and at logon if a run was missed. It runs only when a
  network is available, at low priority, with no window. It runs with no
  `--machine`, which means `home`.
- **Python:** `C:\Users\lucas\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe`,
  the windowless twin of `python.exe` in the same folder.
- **Log:** `C:\Users\lucas\trailblazer-collector\collector.log`. It rotates
  at 256 KB and keeps 3 old files.
- **Git:** pushes with the GitHub CLI's stored credentials. No token is
  written anywhere.

Run either by hand:

```
python3 tools/home_collector.py --machine server     # on the server
python tools\home_collector.py                       # on the PC (home)
```

## How to stop it

```
schtasks /Delete /TN "TrailBlazer home collector" /F
```

Then delete `C:\Users\lucas\trailblazer-collector` if you like. The builds
keep using the last snapshots committed. After three days, an issue says the
collector has gone quiet.

## Moving it to a Raspberry Pi (or any small Linux machine)

1. Install git and Python 3.9 or later (Raspberry Pi OS has both):

   ```
   sudo apt install -y git python3
   ```

2. Make a fine-grained GitHub token that can push to this repository only.
   On github.com, go to Settings, then Developer settings, then Personal
   access tokens, then Fine-grained tokens, then Generate. Set the
   repository access to `LPSD-1/trailblazer-datasets` only, and the
   permissions to Contents: Read and write, nothing else. Pick an expiry
   date and note it.

3. Make the sparse clone and store the token for git (it goes into
   `~/.git-credentials`, readable by your user only; it is never committed):

   ```
   git clone --filter=blob:none --sparse https://github.com/LPSD-1/trailblazer-datasets ~/trailblazer-collector
   cd ~/trailblazer-collector
   git sparse-checkout set tools home-collected
   git config user.name  "trailblazer-home-collector"
   git config user.email "home-collector@trailblazer.invalid"
   git config credential.helper store
   git pull   # username: your GitHub user; password: paste the token
   chmod 600 ~/.git-credentials
   ```

4. Run it once by hand and read the log:

   ```
   python3 tools/home_collector.py
   tail collector.log
   ```

5. Schedule it every 6 hours with cron (`crontab -e`), low priority:

   ```
   17 */6 * * * cd $HOME/trailblazer-collector && nice -n 10 /usr/bin/python3 tools/home_collector.py >/dev/null 2>&1
   ```

6. Switch off the Windows task so two machines are not collecting:

   ```
   schtasks /Delete /TN "TrailBlazer home collector" /F
   ```

7. Renew the token before it expires. When it lapses, pushes fail, and
   three days later the `home-collector` issue opens.

## Changing what it reads

Edit `home-collected/collector.json` in the repository. Each URL must be
exactly the one the pipeline asks for, and `tools/test_home_collector.py`
checks that. Add only councils that refuse GitHub's servers. Anything CI can
read, CI reads itself.
