import gzip
import io
import os
import threading
import time
from urllib.parse import unquote

from flask import Flask, jsonify, render_template, request

import catalog

app = Flask(__name__)

# Single source of truth for the product name. It used to be "musicForFocus()",
# which was a deliberate echo of musicForProgramming() - too close to someone
# else's trademark to keep on a public site. Change it here and it propagates
# to the page title, the command palette banner and /api/config.
APP_NAME = os.environ.get('MFP_APP_NAME', 'Roomtone')
APP_TAGLINE = os.environ.get(
    'MFP_APP_TAGLINE', 'long-form mixes for quiet work')
APP_DEV = os.environ.get("MFP_APP_DEV", "By CodeFusion")

# Both /api/episodes?refresh=1 and POST /api/episodes/refresh force a fresh
# upstream fetch, and neither is authenticated. Without a floor, a loop of them
# would turn this host into a proxy for the feed publisher. The cooldown lives
# here rather than in catalog.load_catalog so that internal callers - the tests,
# the generator - still get an honest "refresh now", and only HTTP callers are
# capped.
REFRESH_COOLDOWN = float(os.environ.get('MFP_REFRESH_COOLDOWN', '60'))
_refresh_lock = threading.Lock()
_last_refresh = [0.0]


def _refresh_allowed():
    """True at most once per REFRESH_COOLDOWN seconds."""
    now = time.time()
    with _refresh_lock:
        if now - _last_refresh[0] < REFRESH_COOLDOWN:
            return False
        _last_refresh[0] = now
        return True


def _too_soon():
    retry = round(REFRESH_COOLDOWN - (time.time() - _last_refresh[0]), 1)
    response = jsonify({
        'error': 'refresh is rate limited; retry in %.1fs' % max(0.0, retry)
    }), 429
    response[0].headers['Retry-After'] = str(int(max(1, retry)))
    return response

#main page route
@app.route('/')
def index():
    return render_template('index.html', app_name=APP_NAME,
                           tagline=APP_TAGLINE,
                           devname=APP_DEV)

#api endpoint for the episode catalog, loaded from the hosted manifest
@app.route('/api/episodes')
def get_episodes():
    refresh = request.args.get('refresh') == '1'
    if refresh and not _refresh_allowed():
        return _too_soon()
    try:
        payload = catalog.load_catalog(refresh=refresh)
    except catalog.CatalogError as exc:
        return jsonify({'error': str(exc)}), 503
    return jsonify(payload)


#force a re-fetch of the remote manifest, bypassing the TTL cache
@app.route('/api/episodes/refresh', methods=['POST'])
def refresh_episodes():
    if not _refresh_allowed():
        return _too_soon()
    try:
        payload = catalog.load_catalog(refresh=True)
    except catalog.CatalogError as exc:
        return jsonify({'error': str(exc)}), 503
    return jsonify(payload)

#where the catalog came from, so the UI can show it
@app.route('/api/config')
def get_config():
    config = catalog.catalog_config()
    config['app_name'] = APP_NAME
    return jsonify(config)


# Cheap liveness probe for the host. Deliberately does NOT touch the catalog:
# it must stay fast and must not fail just because the feed publisher is slow.
@app.route('/healthz')
def healthz():
    return jsonify({'status': 'ok'})

#every episode streams from its own remote audio_url; this server never hosts
#or proxies audio. Flask serves static/ automatically, so /static/audio/ has to
#be refused explicitly - otherwise a stray file in that folder is still
#downloadable, and the catalog can quietly fall back to local playback.
#
#404 rather than 403 on purpose: it does not confirm that the file exists.
#
#The comparison resolves the path first rather than using a plain prefix test:
#the filesystem is case-insensitive, so /static/Audio/x.mp3 reaches the same
#file, and werkzeug collapses /static/./audio/x.mp3 down to a real file on disk
#while the raw path still reads /static/./audio/... - both slip past startswith.
#So percent-decode, drop empty/'.' segments and pop '..', then compare the
#leading segments. Segments above the root are dropped rather than raising,
#which fails closed: /../static/audio/x.mp3 resolves to the blocked path.
def _is_local_audio_request(path):
    segments = []
    for segment in unquote(path.replace('\\', '/')).lower().split('/'):
        if segment in ('', '.'):
            continue
        if segment == '..':
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return segments[:2] == ['static', 'audio']


@app.before_request
def block_local_audio():
    if _is_local_audio_request(request.path):
        return jsonify({
            'error': 'local audio is not served; '
                     'stream each episode from its catalog audio_url'
        }), 404


# The catalog is the only sizeable thing this host sends (about 67 KB of JSON),
# and it gzips to roughly 27 KB. Audio never passes through here, so for a
# public instance this is effectively the whole bandwidth bill.
GZIP_MIN_BYTES = int(os.environ.get('MFP_GZIP_MIN_BYTES', '1024'))


# Static assets are versioned only by cache-busting in the deploy, so a day of
# browser caching is safe and keeps the CSS/JS off every page load. The API is
# never cached here - catalog.py owns its own TTL and reports `cached` in the
# payload.
@app.after_request
def add_response_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')

    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=86400'

    # Compress text responses, but never re-compress something already encoded,
    # never touch the binary audio path (which is blocked anyway), and never
    # touch a file-passthrough response: those report no content length and
    # stream from disk, so rewriting the body would break them.
    if (not response.headers.get('Content-Encoding')
            and response.mimetype in ('application/json', 'text/html',
                                      'text/css', 'application/javascript',
                                      'text/javascript')
            and 'gzip' in request.headers.get('Accept-Encoding', '')):
        length = response.calculate_content_length()
        if length is not None and length >= GZIP_MIN_BYTES:
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=6, mtime=0) as gz:
                gz.write(response.get_data())
            raw = response.get_data()
            data = buf.getvalue()
            # Only switch over if it actually helped.
            if len(data) < len(raw):
                response.set_data(data)
                response.headers['Content-Encoding'] = 'gzip'
                response.headers['Content-Length'] = str(len(data))

    response.headers.add('Vary', 'Accept-Encoding')
    return response

if __name__ == "__main__":
    # Debug is OFF unless asked for. The Werkzeug debugger is a remote shell on
    # the box, so a default-on debug flag would expose this host the moment it
    # was deployed somewhere public. Local dev: MFP_DEBUG=1 python app.py
    debug = os.environ.get('MFP_DEBUG', '0') == '1'
    app.run(debug=debug, port=int(os.environ.get('PORT', 5000)))
