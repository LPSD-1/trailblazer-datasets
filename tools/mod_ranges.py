#!/usr/bin/env python3
"""Military range firing times in England and Wales, from GOV.UK.

    python tools/mod_ranges.py

Several byways cross military training areas - Salisbury Plain above all -
and "do not access the byways when the red flags are flying". The MoD
publishes each range's firing times on GOV.UK every month, collected under
"Military ranges firing notices". This reads that collection through the
GOV.UK content API (Open Government Licence), takes each range's notice for
the current month, and publishes status/mod-ranges.json: per range, the
notice's title and link, the month it covers, and its timing lines exactly
as published ("Tuesday: 9am to 11:30pm").

WHAT IT DOES NOT DO: draw the ranges. No open dataset of range or danger
area boundaries was found (7 October 2026): the byelaw maps are PDFs, and
OpenStreetMap is ruled out. So no byway is put inside a range by a guess.
A person who has checked a range's byelaw map may list its byways in
status/mod-ranges-ways.json ({"<range label>": ["<way_uid>", ...]}); those
are carried onto the range here, and a uid that no longer exists is
reported, not published.

Scotland's notices are skipped: this data covers England and Wales.
"""
import argparse
import datetime
import html
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from council_sources import read_json, write_json  # noqa: E402
from polite_http import FetchFailed, Refused  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "status")
API = "https://www.gov.uk/api/content"
COLLECTION = "/government/collections/firing-notice"

_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"]
_MONTH_YEAR = re.compile(r"(?i)\b(%s)\s+(\d{4})\b" % "|".join(_MONTHS))
_TIME = re.compile(r"(?i)\b\d{1,2}(:\d{2})?\s?(am|pm)\b|\b\d{4}\s?(hrs|-)|"
                   r"\bno (live )?firing\b|\bnon[- ]firing\b|\bclosed\b")
_DAY = re.compile(r"(?i)^(monday|tuesday|wednesday|thursday|friday|"
                  r"saturday|sunday)\b")


def month_of(title):
    """'... October 2026 firing times' -> (2026, 10); the LAST month named,
    so a span ('28 September to 8 November 2026') reads as its end."""
    found = _MONTH_YEAR.findall(title or "")
    if not found:
        spans = re.findall(r"(?i)\b(%s)\b" % "|".join(_MONTHS), title or "")
        year = re.findall(r"\b(20\d{2})\b", title or "")
        if spans and year:
            return int(year[-1]), _MONTHS.index(spans[-1].lower()) + 1
        return None
    name, year = found[-1]
    return int(year), _MONTHS.index(name.lower()) + 1


def label_of(title):
    """The range a notice is for: its title without the month and the
    words every notice shares."""
    text = re.sub(r"(?i)\b\d{1,2}(st|nd|rd|th)?\s+(%s)\b" % "|".join(
        _MONTHS), " ", title or "")
    text = _MONTH_YEAR.sub(" ", text)
    text = re.sub(r"(?i)\b(%s)\b|\b20\d{2}\b" % "|".join(_MONTHS), " ", text)
    text = re.sub(r"(?i)\b(six week|firing|flying|times?|notices?|forecast|"
                  r"and activity|to)\b|[:/]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -,")
    return re.sub(r"(?i)^for\s+|\s+for$", "", text).strip(" -,")


def choose(attachments, today):
    """{label: attachment} - this month's notice per range, else the newest
    one that has begun."""
    now = (int(today[:4]), int(today[5:7]))
    best = {}
    for a in attachments:
        when = month_of(a.get("title"))
        if not when or when > (now[0], now[1] + 1) and not (
                now[1] == 12 and when == (now[0] + 1, 1)):
            continue
        label = label_of(a.get("title"))
        rank = (when == now, when <= now, when)
        if label not in best or rank > best[label][0]:
            best[label] = (rank, a, when)
    return dict((label, (a, when)) for label, (_r, a, when) in best.items())


def text_lines(body):
    """A GOV.UK body as its lines of text, headings and list items apart."""
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", body or "")
    body = re.sub(r"(?i)<br\s*/?>|</(p|li|h\d|dt|dd|tr|div)>", "\n", body)
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.split("\n")]
    return [l for l in lines if l]


def timings(lines):
    """[{heading, line}] for every line that says when firing happens.

    A day name on a line of its own followed by its times is joined up.
    The heading is the nearest line above that is neither a timing nor
    longer than a sentence: the range's name, where the notice gives one.
    """
    out, heading = [], None
    i = 0
    while i < len(lines):
        line = lines[i]
        if _DAY.match(line) and not _TIME.search(line) and i + 1 < len(
                lines) and _TIME.search(lines[i + 1]):
            line = "%s %s" % (line, lines[i + 1])
            i += 1
        if _TIME.search(line) and len(line) < 160:
            out.append({"heading": heading, "line": line})
        elif len(line) < 80 and not line.endswith("."):
            heading = line.rstrip(":")
        i += 1
    return out[:200]


def fetch(client, today, log=print):
    coll = client.get_json(API + COLLECTION)
    docs = [d for d in (coll.get("links") or {}).get("documents") or []
            if "scotland" not in (d.get("title") or "").lower()]
    if not docs:
        raise FetchFailed("the firing notice collection lists no documents")
    ranges = []
    for doc in sorted(docs, key=lambda d: d.get("base_path") or ""):
        pub = client.get_json(API + doc["base_path"])
        attachments = (pub.get("details") or {}).get("attachments") or []
        chosen = choose(attachments, today)
        if not chosen:
            ranges.append({"publication": doc.get("title"),
                           "url": "https://www.gov.uk" + doc["base_path"],
                           "label": label_of(doc.get("title")),
                           "updated": (doc.get("public_updated_at") or
                                       "")[:10],
                           "month": None, "times": []})
            continue
        for label, (a, when) in sorted(chosen.items()):
            url = a.get("url") or ""
            page = client.get_json(API + url) if url.startswith(
                "/government/") else {}
            lines = text_lines((page.get("details") or {}).get("body"))
            ranges.append({"publication": doc.get("title"),
                           "label": label, "title": a.get("title"),
                           "url": "https://www.gov.uk" + url,
                           "updated": (page.get("public_updated_at") or
                                       doc.get("public_updated_at") or
                                       "")[:10],
                           "month": "%04d-%02d" % when,
                           "times": timings(lines),
                           "byway_notes": [l for l in lines if re.search(
                               r"(?i)\bbyways?\b|\broads? .*closed", l)][:10]})
    log("MoD ranges: %d notices from %d publications" % (len(ranges),
                                                        len(docs)))
    return ranges


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--today")
    args = ap.parse_args(argv)
    today = args.today or datetime.date.today().isoformat()
    import polite_http
    path = os.path.join(args.out, "mod-ranges.json")
    held = (read_json(path, {}) or {}).get("ranges") or []
    try:
        ranges = fetch(polite_http.PoliteClient(min_gap=2.0), today)
    except (Refused, FetchFailed, ValueError) as e:
        print("::error::GOV.UK firing notices could not be read (%s); "
              "status/mod-ranges.json is unchanged" % e)
        return 1
    if held and len(ranges) < len(held) * 2 // 3:
        print("::error::%d range notices against %d held - a bad read; "
              "kept the last" % (len(ranges), len(held)))
        return 1
    pins = read_json(os.path.join(args.out, "mod-ranges-ways.json"), {}) \
        or {}
    if pins:
        from byway_match import load_byways
        known = load_byways().ways
        for r in ranges:
            uids = pins.get(r["label"]) or []
            r["ways"] = [u for u in uids if u in known]
            gone = [u for u in uids if u not in known]
            if gone:
                print("::warning::%s: reviewed byways no longer published: "
                      "%s" % (r["label"], ", ".join(gone)))
    write_json(path, {
        "attribution": "Firing times from the Ministry of Defence's "
                       "notices on GOV.UK (Open Government Licence v3.0). "
                       "Red flags by day and red lamps by night mean live "
                       "firing: do not cross the range boundary whatever "
                       "these times say.",
        "source": "https://www.gov.uk" + COLLECTION,
        "ranges": ranges})
    return 0


if __name__ == "__main__":
    sys.exit(main())
