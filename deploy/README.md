# Deploying Roomtone

Flask plus a catalog fetch. No database, no object storage, no build step, no
process to supervise on Vercel.

**This app deploys to Vercel** on the free `*.vercel.app` subdomain, for a
total cost of zero. That section below is the one that matters; the others are
kept as fallbacks.

| | Cost | Setup |
|---|---|---|
| **Vercel (in use)** | **$0** | `vercel --prod`, nothing else |
| Render free | $0 | push repo, click through a blueprint |
| Railway | $0 ($5 credit) | `railway init && railway up` |
| Fly.io | ~$0 (allowance varies) | `fly launch` |
| Own Linux VPS | ~$5/mo | below |
| Custom domain | ~$10-15/yr | optional, add later |

TLS is issued automatically on the `vercel.app` subdomain, so a domain is not
required to start - see "Adding a domain later" if you change your mind.

## Why the cost is genuinely near zero

Audio never passes through this host. Every episode streams directly from
`datashat.net` to the visitor's browser, so all 10.7 GB of audio bypasses the
server entirely.

What Vercel *does* send per visitor, measured gzip:

| | |
|---|---|
| HTML + CSS + JS | ~17 KB (66 KB uncompressed) |
| catalog JSON | ~27 KB (67 KB raw) |
| **audio** | **0 KB** |

That is roughly **44 KB per page load**. Vercel's free transfer allowance runs
to millions of page loads. The catalog TTL means upstream traffic is a handful
of requests per hour regardless of how many people are listening.

Where the money goes:

- **Software: free, forever.** Flask, gunicorn and nginx are all free.
- **Hosting: free on Vercel's Hobby tier**, which is what this deploys to. No
  card, no server to rent.
- **Domain: optional.** ~$10-15/year. Skipping it costs nothing but a
  less memorable address - `vercel.app` already has TLS.
- **TLS: free.** Automatic on Vercel; Let's Encrypt via certbot if you ever
  move to a VPS.

The one caveat is the audio bandwidth, and it is not yours: every visitor's
playback is a request against Datashat's bandwidth, not Vercel's. See "One
thing to be aware of" at the end.

If you later move to a VPS, that is the step that starts costing money -
roughly $4-6/month - and it is the only step on this list that does.

## Deploying to Vercel

This is the chosen path. `app.py` exports a top-level `app`, which is an
entrypoint Vercel auto-detects, so there is no build command, no output
directory and no gunicorn to configure:

```bash
npm i -g vercel
vercel login
vercel --prod            # first run creates the project; prints the URL
```

You get `https://<project>-<hash>.vercel.app` with TLS already issued. That is
the whole setup.

Three files in the repo root support this and nothing else:

| File | Job |
|---|---|
| `vercel.json` | env vars, `maxDuration`, bundle `excludeFiles` |
| `.python-version` | pins 3.11 so prod matches local |
| `.vercelignore` | keeps 13.5 MB of local MP3s out of the upload |

**Check this after the first deploy:** Settings -> Deployment Protection must be
off. If Vercel Authentication is on, only Vercel account holders can view the
site - the URL loads, but for you and nobody else. There is no application-level
password in this app, so that setting is the only thing between your focus app
and the public.

`gunicorn` stays in `requirements.txt` even though Vercel never runs it. Vercel
imports `app` as a WSGI callable directly; gunicorn is only for the VPS route
below. It costs nothing to leave installed.

To verify a deploy:

```bash
curl -s <your-url>/healthz
curl -s <your-url>/api/episodes | head -c 200
curl -sI <your-url>/api/episodes -H 'Accept-Encoding: gzip' | grep -i content-encoding
curl -sI <your-url>/static/audio/track_01.mp3     # expect 404
```

If pages render with a 500, suspect `.vercelignore` or the `excludeFiles` globs
in `vercel.json` - `templates/` and `static/css` + `static/js` must survive both,
because Flask reads them from disk at request time. Vercel's own documentation
suggests excluding `static/**`, which would break this app.

## Other platforms

Working configs for three alternatives are kept in `alternatives/` - move them
back to the repo root to use them. Each sets `MFP_DEBUG=0`, the feed URL, a
1-hour TTL and a 60s refresh cooldown.

- **Render** (`alternatives/render.yaml`) - push to GitHub, then render.com ->
  New -> Blueprint -> select the repo.
- **Railway** (`alternatives/railway.toml` + `alternatives/Procfile`) -
  `railway init && railway up`.
- **Fly.io** (`alternatives/fly.toml`) -
  `fly launch --no-deploy --copy-config && fly deploy`.

All three bind `0.0.0.0:$PORT` with gunicorn, because each platform puts its own
proxy in front. The systemd unit below binds `127.0.0.1:8000` and lets nginx
terminate TLS. That difference is deliberate.

## Adding a domain later

Skipping the domain at first is fine. Attaching one later needs no code change
and no redeploy beyond a new build - add it in Vercel's Domains tab, then point
a CNAME at whatever Vercel shows you (`cname.vercel-dns.com` for a subdomain).

The only thing a domain actually buys you here is a memorable address, since
the app has no cookies, accounts or server-side sessions. The one thing to know
is that the Pomodoro preferences live in `localStorage`, which is per-origin, so
existing listeners lose their saved settings when the address changes. That is a
good reason to pick the final address before anyone else starts using it.

## Own Linux server

```bash
# 1. system packages
sudo apt update
sudo apt install -y python3-venv nginx certbot python3-certbot-nginx

# 2. service account and code
sudo useradd --system --no-create-home --shell /usr/sbin/nologin musicfocus
sudo mkdir -p /srv/musicfocus
sudo chown musicfocus:musicfocus /srv/musicfocus
cd /srv/musicfocus
# copy app.py, catalog.py, episodes.json, static/, templates/, tools/ here

# 3. virtualenv (gunicorn is Linux-only, which is fine here)
sudo -u musicfocus python3 -m venv .venv
sudo -u musicfocus ./.venv/bin/pip install -r requirements.txt

# 4. service
sudo cp deploy/musicfocus.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now musicfocus
sudo systemctl status musicfocus
curl -s localhost:8000/healthz          # {"status":"ok"}

# 5. nginx + TLS
sudo cp deploy/nginx.conf /etc/nginx/sites-available/musicfocus
sudo ln -s /etc/nginx/sites-available/musicfocus /etc/nginx/sites-enabled/
# edit server_name + certificate paths first, then:
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d focus.example.com
```

Verify: `curl -I https://focus.example.com/api/episodes -H 'Accept-Encoding: gzip'`
should return `Content-Encoding: gzip` and a `Content-Length` near 27000.

## Tuning notes

- `--workers 2 --threads 4` suits a small public instance. The catalog cache is
  per worker, so upstream requests scale with workers, **not** with visitors.
  Raise workers for more concurrent users; the feed is not a bottleneck.
- `MFP_CATALOG_TTL=3600` is safe because published episodes never change. Drop
  it if you want a new mix to appear within minutes of publication.
- `MFP_REFRESH_COOLDOWN=60` caps the unauthenticated refresh endpoint to one
  upstream fetch per minute, so the feed publisher is never spammed.

## Refreshing the catalog

Refreshes are a build-time job, not a runtime one:

```bash
cd /srv/musicfocus
sudo -u musicfocus ./.venv/bin/python tools/build_manifest.py \
    --from-rss https://musicforprogramming.net/rss.xml
sudo -u musicfocus ./.venv/bin/python tools/scrape_tracklists.py
sudo systemctl restart musicfocus
```

The scraper downloads 79 episode pages and takes about 30 seconds. Run it only
when MFP publishes something new.

## One thing to be aware of

This hotlinks MFP's audio from their CDN (`datashat.net`) rather than
rehosting it - the same model as any podcast client, and it is what keeps the
hosting free. Every visitor's playback is a request against their bandwidth, so
if this ever gets real traffic it's worth telling Datashette rather than
letting it happen quietly. Nothing in the code prevents it going large.