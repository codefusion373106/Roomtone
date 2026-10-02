"""Episode catalog loading.

The catalog lives in a manifest that is fetched at runtime, so 79 episodes of
metadata can be served without committing anything to the repo. Two formats are
accepted for the same URL:

1. a JSON manifest - ``{"episodes": [...]}`` or a bare list
2. an RSS/Atom podcast feed - the standard interchange format, and what the
   musicForProgramming() catalogue itself publishes at ``/rss.xml``

Resolution order:

1. ``MFP_MANIFEST_URL``  - remote manifest (any static host: CDN, S3, GitHub raw)
2. ``episodes.json``     - local manifest, used as the dev/default fallback
3. the last good payload - served stale rather than failing outright

Only metadata is read here. Audio always streams straight from ``audio_url`` to
the browser, so no audio is ever proxied or stored by this app.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_MANIFEST = os.path.join(BASE_DIR, "episodes.json")

MANIFEST_URL = os.environ.get("MFP_MANIFEST_URL", "").strip()
# A feed refresh is cheap for us and free for the publisher, but the catalog is
# immutable once an episode is out, so the TTL can be generous. One hour means
# a 4-worker host makes at most a handful of upstream requests an hour no
# matter how many visitors it serves.
CACHE_TTL = int(os.environ.get("MFP_CATALOG_TTL", "3600"))
FETCH_TIMEOUT = float(os.environ.get("MFP_CATALOG_TIMEOUT", "8"))
MAX_MANIFEST_BYTES = int(os.environ.get("MFP_CATALOG_MAX_BYTES", str(4 * 1024 * 1024)))
# Identifies this app to the feed host. Deliberately not a browser UA: the feed
# is a public RSS document, so there is nothing to spoof, and naming ourselves
# honestly is the point.
USER_AGENT = os.environ.get("MFP_USER_AGENT", "Roomtone/1.0 (+catalog)")

_lock = threading.Lock()
_cache = {"payload": None, "fetched_at": 0.0, "etag": None}


class CatalogError(Exception):
    pass


def _coerce_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_episode(raw, index):
    """Coerce one manifest entry into the shape the client expects.

    Raises CatalogError when the entry is unusable, so a single bad row is
    reported rather than silently producing a broken sidebar.
    """
    if not isinstance(raw, dict):
        raise CatalogError("episode #%d is not an object" % index)

    audio_url = (raw.get("audio_url") or raw.get("url") or "").strip()
    if not audio_url:
        raise CatalogError("episode #%d has no audio_url" % index)
    if not audio_url.startswith(("http://", "https://", "/")):
        raise CatalogError("episode #%d audio_url must be http(s) or root-relative" % index)

    episode_id = raw.get("id")
    if episode_id is None or str(episode_id).strip() == "":
        raise CatalogError("episode #%d has no id" % index)
    episode_id = str(episode_id).strip()

    title = (raw.get("title") or "").strip() or ("Episode %s" % episode_id)

    tracks = raw.get("tracks") or raw.get("tracklist") or []
    if isinstance(tracks, str):
        tracks = [t.strip() for t in tracks.split("\n") if t.strip()]
    if not isinstance(tracks, list):
        raise CatalogError("episode %s tracks must be a list" % episode_id)
    tracks = [str(t).strip() for t in tracks if str(t).strip()]

    duration = _coerce_int(raw.get("duration"), None)
    if duration is None:
        duration = 0

    return {
        "id": episode_id,
        "title": title,
        "published": (raw.get("published") or None),
        "duration": duration,
        "bytes": _coerce_int(raw.get("bytes"), 0),
        "bitrate": _coerce_int(raw.get("bitrate"), 0),
        "audio_url": audio_url,
        "tracks": tracks,
    }


def _finalize(episodes_raw, meta=None):
    """Shared tail of both parsers: validate, sort newest first, collect skips."""
    episodes = []
    skipped = []
    for i, raw in enumerate(episodes_raw):
        try:
            episodes.append(_normalize_episode(raw, i))
        except CatalogError as exc:
            skipped.append(str(exc))

    # Newest first, so a 79-episode sidebar opens on the latest mix.
    episodes.sort(key=lambda e: _coerce_int(e["id"], 0) or 0, reverse=True)

    return {
        "episodes": episodes,
        "meta": meta or {},
        "skipped": skipped,
    }


def _parse_manifest(text):
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CatalogError("manifest is not valid JSON: %s" % exc)

    if isinstance(data, list):
        episodes_raw = data
        meta = {}
    elif isinstance(data, dict):
        episodes_raw = data.get("episodes") or []
        meta = {
            "version": data.get("version"),
            "updated": data.get("updated"),
        }
    else:
        raise CatalogError("manifest must be an object or a list")

    if not isinstance(episodes_raw, list):
        raise CatalogError("manifest 'episodes' must be a list")

    result = _finalize(episodes_raw, meta)
    result["is_feed"] = False
    return result


# --------------------------------------------------------------------------
# RSS / Atom podcast feeds
# --------------------------------------------------------------------------

_NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "atom": "http://www.w3.org/2005/Atom",
}


def _q(prefix, tag):
    return "{%s}%s" % (_NS[prefix], tag)


def _first_text(node, *tags):
    for tag in tags:
        el = node.find(tag)
        if el is not None and el.text and el.text.strip():
            return el.text.strip()
    return ""


def _feed_duration(value):
    """itunes:duration is either bare seconds or H:MM:SS / MM:SS."""
    text = (value or "").strip()
    if not text:
        return 0
    if ":" not in text:
        return _coerce_int(text, 0) or 0
    parts = text.split(":")
    if len(parts) > 3:
        return 0
    total = 0
    for part in parts:
        number = _coerce_int(part, None)
        if number is None:
            return 0
        total = total * 60 + number
    return total


def _feed_id(title, guid, audio_url, index):
    """Feeds carry no episode id, so derive a numeric one for correct sorting.

    Titles like "Episode 79: Corticyte" give the number directly. The guid/URL
    fallback strips the file extension first, otherwise every ``...mp3`` URL
    would contribute its codec digit ("mp3") as the episode number.
    """
    match = re.search(r"\d+", title or "")
    if match:
        return match.group(0)

    for candidate in (guid, audio_url):
        stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", candidate or "")
        match = re.search(r"\d+", stem)
        if match:
            return match.group(0)

    return str(index + 1)


def _iso_date(value):
    if not value:
        return None
    text = str(value).strip()
    direct = re.match(r"\d{4}-\d{2}-\d{2}", text)
    if direct:
        return direct.group(0)
    try:
        return parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError, IndexError, TypeError):
        return None


def _feed_item_to_raw(item, index):
    enclosure = item.find("enclosure")
    audio_url = enclosure.get("url") if enclosure is not None else None
    nbytes = enclosure.get("length") if enclosure is not None else None

    if not audio_url:
        # Atom spells the same idea <link rel="enclosure" href="..."/>
        for link in item.findall(_q("atom", "link")):
            if link.get("rel") == "enclosure" and link.get("href"):
                audio_url = link.get("href")
                nbytes = link.get("length")
                break

    title = _first_text(item, "title", _q("atom", "title"))
    guid = _first_text(item, "guid", "id") or (item.get("href") or "")
    published = _first_text(
        item, "pubDate", "published", "updated", _q("atom", "published"),
        _q("atom", "updated"))
    duration = _first_text(item, _q("itunes", "duration"))

    return {
        "id": _feed_id(title, guid, audio_url, index),
        "title": title,
        "published": _iso_date(published),
        "duration": _feed_duration(duration),
        "bytes": _coerce_int(nbytes, 0) or 0,
        "audio_url": (audio_url or "").strip(),
        # Feeds do not carry tracklists. Leaving this empty keeps stats honest
        # rather than inventing one.
        "tracks": [],
    }


def _parse_feed(text):
    # A DOCTYPE with entity declarations is how "billion laughs" expansion
    # bombs are delivered. MAX_MANIFEST_BYTES caps the source, but not the
    # expanded form, so refuse these outright - real podcast feeds do not need
    # custom entities.
    if re.search(r"<!ENTITY", text, re.IGNORECASE):
        raise CatalogError("feed declares XML entities; refusing to parse")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise CatalogError("feed is not valid XML: %s" % exc)

    kind = root.tag.split("}")[-1].lower()
    meta = {}
    if kind == "rss":
        channel = root.find("channel")
        if channel is None:
            raise CatalogError("rss feed has no <channel>")
        items = channel.findall("item")
        meta = {
            "version": None,
            "updated": _iso_date(_first_text(channel, "lastBuildDate", "pubDate")),
            "feed_title": _first_text(channel, "title"),
        }
    elif kind == "feed":
        items = root.findall(_q("atom", "entry"))
        meta = {
            "version": None,
            "updated": _iso_date(_first_text(root, _q("atom", "updated"))),
            "feed_title": _first_text(root, _q("atom", "title")),
        }
    else:
        raise CatalogError("not a podcast feed (root element is <%s>)" % kind)

    if not items:
        raise CatalogError("feed contains no items")

    raws = [_feed_item_to_raw(item, i) for i, item in enumerate(items)]
    result = _finalize(raws, meta)
    result["is_feed"] = True
    return result


def _local_tracklists():
    """id -> tracks from the bundled manifest, or {} when it has none.

    A feed carries no tracklists, but the ones published on the episode pages do
    not change once an episode is out, so the bundled manifest is where they are
    kept.
    """
    try:
        parsed = _parse_manifest(_read_local())
    except (CatalogError, OSError, ValueError):
        return {}
    return {e["id"]: e["tracks"] for e in parsed["episodes"] if e["tracks"]}


def _inherit_local_tracklists(episodes):
    """Fill trackless feed episodes from the bundled manifest.

    This lets ``MFP_MANIFEST_URL`` point straight at the live feed and still
    show tracklists: the feed stays the source of truth for anything that can
    change (audio URL, duration, size), while tracklists are merged in locally
    rather than lost. ``compute_stats`` runs after this, so the totals already
    reflect the merged lists.
    """
    known = _local_tracklists()
    if not known:
        return
    for e in episodes:
        if not e["tracks"]:
            tracks = known.get(e["id"])
            if tracks:
                e["tracks"] = list(tracks)


def _parse_any(text):
    """Accept a JSON manifest or an RSS/Atom feed at the same URL."""
    if text.lstrip("﻿ \t\r\n").startswith("<"):
        return _parse_feed(text)
    try:
        return _parse_manifest(text)
    except CatalogError as json_error:
        # A feed can arrive with a byte-order mark or leading junk that defeats
        # the sniff above; give XML a second chance before blaming JSON.
        try:
            return _parse_feed(text)
        except CatalogError:
            raise json_error


def _read_local():
    with open(LOCAL_MANIFEST, "r", encoding="utf-8") as fh:
        return fh.read()


def _fetch_remote(etag=None):
    """Fetch the remote manifest.

    Returns ``(text, etag, not_modified)``. ``not_modified`` is True when the
    server answered 304, which means our cached copy is still current - that is
    not the same as having no data, so it must not fall through to the local
    fallback.
    """
    if not MANIFEST_URL.lower().startswith(("http://", "https://")):
        raise CatalogError("MFP_MANIFEST_URL must be http:// or https://")

    request = urllib.request.Request(MANIFEST_URL, headers={"User-Agent": USER_AGENT})
    if etag:
        request.add_header("If-None-Match", etag)

    try:
        resp = urllib.request.urlopen(request, timeout=FETCH_TIMEOUT)
    except urllib.error.HTTPError as exc:
        # urlopen raises for any non-2xx, and 304 is not 2xx - so "still
        # current" arrives as an exception rather than a response.
        if exc.code == 304:
            return None, exc.headers.get("ETag") or etag, True
        raise

    with resp:
        declared = resp.headers.get("Content-Length")
        if declared and int(declared) > MAX_MANIFEST_BYTES:
            raise CatalogError("manifest exceeds %d bytes" % MAX_MANIFEST_BYTES)
        raw = resp.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise CatalogError("manifest exceeds %d bytes" % MAX_MANIFEST_BYTES)
        return raw.decode("utf-8", "replace"), resp.headers.get("ETag"), False


def _build(text, source):
    parsed = _parse_any(text)
    # Only the runtime feed path inherits tracklists. build_manifest.py calls
    # the parser directly and must keep seeing a feed's true contents.
    if parsed.get("is_feed"):
        _inherit_local_tracklists(parsed["episodes"])

    # An intentionally empty catalog is fine, but a manifest where every entry
    # was rejected is a broken manifest - surfacing an empty sidebar with no
    # explanation would be worse than falling back.
    if not parsed["episodes"] and parsed["skipped"]:
        raise CatalogError(
            "no usable episodes (%d rejected: %s)"
            % (len(parsed["skipped"]), "; ".join(parsed["skipped"][:3]))
        )

    payload = {
        "episodes": parsed["episodes"],
        "meta": parsed["meta"],
        "skipped": parsed["skipped"],
        "source": source,
        "updated": parsed["meta"].get("updated"),
    }
    payload["stats"] = compute_stats(parsed["episodes"])
    return payload


def compute_stats(episodes):
    """Real totals, so the sidebar header stops advertising made-up numbers."""
    total_seconds = sum(e["duration"] for e in episodes)
    return {
        "episodes": len(episodes),
        "tracks": sum(len(e["tracks"]) for e in episodes),
        "seconds": total_seconds,
        "hours": round(total_seconds / 3600, 1),
        "bytes": sum(e["bytes"] for e in episodes),
    }


def load_catalog(refresh=False):
    """Return the catalog payload, using a TTL cache unless refresh is forced.

    Resolution order, best source first:
      1. remote manifest, freshly fetched
      2. last good remote copy (stale beats a 2-episode dev manifest)
      3. bundled local manifest
      4. raise, and let the caller surface the reason
    """
    now = time.time()
    with _lock:
        fresh_enough = (_cache["payload"] is not None) and (now - _cache["fetched_at"] < CACHE_TTL)
        if fresh_enough and not refresh:
            return dict(_cache["payload"], cached=True)

        etag = _cache["etag"] if refresh else None
        errors = []

        def commit(payload):
            payload["errors"] = errors
            payload["fetched_at"] = now
            _cache["payload"] = payload
            _cache["fetched_at"] = now
            return dict(payload, cached=False)

        def stale():
            _cache["fetched_at"] = now
            return dict(_cache["payload"], cached=True, stale=True, errors=errors)

        # 1. remote
        if MANIFEST_URL:
            text = None
            not_modified = False
            try:
                text, new_etag, not_modified = _fetch_remote(etag)
                if new_etag:
                    _cache["etag"] = new_etag
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, CatalogError) as exc:
                errors.append("remote: %s" % exc)

            if not_modified:
                if _cache["payload"] is not None:
                    # Server says our copy is still current - keep serving it.
                    return dict(_cache["payload"], cached=True, not_modified=True, errors=errors)
            elif text is not None:
                try:
                    return commit(_build(text, MANIFEST_URL))
                except CatalogError as exc:
                    # A corrupt or unusable remote manifest is just as broken as
                    # an unreachable one - fall through instead of failing.
                    errors.append("remote manifest: %s" % exc)

        # 2. last good copy of the authoritative source
        if _cache["payload"] is not None:
            return stale()

        # 3. bundled local manifest
        try:
            local_text = _read_local()
        except OSError as exc:
            errors.append("local: %s" % exc)
            raise CatalogError("; ".join(errors) or "no manifest available")

        try:
            return commit(_build(local_text, "local"))
        except CatalogError as exc:
            errors.append("local manifest: %s" % exc)
            raise CatalogError("; ".join(errors))


def catalog_config():
    return {
        "manifest_url": MANIFEST_URL or None,
        "local_manifest": os.path.basename(LOCAL_MANIFEST),
        "ttl": CACHE_TTL,
        "remote_enabled": bool(MANIFEST_URL),
    }