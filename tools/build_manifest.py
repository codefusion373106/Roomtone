"""Build episodes.json - the catalog manifest served by catalog.py.

Two jobs:

1. Measure real audio files, so duration/bytes/bitrate are read from the files
   instead of guessed. This is what stops the manifest claiming a 4-minute
   file is 1:02:14.
2. Emit one row per episode, merging any real metadata you supply with
   placeholders for slots you have not filled yet.

Usage
-----
  # measure local files, then fill remaining slots with placeholders
  python tools/build_manifest.py --count 79

  # merge real metadata, with audio_url built from a CDN pattern
  python tools/build_manifest.py --from real.json \\
      --base-url "https://cdn.example.com/ep{id}.mp3"

  # accept a CSV: id,title,published,duration,bytes,audio_url,tracks
  python tools/build_manifest.py --from real.csv --tracks-separator ";"

Rows from --from always win. Placeholder rows are tagged "synthetic": true so
they can be found and replaced later:

  python tools/build_manifest.py --list-synthetic
"""

import argparse
import csv
import io
import json
import os
import struct
import sys
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

AUDIO_EXT = (".mp3", ".m4a", ".mp4", ".aac", ".wav", ".flac", ".ogg", ".opus")

# --------------------------------------------------------------------------
# ID3v2 text frames, so titles can come from the files when present
# --------------------------------------------------------------------------

ID3_FRAMES = {b"TIT2": "title", b"TPE1": "artist", b"TALB": "album", b"TRCK": "track"}


def read_id3(data):
    """Pull title/artist/track out of an ID3v2 tag. Returns {} when absent.

    Tolerant by design: tags are frequently truncated or padded, and a probe
    must never raise out of here.
    """
    out = {}
    if len(data) < 10 or data[:3] != b"ID3":
        return out

    size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | \
           ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
    pos = 10
    # Clamp to what the file actually holds; a bad header must not run us off
    # the end or into padding.
    end = min(10 + size, len(data))

    while pos + 10 <= end:
        fid = data[pos:pos + 4]
        raw_size = data[pos + 4:pos + 8]
        if len(raw_size) < 4 or not all(32 <= b < 127 for b in fid):
            break
        # ID3v2.3/2.4 frame size is a plain 32-bit big-endian integer.
        fsize = struct.unpack(">I", raw_size)[0]
        if fsize <= 0 or pos + 10 + fsize > len(data):
            break
        if fid in ID3_FRAMES:
            body = data[pos + 10:pos + 10 + fsize]
            # first byte is the encoding: 0=ISO-8859-1, 1=UTF-16, 3=UTF-8
            enc = body[0] if body else 0
            raw = body[1:]
            try:
                if enc == 1:
                    text = raw.decode("utf-16", "replace")
                elif enc == 3:
                    text = raw.decode("utf-8", "replace")
                else:
                    text = raw.decode("latin-1", "replace")
            except Exception:
                text = ""
            text = text.split("\x00")[0].strip()
            if text:
                out[ID3_FRAMES[fid]] = text
        pos += 10 + fsize
    return out


def read_id3_trailing(data):
    """ID3v1 trailer, 128 bytes at the end of the file."""
    if len(data) < 128 or data[-128:-125] != b"TAG":
        return {}
    b = data[-128:]
    def field(a, z):
        return b[a:z].split(b"\x00")[0].decode("latin-1", "replace").strip()
    out = {}
    if field(3, 33):
        out["title"] = field(3, 33)
    if field(33, 63):
        out["artist"] = field(33, 63)
    return out


# --------------------------------------------------------------------------
# format probes
# --------------------------------------------------------------------------

_MP3_BITRATE_V1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
_MP3_BITRATE_V2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_frame_header(data, i):
    """Return (frame_len, bitrate, samplerate, samples) for a frame at i, else None."""
    if i + 4 > len(data) or data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
        return None
    ver = (data[i + 1] >> 3) & 0x3
    layer = (data[i + 1] >> 1) & 0x3
    bri = (data[i + 2] >> 4) & 0xF
    sri = (data[i + 2] >> 2) & 0x3
    pad = (data[i + 2] >> 1) & 0x1

    if ver == 1 or layer == 0 or bri in (0, 15) or sri == 3:
        return None

    bitrate = (_MP3_BITRATE_V1 if ver == 3 else _MP3_BITRATE_V2)[bri]
    if not bitrate:
        return None
    rate = _MP3_RATES[ver][sri]

    if layer == 3:                      # Layer I
        bitrate *= 4
        samples = 384
    elif ver == 3:                      # MPEG1 Layer II/III
        samples = 1152
    else:                               # MPEG2 / 2.5 Layer III
        samples = 576

    length = (samples // 8) * bitrate * 1000 // rate + pad
    if length < 24:
        return None
    # side-info size, used to locate a Xing/VBRI header
    side_info = {3: 32, 2: 17, 0: 17}[ver] if layer == 3 else {3: 32, 2: 17, 0: 9}[ver]
    return length, bitrate, rate, samples, side_info


def probe_mp3(data):
    start = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        start = 10 + (((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) |
                      ((data[8] & 0x7F) << 7) | (data[9] & 0x7F))

    first = None
    for i in range(start, min(len(data) - 4, start + 200000)):
        hdr = _mp3_frame_header(data, i)
        if not hdr:
            continue
        # Require a run of consecutive valid frames, so random 0xFF bytes in
        # the audio are not mistaken for a sync word.
        off, ok = i, 0
        while ok < 4:
            h = _mp3_frame_header(data, off)
            if not h:
                break
            off += h[0]
            ok += 1
        if ok == 4:
            first = i
            break
    if first is None:
        return None

    _, bitrate, rate, samples, side_info = _mp3_frame_header(data, first)

    # Xing/Info (after side info) or VBRI (fixed offset 36) for VBR files.
    xing = data.find(b"Xing", first)
    if xing < 0 or xing > first + 200:
        xing = data.find(b"Info", first)
        if xing < 0 or xing > first + 200:
            xing = -1
    vbri = data.find(b"VBRI", first)
    if vbri < 0 or vbri > first + 200:
        vbri = -1

    if xing >= 0 and xing + 16 <= len(data):
        flags = struct.unpack(">I", data[xing + 4:xing + 8])[0]
        if flags & 1:
            frames = struct.unpack(">I", data[xing + 8:xing + 12])[0]
            if frames:
                return {"duration": frames * samples / float(rate),
                        "bitrate": int((len(data) - first) * 8 / (frames * samples / float(rate)) / 1000),
                        "vbr": True}
    if vbri >= 0 and vbri + 26 <= len(data):
        frames = struct.unpack(">I", data[vbri + 14:vbri + 18])[0]
        if frames:
            secs = frames * samples / float(rate)
            return {"duration": secs,
                    "bitrate": int((len(data) - first) * 8 / secs / 1000),
                    "vbr": True}

    # Constant bitrate: derive the whole duration from the file size.
    secs = (len(data) - first) * 8 / float(bitrate * 1000)
    return {"duration": secs, "bitrate": bitrate, "vbr": False}


def probe_wav(data):
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos = 12
    byte_rate = channels = bits = 0
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        csize = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + csize]
        if cid == b"fmt " and len(body) >= 16:
            channels = struct.unpack("<H", body[2:4])[0]
            byte_rate = struct.unpack("<I", body[8:12])[0]
            bits = struct.unpack("<H", body[14:16])[0]
        elif cid == b"data":
            if byte_rate:
                return {"duration": csize / float(byte_rate),
                        "bitrate": int(byte_rate * 8 / 1000),
                        "channels": channels, "bits": bits, "vbr": False}
            return None
        pos += 8 + csize + (csize & 1)
    return None


def probe_flac(data):
    if len(data) < 42 or data[:4] != b"fLaC":
        return None
    pos = 4
    while pos + 4 <= len(data):
        last = data[pos] & 0x80
        btype = data[pos] & 0x7F
        size = int.from_bytes(data[pos + 1:pos + 4], "big")
        if btype == 0 and pos + 4 + 18 <= len(data):
            info = data[pos + 4:pos + 4 + 18]
            rate = int.from_bytes(info[10:13], "big") >> 4
            channels = ((info[12] >> 1) & 0x7) + 1
            # total samples is a 36-bit field straddling byte 13: its top 4
            # bits share that byte with bits-per-sample, so mask them off.
            total = ((info[13] & 0x0F) << 32) | int.from_bytes(info[14:18], "big")
            if rate:
                return {"duration": total / float(rate), "samplerate": rate,
                        "channels": channels, "vbr": True}
            return None
        pos += 4 + size
        if last:
            break
    return None


def probe_mp4(data):
    """Read duration from the mvhd atom of an MP4/M4A/AAC container.

    mvhd layout after the 'mvhd' tag: version+flags (4), creation_time (4),
    modification_time (4), timescale (4), duration (4 or 8).
    """
    i = data.find(b"mvhd")
    if i < 0 or i + 8 > len(data):
        return None
    version = data[i + 4]
    if version == 1:
        if i + 28 > len(data):
            return None
        timescale = struct.unpack(">I", data[i + 16:i + 20])[0]
        duration = struct.unpack(">Q", data[i + 20:i + 28])[0]
    else:
        if i + 24 > len(data):
            return None
        timescale = struct.unpack(">I", data[i + 16:i + 20])[0]
        duration = struct.unpack(">I", data[i + 20:i + 24])[0]
    if not timescale or not duration:
        return None
    return {"duration": duration / float(timescale), "vbr": True}


def probe_file(path):
    """Best-effort metadata for one audio file."""
    size = os.path.getsize(path)
    info = {"bytes": size, "file": os.path.basename(path)}
    try:
        with io.open(path, "rb") as fh:
            head = fh.read(1024 * 256)
            fh.seek(max(0, size - 128))
            tail = fh.read(128)
    except OSError as exc:
        info["error"] = str(exc)
        return info

    ext = os.path.splitext(path)[1].lower()
    result = None
    try:
        if ext == ".mp3" or head[:1] == b"\xff":
            # mp3 duration needs the whole file, not just the head.
            with io.open(path, "rb") as fh:
                blob = fh.read()
            result = probe_mp3(blob)
            result = dict(result or {})
            result.update(read_id3(blob) or {})
            result.update(read_id3_trailing(tail) or {})
        elif head[:4] == b"RIFF":
            result = probe_wav(head)
        elif head[:4] == b"fLaC":
            result = probe_flac(head)
        elif head[4:8] == b"ftyp":
            result = probe_mp4(head)
            if result is None:
                # mvhd can sit deep in the moov atom, past the head buffer.
                with io.open(path, "rb") as fh:
                    result = probe_mp4(fh.read(8 * 1024 * 1024))
    except Exception as exc:                           # pragma: no cover - defensive
        info["error"] = "probe failed: %s" % exc
        return info

    if not result:
        info.setdefault("error", "unsupported format")
        return info

    info.update(result)
    return info


# --------------------------------------------------------------------------
# manifest assembly
# --------------------------------------------------------------------------

PLACEHOLDER_WORDS = [
    "Slow Circuit", "Night Bus", "Pale Signal", "Glass Corridor", "Low Tide",
    "Quiet Machine", "Paper Static", "Amber Hours", "Drift Chamber", "Soft Transit",
    "Winter Terminal", "Blue Ledger", "Halfway Light", "Copper Rain", "Long Exposure",
    "Tape Delay", "Quiet Motion", "North Window", "Undertow", "Faint Signal",
]


def tracklist_for(number, count):
    """Deterministic placeholder tracklist so runs are reproducible."""
    picks = []
    for k in range(count):
        word = PLACEHOLDER_WORDS[(number * 7 + k * 3) % len(PLACEHOLDER_WORDS)]
        picks.append("Placeholder Artist %02d - %s" % ((number + k) % 97 + 1, word))
    return picks


def distribute_total(total, buckets, minimum):
    """Spread `total` items over `buckets`, never below `minimum` each."""
    per = minimum
    extra = total - per * buckets
    if extra < 0:
        raise ValueError("cannot fit %d tracks into %d episodes at %d each"
                         % (total, buckets, minimum))
    counts = [per] * buckets
    for i in range(extra):
        counts[i % buckets] += 1
    return counts


def build_audio_url(pattern, episode_id):
    try:
        return pattern.format(id=episode_id, ep=episode_id, i=episode_id)
    except (KeyError, IndexError, ValueError):
        return pattern


# Rows carrying one of these markers are still waiting for a real remote URL.
# The app serves no audio of its own, so a /static/... URL is treated the same
# way as an unfilled placeholder rather than silently emitted.
PLACEHOLDER_MARKERS = ("REPLACE-ME", "/static/audio/", "http://REPLACE-ME")


def needs_url(url):
    return (not url) or any(m in url for m in PLACEHOLDER_MARKERS)


def _int_or(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def fetch_feed(url, timeout=30):
    """Download a podcast feed.

    A browser User-Agent is required: musicforprogramming.net answers 403 to
    default urllib agents.
    """
    request = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"),
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def episodes_from_feed(url, timeout=30):
    """Parse a feed into manifest rows, reusing the app's own parser.

    catalog.py already validates, sorts and normalises feed items, so building a
    manifest from a feed and serving one at runtime cannot drift apart.
    """
    import catalog  # local module, on sys.path via BASE_DIR above

    parsed = catalog._parse_feed(fetch_feed(url, timeout))
    rows = []
    for e in parsed["episodes"]:
        if needs_url(e["audio_url"]):
            continue
        rows.append({
            "id": e["id"],
            "title": e["title"],
            "published": e["published"],
            "duration": e["duration"],
            "bytes": e["bytes"],
            "bitrate": e["bitrate"],
            "audio_url": e["audio_url"],
            # Feeds carry no tracklists. Leaving this empty is honest; the
            # generator's placeholder step can still fill slots if asked.
            "tracks": [],
        })
    return rows, parsed


def parse_source_rows(path, tracks_sep):
    if not path:
        return []
    if path.lower().endswith(".csv"):
        with io.open(path, "r", encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))
    with io.open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("episodes", [])
    return data


def coerce_row(row, tracks_sep):
    if not isinstance(row, dict):
        return None
    eid = str(row.get("id") or "").strip()
    if not eid:
        return None

    url = str(row.get("audio_url") or row.get("url") or "").strip()
    if not url:
        return None

    tracks = row.get("tracks") or row.get("tracklist") or []
    if isinstance(tracks, str):
        tracks = [t.strip() for t in tracks.split(tracks_sep) if t.strip()]
    tracks = [str(t).strip() for t in tracks if str(t).strip()]

    def num(key):
        try:
            return int(row.get(key))
        except (TypeError, ValueError):
            return 0

    out = {
        "id": eid,
        "title": str(row.get("title") or "Episode %s" % eid).strip(),
        "published": (str(row["published"]).strip() if row.get("published") else None),
        "duration": num("duration"),
        "bytes": num("bytes"),
        "bitrate": num("bitrate"),
        "audio_url": url,
        "tracks": tracks,
    }
    if row.get("synthetic"):
        out["synthetic"] = True
    return out


def main():
    ap = argparse.ArgumentParser(description="Build episodes.json")
    ap.add_argument("--count", type=int, default=79, help="total episode slots")
    ap.add_argument("--from", dest="src", help="JSON or CSV of real episode rows")
    ap.add_argument("--from-rss", metavar="URL",
                    help="build every row from a podcast feed (e.g. the "
                         "musicforprogramming.net rss.xml)")
    ap.add_argument("--feed-timeout", type=float, default=30.0,
                    help="seconds to wait for --from-rss (default 30)")
    ap.add_argument("--tracks-separator", default="|", help="track separator for CSV (default |)")
    ap.add_argument("--base-url", help="audio_url pattern, e.g. https://cdn/ep{id}.mp3")
    ap.add_argument("--probe", help="directory of real audio to measure")
    ap.add_argument("--total-tracks", type=int, help="total tracks to distribute across slots")
    ap.add_argument("--tracks-min", type=int, default=17, help="min placeholder tracks per slot")
    ap.add_argument("--out", default=os.path.join(BASE_DIR, "episodes.json"))
    ap.add_argument("--list-synthetic", action="store_true", help="list synthetic rows and exit")
    ap.add_argument("--list-missing", action="store_true",
                    help="list every row still awaiting a real audio_url, and exit")
    args = ap.parse_args()

    if args.list_missing:
        with io.open(args.out, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        rows = doc.get("episodes", [])
        pending = [e for e in rows if needs_url(e.get("audio_url", ""))]
        for e in sorted(pending, key=lambda x: -_int_or(x.get("id"))):
            print("%-4s %-46s %s%s" % (
                e.get("id"), e.get("audio_url", "")[:46],
                e.get("title", ""), "  [synthetic metadata]" if e.get("synthetic") else ""))
        print("\n%d of %d rows still need a real audio_url" % (len(pending), len(rows)))
        if pending:
            print('fix them with:  python tools/build_manifest.py --from %s \\\n'
                  '                    --base-url "https://your-host/ep{id}.mp3"'
                  % os.path.relpath(args.out, BASE_DIR))
        return 0

    if args.list_synthetic:
        with io.open(args.out, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        syn = [e for e in doc.get("episodes", []) if e.get("synthetic")]
        if not syn:
            print("no synthetic rows")
        for e in syn:
            print("%s  %s" % (e["id"], e["audio_url"]))
        print("%d synthetic of %d total" % (len(syn), len(doc.get("episodes", []))))
        return 0

    episodes = []
    seen = set()

    # 1. real rows supplied by the user always win
    for row in parse_source_rows(args.src, args.tracks_separator):
        out = coerce_row(row, args.tracks_separator)
        if out and out["id"] not in seen:
            # A row still holding a placeholder or local-audio URL gets its
            # real address from --base-url, so `--from episodes.json` plus a
            # new CDN pattern is enough to point the whole catalog remotely.
            # `synthetic` is left alone: it flags invented metadata, which a
            # real URL does not make true.
            if args.base_url and needs_url(out["audio_url"]):
                out["audio_url"] = build_audio_url(args.base_url, out["id"])
            seen.add(out["id"])
            episodes.append(out)

    # 1b. a live podcast feed: the whole catalog, with no invented rows
    if args.from_rss:
        try:
            feed_rows, parsed = episodes_from_feed(args.from_rss, args.feed_timeout)
        except Exception as exc:
            ap.error("could not read feed %s: %s" % (args.from_rss, exc))
        added = 0
        for row in feed_rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            episodes.append(row)
            added += 1
        skipped = len(parsed["skipped"])
        total_bytes = sum(r["bytes"] for r in feed_rows)
        print("feed %s" % args.from_rss)
        print("  %d episodes, %d added, %d rejected, %d skipped as duplicate"
              % (len(feed_rows), added, skipped, len(feed_rows) - added))
        print("  %.1f GB of audio, %.1f hours, %d tracklists (feeds have none)"
              % (total_bytes / 1073741824.0,
                 sum(r["duration"] for r in feed_rows) / 3600.0,
                 sum(len(r["tracks"]) for r in feed_rows)))
        for note in parsed["skipped"][:5]:
            print("  rejected: %s" % note, file=sys.stderr)

    # 2. real files on disk, measured
    if args.probe:
        if not args.base_url:
            ap.error("--probe measures local files, but this app serves no local "
                     "audio; pass --base-url so the rows get a real remote URL")
        if not os.path.isdir(args.probe):
            ap.error("--probe directory not found: %s" % args.probe)
        files = sorted(f for f in os.listdir(args.probe) if f.lower().endswith(AUDIO_EXT))
        if not files:
            ap.error("no audio files in %s" % args.probe)
        for name in files:
            info = probe_file(os.path.join(args.probe, name))
            if info.get("error") or not info.get("duration"):
                print("skip %s (%s)" % (name, info.get("error", "no duration")), file=sys.stderr)
                continue
            eid = str(len(episodes) + 1)
            url = build_audio_url(args.base_url, eid)
            if url in [e["audio_url"] for e in episodes]:
                continue
            seen.add(eid)
            episodes.append({
                "id": eid,
                "title": info.get("title") or "Episode %s" % eid,
                "published": None,
                "duration": int(round(info["duration"])),
                "bytes": info["bytes"],
                "bitrate": info.get("bitrate", 0),
                "audio_url": url,
                "tracks": [],
            })
            print("probed %-24s %7.1fs  %6.1f MB  %s" % (
                name, info["duration"], info["bytes"] / 1048576.0,
                "vbr" if info.get("vbr") else "cbr"))

    # 3. placeholders for unfilled slots
    missing = args.count - len(episodes)
    if missing > 0:
        have_tracks = sum(len(e["tracks"]) for e in episodes)
        target = args.total_tracks
        if target is None:
            target = have_tracks + missing * args.tracks_min
        remaining = max(0, target - have_tracks)

        counts = distribute_total(remaining, missing, 0) if missing else []

        existing = set()
        for e in episodes:
            try:
                existing.add(int(e["id"]))
            except (TypeError, ValueError):
                pass
        numbers = [n for n in range(args.count, 0, -1) if n not in existing]
        numbers = numbers[:missing]

        for n, count in zip(numbers, counts):
            url = (build_audio_url(args.base_url, n) if args.base_url
                   else "https://REPLACE-ME/ep%d.mp3" % n)
            episodes.append({
                "id": str(n),
                "title": "Episode %d: %s" % (n, PLACEHOLDER_WORDS[n % len(PLACEHOLDER_WORDS)]),
                "published": None,
                "duration": 0,
                "bytes": 0,
                "bitrate": 0,
                "audio_url": url,
                "tracks": tracklist_for(n, count),
                "synthetic": True,
            })
        print("added %d placeholder episodes (%d tracks)" % (missing, sum(counts)))

    # newest first, matching catalog.py's ordering
    def sort_key(e):
        try:
            return int(e["id"])
        except (TypeError, ValueError):
            return 0
    episodes.sort(key=sort_key, reverse=True)

    real = [e for e in episodes if not e.get("synthetic")]
    synthetic_count = len(episodes) - len(real)
    if synthetic_count:
        note = (
            "Catalog manifest fetched by catalog.py at runtime. duration is seconds, "
            "bytes is the exact file size. %d row(s) tagged \"synthetic\": true are "
            "placeholders with no real audio behind them - replace audio_url and "
            "duration, then drop the synthetic flag. Run "
            "`python tools/build_manifest.py --list-synthetic` to find them."
            % synthetic_count
        )
    else:
        note = (
            "Catalog manifest built from the musicForProgramming podcast feed. "
            "duration is seconds and bytes is the exact file size. Audio streams "
            "straight from audio_url; nothing is proxied or stored here."
        )
    doc = {
        "version": 1,
        "updated": None,
        "note": note,
        "episodes": episodes,
    }

    with io.open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    total_tracks = sum(len(e["tracks"]) for e in episodes)
    print("\nwrote %s" % args.out)
    print("  episodes : %d (%d real, %d synthetic)"
          % (len(episodes), len(real), len(episodes) - len(real)))
    print("  tracks   : %d" % total_tracks)
    print("  duration : %.1f h" % (sum(e["duration"] for e in episodes) / 3600.0))
    if not args.base_url and any(e.get("synthetic") for e in episodes):
        print("\n  NOTE: synthetic rows use https://REPLACE-ME/... and will not play.")
        print("        Re-run with --base-url \"https://your-cdn/ep{id}.mp3\".")
    return 0


if __name__ == "__main__":
    sys.exit(main())