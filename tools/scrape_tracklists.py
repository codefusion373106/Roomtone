"""Add per-episode tracklists to episodes.json.

The RSS feed carries no tracklists, but every item links to an episode page.
Those pages are a Sapper SPA whose initial state is inlined as a JS object
literal:

    __SAPPER__={baseUrl:"",preloaded:[void 0,{entry:{...,
        tracklist:"artist - title\\u003Cbr\\u003Eartist - title..."}}]}

So this walks each episode page, lifts the ``tracklist`` string out of that
payload, decodes the JS escapes, and splits it on ``<br>``. Audio URLs,
durations and byte sizes still come from the feed - only the tracklists are
added here.
"""

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import catalog  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# `tracklist:"..."`, allowing escaped quotes inside the value.
TRACKLIST_RE = re.compile(r'tracklist\s*:\s*"((?:[^"\\]|\\.)*)"')

ITUNES_DURATION = "{http://www.itunes.com/dtds/podcast-1.0.dtd}duration"


def fetch(url, timeout=25, accept="*/*"):
    request = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": accept,
    })
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def feed_page_links(url, timeout):
    """Map episode id -> episode page URL, read straight off the feed.

    catalog.py deliberately does not surface the page link in its episode shape,
    and this tool only needs the URL, so it reads it directly rather than
    widening the API contract for one optional field.
    """
    root = ET.fromstring(fetch(url, timeout))
    channel = root.find("channel")
    if channel is None:
        raise catalog.CatalogError("feed has no <channel>")
    links = {}
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        duration = (item.findtext(ITUNES_DURATION) or "").strip()
        match = re.search(r"\d+", title)
        if match and link:
            links[match.group(0)] = link
    return links


def decode_js_string(raw):
    """Decode the body of a double-quoted JS string.

    json already handles \\uXXXX, \\n, \\t, \\/ and \\"; \\xXX is JS-only, so it
    is rewritten to \\u00XX first. The escaped angle brackets decode to real
    ``<br>`` tags, which the caller splits on.
    """
    raw = re.sub(r"\\x([0-9a-fA-F]{2})", r"\\u00\g<1>", raw)
    return json.loads('"' + raw + '"')


def parse_tracklist(html):
    """Return the episode's track list, or [] when the page has none."""
    match = TRACKLIST_RE.search(html)
    if not match:
        return []
    text = decode_js_string(match.group(1))
    tracks = []
    for line in text.replace("<br>", "\n").split("\n"):
        line = line.strip()
        if line:
            tracks.append(line)
    return tracks


def main():
    ap = argparse.ArgumentParser(description="Add tracklists to episodes.json")
    ap.add_argument("--feed", default="https://musicforprogramming.net/rss.xml")
    ap.add_argument("--out", default=os.path.join(BASE_DIR, "episodes.json"))
    ap.add_argument("--limit", type=int, help="only fetch N episodes (testing)")
    ap.add_argument("--only", help="comma-separated episode ids to fetch")
    ap.add_argument("--delay", type=float, default=0.35,
                    help="seconds between requests (default 0.35)")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what was found, but do not write episodes.json")
    args = ap.parse_args()

    parsed = catalog._parse_feed(fetch(args.feed, args.timeout, "application/rss+xml,*/*"))
    wanted = parsed["episodes"]
    if args.only:
        # Feed ids keep the title's zero padding ("01"), so accept "1" too.
        keep = set()
        for part in args.only.split(","):
            part = part.strip()
            if part:
                keep.add(part)
                keep.add(part.zfill(2))
        wanted = [e for e in wanted if e["id"] in keep]
    if args.limit:
        wanted = wanted[:args.limit]

    links = feed_page_links(args.feed, args.timeout)

    with io.open(args.out, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    existing = {str(e["id"]): e for e in doc.get("episodes", [])}

    total = with_tracks = 0
    failures = []
    for n, e in enumerate(wanted, 1):
        eid = e["id"]
        link = links.get(eid)
        if not link:
            failures.append((eid, "feed item has no page link"))
            print("  [%2d/%d] %-3s no page link" % (n, len(wanted), eid), file=sys.stderr)
            continue
        try:
            tracks = parse_tracklist(fetch(link, args.timeout, "text/html,*/*"))
        except Exception as exc:
            failures.append((eid, str(exc)))
            print("  [%2d/%d] %-3s FAILED %s" % (n, len(wanted), eid, exc), file=sys.stderr)
            continue

        row = existing.get(eid)
        if row is None:
            row = {"id": eid, "title": e["title"], "published": e["published"],
                   "duration": e["duration"], "bytes": e["bytes"], "bitrate": 0,
                   "audio_url": e["audio_url"], "tracks": []}
            doc.setdefault("episodes", []).append(row)
            existing[eid] = row
        row["tracks"] = tracks
        total += len(tracks)
        if tracks:
            with_tracks += 1
        print("  [%2d/%d] %-3s %4d tracks  %s"
              % (n, len(wanted), eid, len(tracks), e["title"][:32]))
        if args.delay and n < len(wanted):
            time.sleep(args.delay)

    if not args.dry_run:
        doc["episodes"].sort(key=lambda r: -catalog._coerce_int(r.get("id"), 0))
        with io.open(args.out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

    print("\n%d of %d episodes got a tracklist" % (with_tracks, len(wanted)))
    print("%d tracks total" % total)
    if failures:
        print("\n%d episode(s) without a tracklist:" % len(failures), file=sys.stderr)
        for eid, why in failures:
            print("  %-4s %s" % (eid, why), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())