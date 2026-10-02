# Roomtone

**Live:** https://roomtone-gamma.vercel.app/

A focus timer and long-form audio player. Runs entirely on Vercel's free tier.

Set a timer, put on a continuous ambient mix, and leave it running. No account,
no database, no build step.

## What it does

- **Pomodoro timer** with presets and a custom work/break/cycles mode. Preferences
  persist in `localStorage`.
- **79 long-form mixes**, roughly three hours each, playable from a single catalog.
- **1,380 track names** per episode, so you can find the section you want.
- **Live waveform** driven by a Web Audio analyser. Falls back to a flat line when
  the CDN withholds CORS headers, rather than showing nothing.
- **Command palette** (`Ctrl`/`Cmd` + `K`) to drive playback without the mouse.
- Search across episodes and tracks, browser notifications on timer completion.

## Running it

```bash
pip install -r requirements.txt
python app.py          # http://localhost:5000
```

Debug mode is **off** by default — the Werkzeug debugger is a remote shell, so a
default-on debug flag would expose the host the moment this ran somewhere public.
For local debugging:

```bash
MFP_DEBUG=1 python app.py
```

## Deploying

Zero-config Flask: `app.py` exports a top-level `app`, which Vercel detects and
runs as a function. There is no build command and no output directory.

```bash
npm i -g vercel
vercel --prod
```

After the first deploy, confirm **Settings → Deployment Protection is off**.
Otherwise the URL resolves for you and 404s for everyone else, with no error.

`vercel.json`, `.python-version` and `.vercelignore` are the only supporting files.
Full deployment notes, including Render/Railway/Fly.io fallbacks and a VPS route,
are in [`deploy/README.md`](deploy/README.md).

`.python-version` has to name a version Vercel still ships, which is the part worth
watching. Vercel's Python runtime installs dependencies with **uv**, and uv reads
that file to pick the interpreter. Vercel offers 3.12 (default), 3.13 and 3.14 —
no 3.11 — so pinning 3.11 fails the build with `No interpreter found for Python
3.11` before a single dependency is resolved. The pin exists only to stop Vercel
drifting to a newer default; local dev is unaffected, since `python app.py` runs
whatever interpreter is on `PATH`.

## How it stays free

Audio never touches this server. Every episode streams straight from the source
CDN to the visitor's browser, so all 10.7 GB of it bypasses Vercel entirely.

Per page load the server sends ~44 KB: ~17 KB of gzipped HTML/CSS/JS and a ~27 KB
gzipped catalog. The catalog is cached for an hour, so upstream sees a handful of
requests regardless of how many people are listening.

One dependency, and it's Flask. `catalog.py` fetches and parses the feed with
`urllib` and `xml.etree` from the standard library.

## Layout

| | |
|---|---|
| `app.py` | routes, gzip, security headers, static/audio guard |
| `catalog.py` | feed parsing, ETag revalidation, disk cache, local fallback |
| `episodes.json` | bundled catalog, used when upstream is unreachable |
| `templates/`, `static/` | the app itself |
| `tools/` | catalog refresh scripts, build-time only, not deployed |

The three columns are a grid, and the left and right ones are `position: sticky`
capped to the viewport. A sticky grid item is constrained to its grid area, so the
footer has to live *outside* `.shell` — as a final row of that grid it sat exactly
where the columns come to rest at the bottom of the page, and the library stats
painted over the credits. For the same reason `.col-left` scrolls internally: it is
capped shorter than its own content on a short window, and visible overflow would
just paint the lower panels outside the column.

## Refreshing the catalog

`episodes.json` is a build-time artifact, not a runtime one:

```bash
python tools/build_manifest.py --from-rss https://musicforprogramming.net/rss.xml
python tools/scrape_tracklists.py
```

The scraper downloads 79 episode pages and takes about 30 seconds. Run it when new
episodes are published.

## Content

Music and tracklists come from the [Music for Programming](https://musicforprogramming.net)
podcast by Datashette. This is an unofficial player for it — no affiliation, and
the audio is hotlinked from their CDN rather than rehosted, the same model as any
podcast client. That podcast is where this whole idea came from: Roomtone is
built around their three-hour mixes as a focus session, and the credits in the
footer say so.

That last point is worth taking seriously: every visitor's playback is a request
against Datashat's bandwidth. If this picks up real traffic, tell them rather than
letting it happen quietly.

## Credits

- **Built by** CodeFusion — the developer behind Roomtone.
- **Inspired by** [Music for Programming](https://musicforprogramming.net) by
  Datashette, whose mixes this player streams.

## Configuration

All optional. Defaults live in `app.py` (branding, timeouts) and `catalog.py`
(catalog fetching).

| Variable | Default | |
|---|---|---|
| `MFP_APP_NAME` | `Roomtone` | shown in the title and header |
| `MFP_APP_TAGLINE` | `long-form mixes for quiet work` | header subtitle |
| `MFP_MANIFEST_URL` | *(unset)* | remote catalog; **empty disables remote fetching**, leaving the bundled `episodes.json` to serve |
| `MFP_CATALOG_TTL` | `3600` | cache seconds |
| `MFP_REFRESH_COOLDOWN` | `60` | caps the refresh endpoint to one upstream fetch per minute |
| `MFP_DEBUG` | `0` | **leave off in production** |

`MFP_MANIFEST_URL` has no default on purpose: with it unset the app never makes
an outbound request, so a fresh clone runs entirely offline against
`episodes.json`. `vercel.json` sets it for the deployed instance, which is what
enables live catalog updates there.