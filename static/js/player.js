// --- State Variables ---
let episodes = [];
let currentEpisode = null;
let currentSpeed = 1.0;
let isLofiActive = false;
let epFilter = '';
let playIntent = false;
let dspAvailable = true;
let corsRetryUsed = false;

// --- DOM Elements ---
const audio = document.getElementById('audio-player');
const playBtn = document.getElementById('btn-play');
const playStatus = document.getElementById('play-status');
const timeDisplay = document.getElementById('time-display');
const epTitle = document.getElementById('episode-title');
const fileSize = document.getElementById('file-size');
const epDuration = document.getElementById('ep-duration');
const dspStatus = document.getElementById('dsp-status');
const tracklistContainer = document.getElementById('tracklist-container');
const episodesList = document.getElementById('episodes-list');
const seekBarContainer = document.getElementById('seek-bar-container');
const scrubInput = document.getElementById('scrub');
const scrubFill = document.querySelector('.scrub-fill');
const scrubHead = document.querySelector('.scrub-head');
const waveformCanvas = document.getElementById('waveform');
// Guarded: a missing canvas must never break playback, and the DSP path
// already has to tolerate environments without Web Audio.
const waveformCtx = (waveformCanvas && typeof waveformCanvas.getContext === 'function')
    ? waveformCanvas.getContext('2d')
    : null;
const epSearch = document.getElementById('ep-search');
const btnRefresh = document.getElementById('btn-refresh');
const catalogStatus = document.getElementById('catalog-status');
const epCount = document.getElementById('ep-count');
const statEpisodes = document.getElementById('stat-episodes');
const statTracks = document.getElementById('stat-tracks');
const statHours = document.getElementById('stat-hours');
const volumeInput = document.getElementById('vol');
const volumeLevel = document.getElementById('volume-level');
const volumeContainer = document.getElementById('volume-container');
const btnLofi = document.getElementById('btn-lofi');
const cmdPalette = document.getElementById('cmd-palette');
const cmdInput = document.getElementById('cmd-input');
const cmdOutput = document.getElementById('cmd-output');

const SCRUB_STEPS = 1000;
const PLAY_GLYPH = '▶';
const PAUSE_GLYPH = '❚❚';
let lastVolume = 1;

// --- 1. LOCAL STORAGE PERSISTENCE ---
const CATALOG_CACHE_KEY = 'mfp_catalog_v1';

function loadSavedPreferences() {
    const savedVol = localStorage.getItem('mfp_volume');
    if (savedVol !== null) setVolume(parseFloat(savedVol));
}

function savePreferences() {
    localStorage.setItem('mfp_volume', audio.volume);
    if (currentEpisode) localStorage.setItem('mfp_last_ep', currentEpisode.id);
}

// --- 2. CATALOG: fetch from /api/episodes, cached in the browser ---
function readCatalogCache() {
    try {
        const raw = localStorage.getItem(CATALOG_CACHE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (!parsed || !Array.isArray(parsed.episodes)) return null;
        return parsed;
    } catch (e) {
        return null;
    }
}

function writeCatalogCache(payload) {
    try {
        localStorage.setItem(CATALOG_CACHE_KEY, JSON.stringify({
            episodes: payload.episodes,
            meta: payload.meta || null,
            updated: payload.updated || null,
            source: payload.source || null,
            stats: payload.stats || null,
            cachedAt: Date.now()
        }));
    } catch (e) {
        // Storage may be full or blocked; the catalog still works, just uncached.
    }
}

function clearCatalogCache() {
    try {
        localStorage.removeItem(CATALOG_CACHE_KEY);
    } catch (e) { /* nothing to do */ }
}

function formatBytes(bytes) {
    const b = Number(bytes);
    if (!b || b <= 0) return '--';
    const units = ['B', 'KB', 'MB', 'GB'];
    let v = b;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i++;
    }
    return `${(i === 0 || v >= 100) ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

function shortTitle(ep) {
    const prefix = `Episode ${ep.id}: `;
    return ep.title && ep.title.startsWith(prefix) ? ep.title.slice(prefix.length) : ep.title;
}

function hostOf(url) {
    if (!url) return 'local';
    if (url === 'local') return 'local episodes.json';
    try {
        return new URL(url, window.location.href).host;
    } catch (e) {
        return url;
    }
}

function setCatalogStatus(text, tone) {
    if (!catalogStatus) return;
    catalogStatus.textContent = text;
    catalogStatus.className = `catalog-status ${tone || 'muted'}`;
}

function describeCatalog(payload, fromCache) {
    const bits = [`src: ${hostOf(payload.source)}`];
    if (payload.updated) bits.push(`upd ${payload.updated}`);
    if (payload.stale) bits.push('stale');
    else if (fromCache) bits.push('cached');
    return `// ${bits.join('  ')}`;
}

function renderStats(stats) {
    if (!stats) return;
    if (statEpisodes) statEpisodes.textContent = `// ${stats.episodes} episodes`;
    if (statTracks) statTracks.textContent = `// ${stats.tracks} tracks`;
    if (statHours) statHours.textContent = `// ${stats.hours} hours of focus`;
}

function episodeMatches(ep, query) {
    if (String(ep.id) === query) return true;
    if ((ep.title || '').toLowerCase().includes(query)) return true;
    return (ep.tracks || []).some(t => t.toLowerCase().includes(query));
}

function renderSidebar() {
    if (!episodesList) return;
    episodesList.innerHTML = '';

    const query = epFilter.trim().toLowerCase();
    const visible = query ? episodes.filter(ep => episodeMatches(ep, query)) : episodes;

    if (epCount) epCount.textContent = episodes.length ? `[${visible.length}/${episodes.length}]` : '[0]';

    if (!visible.length) {
        const li = document.createElement('li');
        li.className = 'muted';
        li.textContent = episodes.length ? '// no match' : '// catalog empty';
        episodesList.appendChild(li);
        return;
    }

    // Built with textContent rather than innerHTML: titles and track names come
    // from a remote manifest, so they are untrusted input.
    const frag = document.createDocumentFragment();
    visible.forEach(ep => {
        const li = document.createElement('li');
        li.id = `ep-item-${ep.id}`;
        li.title = `${ep.title} - ${formatTime(ep.duration)}`;

        const num = document.createElement('span');
        num.className = 'ep-num';
        num.textContent = `${ep.id}:`;

        const label = document.createElement('span');
        label.textContent = shortTitle(ep);

        li.appendChild(num);
        li.appendChild(document.createTextNode(' '));
        li.appendChild(label);

        if (currentEpisode && String(ep.id) === String(currentEpisode.id)) li.classList.add('active');

        li.addEventListener('click', () => selectEpisode(ep.id));
        frag.appendChild(li);
    });
    episodesList.appendChild(frag);
}

function applyCatalog(payload, fromCache) {
    episodes = Array.isArray(payload.episodes) ? payload.episodes : [];
    renderStats(payload.stats);
    renderSidebar();
    setCatalogStatus(describeCatalog(payload, fromCache), payload.stale ? 'purple' : 'muted');

    // Only reload audio when the current episode is actually gone from the new
    // catalog - otherwise a background revalidation would restart playback.
    const stillPresent = currentEpisode && episodes.some(ep => String(ep.id) === String(currentEpisode.id));
    if (stillPresent) return;

    const lastEpId = localStorage.getItem('mfp_last_ep');
    const target = episodes.find(ep => String(ep.id) === String(lastEpId)) || episodes[0];
    if (target) loadEpisode(target);
}

async function loadCatalog(options) {
    const force = !!(options && options.force);

    if (btnRefresh) btnRefresh.classList.add('muted');
    setCatalogStatus(force ? '// refreshing...' : '// loading catalog...');

    // A forced refresh is only allowed once every few seconds, so keep the
    // cached copy until the new one actually arrives. Clearing it up front made
    // a rate-limited click fall through to "offline - serving cached catalog"
    // with nothing cached left.
    if (!force) {
        const cached = readCatalogCache();
        if (cached) applyCatalog(cached, true);
    }

    try {
        const resp = await fetch(force ? '/api/episodes/refresh' : '/api/episodes',
            force ? { method: 'POST' } : undefined);
        const payload = await resp.json();
        if (!resp.ok || payload.error) throw new Error(payload.error || `HTTP ${resp.status}`);

        if (force) clearCatalogCache();
        writeCatalogCache(payload);
        applyCatalog(payload, false);

        if (payload.errors && payload.errors.length) {
            setCatalogStatus(describeCatalog(payload, false), 'purple');
        }
    } catch (err) {
        const cached = readCatalogCache();
        if (cached) {
            applyCatalog(cached, true);
            setCatalogStatus(`// offline - serving cached catalog`, 'purple');
        } else {
            episodes = [];
            renderSidebar();
            setCatalogStatus(`// catalog error: ${err.message}`, 'purple');
        }
    } finally {
        if (btnRefresh) btnRefresh.classList.remove('muted');
    }
}

function renderTracklist(ep) {
    if (!tracklistContainer) return;
    tracklistContainer.innerHTML = '';

    const tracks = ep.tracks || ep.tracklist || [];
    if (!tracks.length) {
        const li = document.createElement('li');
        li.className = 'muted';
        li.textContent = 'no tracklist';
        tracklistContainer.appendChild(li);
        return;
    }

    const frag = document.createDocumentFragment();
    tracks.forEach(track => {
        const li = document.createElement('li');
        li.textContent = track;
        frag.appendChild(li);
    });
    tracklistContainer.appendChild(frag);
}

// Rows the manifest builder has not filled in yet. Kept in step with
// PLACEHOLDER_MARKERS in tools/build_manifest.py.
const UNCONFIGURED_URL_MARKERS = ['REPLACE-ME', '/static/audio/'];

function isUnconfigured(ep) {
    const url = ep && ep.audio_url ? String(ep.audio_url) : '';
    if (!url) return true;
    return UNCONFIGURED_URL_MARKERS.some(marker => url.includes(marker));
}

function loadEpisode(ep) {
    currentEpisode = ep;
    epTitle.innerText = ep.title;
    fileSize.innerText = formatBytes(ep.bytes);
    renderTracklist(ep);

    const manifestDuration = formatTime(ep.duration);
    epDuration.innerText = manifestDuration;

    corsRetryUsed = false;
    dspAvailable = true;
    updateDspAvailability();

    audio.pause();
    playIntent = false;
    // Clear any previous no-CORS fallback first, then request CORS for this
    // source. Reversing these two would leave CORS disabled from the start.
    audio.removeAttribute('crossorigin');
    audio.crossOrigin = 'anonymous';

    // This app hosts no audio: every episode streams from its own remote
    // audio_url. A row still holding a placeholder has nothing to fetch, so say
    // so plainly instead of firing a request at a host that does not exist.
    if (isUnconfigured(ep)) {
        audio.removeAttribute('src');
        audio.load();
        playStatus.innerText = 'Unavailable';
        playStatus.className = 'purple';
        playStatus.title = 'This episode has no audio_url yet. Run '
            + 'tools/build_manifest.py --list-missing, then re-run it with '
            + '--base-url pointing at your host.';
        playBtn.innerText = PLAY_GLYPH;
        updateSeekBar();
    } else {
        playStatus.title = '';
        audio.src = ep.audio_url;
        audio.load();
    }
    audio.playbackRate = currentSpeed;

    document.querySelectorAll('#episodes-list li').forEach(el => el.classList.remove('active'));
    const activeItem = document.getElementById(`ep-item-${ep.id}`);
    if (activeItem) activeItem.classList.add('active');

    savePreferences();
    updateSeekBar();
}

function selectEpisode(id) {
    const ep = episodes.find(item => String(item.id) === String(id));
    if (ep) {
        loadEpisode(ep);
        togglePlay(true);
    }
}

function togglePlay(forcePlay = false) {
    if (!audio.src) return;

    if (audio.paused || forcePlay) {
        playIntent = true;
        setupVisualizerAndDSP();
        audio.play().then(() => {
            playBtn.innerText = PAUSE_GLYPH;
            playStatus.innerText = 'Playing';
            playStatus.className = '';
            if (dspAvailable) startVisualizer();
        }).catch(err => {
            playIntent = false;
            playStatus.innerText = 'Ready';
        });
    } else {
        playIntent = false;
        audio.pause();
        playBtn.innerText = PLAY_GLYPH;
        playStatus.innerText = 'Paused';
        playStatus.className = 'is-paused';
        if (visInterval) clearInterval(visInterval);
        drawIdleWaveform();
    }
}

// --- 2. AUDIO EFFECTS SUITE (Web Audio API Low-Pass Filter & Speed) ---
let audioCtx = null;
let analyser = null;
let biquadFilter = null;
let sourceNode = null;
let visInterval = null;


function ensureAudioCtx() {
    if (!dspAvailable) return null;
    if (!audioCtx) {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (audioCtx.state === 'suspended') audioCtx.resume();
    return audioCtx;
}

function updateDspAvailability() {
    if (dspStatus) {
        dspStatus.textContent = dspAvailable ? 'ready' : 'off (no CORS)';
        dspStatus.className = dspAvailable ? 'green' : 'purple';
    }
    if (btnLofi) btnLofi.classList.toggle('muted', !dspAvailable);
    if (!dspAvailable && visInterval) {
        clearInterval(visInterval);
        visInterval = null;
    }
}

function setupVisualizerAndDSP() {
    if (!dspAvailable) return;
    ensureAudioCtx();

    // Guard on the graph nodes, not audioCtx: other features (e.g. the timer
    // chime) may create the context first, and checking audioCtx alone would
    // skip wiring the media element into it.
    if (analyser && biquadFilter && sourceNode) return;

    try {
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 32;

        biquadFilter = audioCtx.createBiquadFilter();
        biquadFilter.type = "lowpass";
        biquadFilter.frequency.value = 22000; // Unfiltered default

        sourceNode = audioCtx.createMediaElementSource(audio);
        sourceNode.connect(biquadFilter);
        biquadFilter.connect(analyser);
        analyser.connect(audioCtx.destination);
    } catch (e) {
        console.log("MediaElementSource connected.");
    }
}

// A remote host without CORS headers makes crossOrigin=anonymous fail the load
// outright. Retry once without it: playback works, but Web Audio cannot process
// a cross-origin stream, so the visualizer and lo-fi must be switched off
// rather than silently outputting silence.
audio.addEventListener('error', () => {
    if (corsRetryUsed) {
        playStatus.innerText = 'Error';
        playStatus.className = 'purple';
        playBtn.innerText = PLAY_GLYPH;
        playIntent = false;
        return;
    }

    corsRetryUsed = true;
    dspAvailable = false;
    audio.removeAttribute('crossorigin');
    audio.crossOrigin = null;
    updateDspAvailability();

    const shouldPlay = playIntent;
    audio.load();
    if (shouldPlay) audio.play().catch(() => {});
});

function toggleLofiFilter() {
    if (!dspAvailable) return;
    isLofiActive = !isLofiActive;
    setupVisualizerAndDSP();

    if (biquadFilter) {
        biquadFilter.frequency.value = isLofiActive ? 1200 : 22000; // Muffle high frequencies at 1200Hz
    }

    btnLofi.textContent = isLofiActive ? '[lo-fi: ON]' : '[lo-fi: off]';
    btnLofi.style.color = isLofiActive ? 'var(--text-purple)' : 'var(--text-green)';
}

if (btnLofi) btnLofi.addEventListener('click', toggleLofiFilter);

function setSpeed(speed) {
    currentSpeed = speed;
    audio.playbackRate = speed;

    document.querySelectorAll('.effects .pill.speed').forEach(btn => {
        btn.classList.toggle('active-spd', parseFloat(btn.dataset.speed) === speed);
    });
}

// Wired from JS rather than inline onclick so the page stays compatible with a
// strict Content-Security-Policy.
document.querySelectorAll('.effects .pill.speed').forEach(btn => {
    btn.addEventListener('click', () => setSpeed(parseFloat(btn.dataset.speed)));
});

// --- 3. VOLUME & SEEK BAR CONTROLS ---
function formatTime(seconds) {
    if (!seconds || isNaN(seconds) || seconds === Infinity) return "0:00:00";
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    const formattedMins = mins < 10 && hrs > 0 ? `0${mins}` : mins;
    const formattedSecs = secs < 10 ? `0${secs}` : secs;
    return hrs > 0 ? `${hrs}:${formattedMins}:${formattedSecs}` : `${mins}:${formattedSecs}`;
}

function hasDuration() {
    return audio.duration && !isNaN(audio.duration) && audio.duration !== Infinity;
}

function setScrub(ratio) {
    const pct = (Math.min(1, Math.max(0, ratio)) * 100).toFixed(3);
    if (scrubFill) scrubFill.style.width = `${pct}%`;
    if (scrubHead) scrubHead.style.left = `${pct}%`;
    if (scrubInput && document.activeElement !== scrubInput) {
        scrubInput.value = String(Math.round(ratio * SCRUB_STEPS));
    }
}

function updateSeekBar() {
    if (!hasDuration()) {
        setScrub(0);
        timeDisplay.textContent = `${formatTime(audio.currentTime)} / 0:00`;
        return;
    }
    setScrub(audio.currentTime / audio.duration);
    timeDisplay.textContent = `${formatTime(audio.currentTime)} / ${formatTime(audio.duration)}`;
}

audio.addEventListener('loadedmetadata', updateSeekBar);
audio.addEventListener('timeupdate', updateSeekBar);

// Dragging the thumb must not be overwritten by timeupdate, so updateSeekBar
// leaves the input alone while it has focus.
if (scrubInput) {
    scrubInput.addEventListener('input', () => {
        if (!hasDuration()) return;
        audio.currentTime = (scrubInput.value / SCRUB_STEPS) * audio.duration;
        setScrub(scrubInput.value / SCRUB_STEPS);
        timeDisplay.textContent =
            `${formatTime(audio.currentTime)} / ${formatTime(audio.duration)}`;
    });
}

function updateVolumeDisplay() {
    const currentVol = audio.muted ? 0 : audio.volume;
    if (volumeInput && document.activeElement !== volumeInput) {
        volumeInput.value = String(Math.round(currentVol * 100));
    }
    if (volumeLevel) {
        const silent = audio.muted || currentVol === 0;
        volumeLevel.textContent = silent ? 'muted' : `${Math.round(currentVol * 100)}%`;
        volumeLevel.className = silent ? 'muted' : 'readout';
    }
}

function setVolume(val) {
    audio.volume = Math.max(0, Math.min(1, val));
    if (audio.volume > 0) audio.muted = false;
    updateVolumeDisplay();
    savePreferences();
}

function toggleMute() {
    if (audio.muted || audio.volume === 0) {
        audio.muted = false;
        if (audio.volume === 0) audio.volume = lastVolume || 0.8;
    } else {
        lastVolume = audio.volume;
        audio.muted = true;
    }
    updateVolumeDisplay();
    savePreferences();
}

if (volumeInput) {
    volumeInput.addEventListener('input', () => setVolume(volumeInput.value / 100));
    // Clicking the label area of the container should not seek the volume.
    volumeContainer.addEventListener('click', (e) => {
        if (e.target === volumeInput) return;
    });
}

// Transport buttons
playBtn.addEventListener('click', () => togglePlay());
document.getElementById('btn-rw').addEventListener("click", () => { audio.currentTime = Math.max(0, audio.currentTime - 15); updateSeekBar(); });
document.getElementById('btn-ff').addEventListener("click", () => { audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 15); updateSeekBar(); });
document.getElementById('btn-random').addEventListener("click", () => {
    if (episodes.length > 0) selectEpisode(episodes[Math.floor(Math.random() * episodes.length)].id);
});

if (epSearch) {
    let searchDebounce;
    epSearch.addEventListener('input', (e) => {
        epFilter = e.target.value || '';
        clearTimeout(searchDebounce);
        searchDebounce = setTimeout(renderSidebar, 120);
    });
    epSearch.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            epSearch.value = '';
            epFilter = '';
            renderSidebar();
        }
        if (e.key === 'Enter') {
            const q = epFilter.trim().toLowerCase();
            if (!q) return;
            const hit = episodes.find(ep => episodeMatches(ep, q));
            if (hit) {
                selectEpisode(hit.id);
                epSearch.blur();
            }
        }
    });
}

if (btnRefresh) btnRefresh.addEventListener('click', () => loadCatalog({ force: true }));

// --- 4. COMMAND PALETTE OVERLAY ---
function toggleCmdPalette() {
    cmdPalette.classList.toggle('open');
    if (cmdPalette.classList.contains('open')) {
        cmdInput.value = '';
        cmdInput.focus();
    }
}

if (cmdInput) {
    cmdInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            const input = cmdInput.value.trim().toLowerCase();
            cmdInput.value = '';
            parseCommand(input);
        }
    });
}

function parseCommand(cmd) {
    const parts = cmd.split(' ');
    const action = parts[0];
    const arg = parts[1];

    switch (action) {
        case 'help':
            cmdOutput.textContent = "AVAILABLE COMMANDS:\n - play [num]      : Load episode by number\n - lofi            : Toggle lofi low-pass filter\n - random          : Select random episode\n - speed [val]     : Set speed (0.85, 1.0, 1.25)\n - timer [sub]     : start | pause | reset | skip | preset <p> | set <work> <break> [cycles]\n - custom [w b c]  : Set custom pomodoro (minutes, minutes, cycles)\n - refresh         : Re-fetch the catalog manifest\n - filter [q]      : Filter episodes by id, title or track\n - clear           : Clear output";
            break;
        case 'play':
            if (arg) {
                selectEpisode(arg);
                cmdOutput.textContent = `> Playing episode ${arg}`;
            } else togglePlay();
            break;
        case 'lofi':
            toggleLofiFilter();
            cmdOutput.textContent = `> Lo-Fi filter ${isLofiActive ? 'ENABLED' : 'DISABLED'}`;
            break;
        case 'random':
            if (episodes.length > 0) selectEpisode(episodes[Math.floor(Math.random() * episodes.length)].id);
            cmdOutput.textContent = "> Selected random episode";
            break;
        case 'speed':
            if ([0.85, 1.0, 1.25].includes(parseFloat(arg))) {
                setSpeed(parseFloat(arg));
                cmdOutput.textContent = `> Playback speed set to ${arg}x`;
            } else cmdOutput.textContent = "> Valid speeds: 0.85, 1.0, 1.25";
            break;
        case 'refresh':
            loadCatalog({ force: true });
            cmdOutput.textContent = "> re-fetching catalog manifest";
            break;
        case 'filter':
            epFilter = parts.slice(1).join(' ');
            if (epSearch) epSearch.value = epFilter;
            renderSidebar();
            cmdOutput.textContent = epFilter ? `> filtering episodes: ${epFilter}` : '> filter cleared';
            break;
        case 'clear':
            cmdOutput.textContent = "";
            break;
        case 'custom': {
            const w = clampInt(parts[1], 1, 240, null);
            const b = clampInt(parts[2], 1, 120, null);
            if (w === null || b === null) {
                cmdOutput.textContent = "> usage: custom <work_min> <break_min> [cycles]";
            } else {
                const c = clampInt(parts[3], 1, 20, 4);
                ctWork.value = w;
                ctBreak.value = b;
                ctCycles.value = c;
                applyCustomTimer();
                cmdOutput.textContent = `> pomodoro set to ${w}/${b} x${c} cycles`;
            }
            break;
        }
        case 'timer': {
            const sub = parts[1];
            if (sub === 'start') { startTimer(); cmdOutput.textContent = "> timer started"; }
            else if (sub === 'pause' || sub === 'stop') { pauseTimer(); cmdOutput.textContent = "> timer paused"; }
            else if (sub === 'reset') { resetTimer(true); cmdOutput.textContent = "> timer reset to work phase"; }
            else if (sub === 'skip') { onPhaseComplete(); cmdOutput.textContent = `> skipped to ${timer.phase} (cycle ${timer.cycle}/${timer.totalCycles})`; }
            else if (sub === 'preset') {
                cmdOutput.textContent = setTimerPreset(parts[2])
                    ? `> preset ${parts[2]} applied`
                    : "> unknown preset. Use: 25/5, 50/10, 15/3, 90/15";
            }
            else if (sub === 'set') {
                const w = clampInt(parts[2], 1, 240, null);
                const b = clampInt(parts[3], 1, 120, null);
                if (w === null || b === null) {
                    cmdOutput.textContent = "> usage: timer set <work_min> <break_min> [cycles]";
                } else {
                    const c = clampInt(parts[4], 1, 20, 4);
                    ctWork.value = w;
                    ctBreak.value = b;
                    ctCycles.value = c;
                    applyCustomTimer();
                    cmdOutput.textContent = `> pomodoro set to ${w}/${b} x${c} cycles`;
                }
            }
            else {
                cmdOutput.textContent = "> timer: start | pause | reset | skip | preset <25/5|50/10|15/3|90/15> | set <work> <break> [cycles]";
            }
            break;
        }
        default:
            cmdOutput.textContent = `> Command not recognized: '${cmd}'. Type 'help' for options.`;
    }
}

// Global Keyboard Hotkeys
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !cmdPalette.classList.contains('hidden')) {
        toggleCmdPalette();
        return;
    }

    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        toggleCmdPalette();
        return;
    }

    if (e.key === '/' && cmdPalette.classList.contains('hidden') && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
        e.preventDefault();
        toggleCmdPalette();
        return;
    }

    if (['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;

    if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
    else if (e.code === 'ArrowLeft') { audio.currentTime = Math.max(0, audio.currentTime - 15); updateSeekBar(); }
    else if (e.code === 'ArrowRight') { audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 15); updateSeekBar(); }
    else if (e.code === 'ArrowUp') { e.preventDefault(); setVolume(audio.volume + 0.1); }
    else if (e.code === 'ArrowDown') { e.preventDefault(); setVolume(audio.volume - 0.1); }
    else if (e.code === 'KeyM') { toggleMute(); }
    else if (e.code === 'KeyL') { toggleLofiFilter(); }
    else if (e.code === 'KeyT') {
        e.preventDefault();
        if (timer.running) pauseTimer(); else startTimer();
    }
    else if (e.code === 'KeyC') { e.preventDefault(); onPhaseComplete(); }
    else if (e.code === 'KeyR' && e.shiftKey) {
        e.preventDefault();
        resetTimer(true);
    }
    else if (e.code === 'KeyR') {
        if (episodes.length > 0) selectEpisode(episodes[Math.floor(Math.random() * episodes.length)].id);
    }
});

// --- Waveform -------------------------------------------------------------
// Drawn from the live analyser, not from a decoded file: the median episode is
// 120 MB, so decoding one up front to get a seekable waveform is not viable.
// This shows real signal data for what is playing right now.
const WAVE_BARS = 96;
const WAVE_GAP = 2;

function resizeWaveform() {
    if (!waveformCanvas || !waveformCtx) return;
    const dpr = window.devicePixelRatio || 1;
    const rect = waveformCanvas.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width * dpr));
    const h = Math.max(1, Math.round(rect.height * dpr));
    if (waveformCanvas.width !== w || waveformCanvas.height !== h) {
        waveformCanvas.width = w;
        waveformCanvas.height = h;
    }
}

// Kept between frames so idle/low bars decay instead of flickering to nothing.
const wavePeaks = new Float32Array(WAVE_BARS);

function drawIdleWaveform() {
    if (!waveformCtx || !waveformCanvas) return;
    const { width: w, height: h } = waveformCanvas;
    wavePeaks.fill(0);
    waveformCtx.clearRect(0, 0, w, h);
    waveformCtx.strokeStyle = 'rgba(227, 183, 120, .13)';
    waveformCtx.lineWidth = Math.max(1, window.devicePixelRatio || 1);
    waveformCtx.beginPath();
    waveformCtx.moveTo(0, h / 2);
    waveformCtx.lineTo(w, h / 2);
    waveformCtx.stroke();
}

function renderWaveform(dataArray) {
    if (!waveformCtx || !waveformCanvas) return;
    resizeWaveform();
    const { width: w, height: h } = waveformCanvas;
    const mid = h / 2;
    const barW = w / WAVE_BARS;

    waveformCtx.clearRect(0, 0, w, h);

    for (let i = 0; i < WAVE_BARS; i++) {
        // Log-ish bin mapping: linear FFT bins put everything in the first
        // tenth of the display and leave the rest flat.
        const bin = Math.floor(Math.pow(i / WAVE_BARS, 1.7) * dataArray.length);
        const target = (dataArray[bin] || 0) / 255;

        // fast attack, slow release reads as a waveform rather than noise
        wavePeaks[i] = target > wavePeaks[i]
            ? target
            : wavePeaks[i] * 0.82;

        const barH = Math.max(wavePeaks[i] * mid * 0.94, h * 0.012);
        const x = i * barW;
        const y = mid - barH / 2;

        const grad = waveformCtx.createLinearGradient(0, y, 0, y + barH);
        grad.addColorStop(0, 'rgba(227, 183, 120, .55)');
        grad.addColorStop(0.5, 'rgba(227, 183, 120, .95)');
        grad.addColorStop(1, 'rgba(227, 183, 120, .55)');
        waveformCtx.fillStyle = grad;

        const radius = Math.min(barW - WAVE_GAP, 3);
        const wBar = Math.max(1, barW - WAVE_GAP);
        if (waveformCtx.roundRect) {
            waveformCtx.beginPath();
            waveformCtx.roundRect(x, y, wBar, barH, radius);
            waveformCtx.fill();
        } else {
            waveformCtx.fillRect(x, y, wBar, barH);
        }
    }
}

function startVisualizer() {
    if (!dspAvailable || !analyser || !waveformCtx) return;
    if (visInterval) clearInterval(visInterval);
    const dataArray = new Uint8Array(analyser.frequencyBinCount);

    resizeWaveform();
    visInterval = setInterval(() => {
        if (audio.paused) {
            drawIdleWaveform();
            return;
        }
        analyser.getByteFrequencyData(dataArray);
        renderWaveform(dataArray);
    }, 70);
}

if (waveformCanvas) {
    drawIdleWaveform();
    if (typeof window.addEventListener === 'function') {
        window.addEventListener('resize', drawIdleWaveform);
    }
}

// --- 5. POMODORO / FOCUS TIMER ---


const TIMER_PRESETS = {
    '25/5':  { work: 25, brk: 5,  cycles: 4 },
    '50/10': { work: 50, brk: 10, cycles: 3 },
    '15/3':  { work: 15, brk: 3,  cycles: 4 },
    '90/15': { work: 90, brk: 15, cycles: 2 }
};

const timer = {
    workMins: 25,
    brkMins: 5,
    totalCycles: 4,
    presetKey: '25/5',
    cycle: 1,
    phase: 'work',
    remaining: 25 * 60,
    running: false,
    finished: false,
    endTime: null,
    sessionsDone: 0
};

let timerInterval = null;

const timerDisplay = document.getElementById('timer-display');
const timerPhase = document.getElementById('timer-phase');
const timerCycle = document.getElementById('timer-cycle');
const timerBar = document.getElementById('timer-seek-bar');
const btnTimerStart = document.getElementById('btn-timer-start');
const btnTimerPause = document.getElementById('btn-timer-pause');
const btnTimerReset = document.getElementById('btn-timer-reset');
const btnTimerSkip = document.getElementById('btn-timer-skip');
const btnTimerCustom = document.getElementById('btn-timer-custom');
const btnTimerApply = document.getElementById('btn-timer-apply');
const customPanel = document.getElementById('custom-timer-panel');
const ctWork = document.getElementById('ct-work');
const ctBreak = document.getElementById('ct-break');
const ctCycles = document.getElementById('ct-cycles');
const statSessions = document.getElementById('stat-sessions');

function clampInt(value, min, max, fallback) {
    const n = parseInt(value, 10);
    if (isNaN(n)) return fallback;
    return Math.min(max, Math.max(min, n));
}

function phaseLengthSecs(phase) {
    const p = phase || timer.phase;
    return (p === 'work' ? timer.workMins : timer.brkMins) * 60;
}

function renderTimer() {
    const total = phaseLengthSecs();
    const mins = Math.floor(timer.remaining / 60);
    const secs = timer.remaining % 60;
    const text = `${mins}:${secs < 10 ? '0' + secs : secs}`;

    if (timerDisplay) {
        timerDisplay.textContent = timer.finished ? '[done]' : `[${text}]`;
        timerDisplay.classList.toggle('running', timer.running && !timer.finished);
        timerDisplay.classList.toggle('done', timer.finished);
    }
    if (timerPhase) {
        timerPhase.textContent = `[${timer.phase === 'work' ? 'work' : 'break'}]`;
        timerPhase.className = timer.phase === 'work' ? 'green' : 'cyan';
    }
    if (timerCycle) timerCycle.textContent = `${timer.cycle}/${timer.totalCycles}`;

    if (timerBar) {
        const ratio = total > 0 ? Math.min(1, Math.max(0, timer.remaining / total)) : 0;
        timerBar.innerHTML =
            `<span style="display:block;height:100%;width:${(ratio * 100).toFixed(2)}%;` +
            `background:${timer.phase !== 'work' ? 'var(--good)' : 'var(--accent)'}"></span>`;
    }

    if (btnTimerStart) btnTimerStart.classList.toggle('muted', timer.running);
    if (btnTimerPause) btnTimerPause.classList.toggle('muted', !timer.running);
    if (statSessions) statSessions.textContent = `${timer.sessionsDone} focus cycle${timer.sessionsDone === 1 ? '' : 's'}`;

    const appName = document.querySelector('.brand-name');
    const baseTitle = appName ? appName.textContent.trim() : 'Roomtone';
    document.title = (timer.running && !timer.finished)
        ? `${text} ${timer.phase} — ${baseTitle}`
        : baseTitle;
}

// Derived from an absolute end timestamp rather than a decrementing counter,
// so the countdown stays accurate when the tab is backgrounded and the
// interval is throttled to once per minute.
function timerTick() {
    if (!timer.running) return;

    const left = Math.round((timer.endTime - Date.now()) / 1000);
    if (left <= 0) {
        timer.remaining = 0;
        onPhaseComplete();
        return;
    }
    if (left !== timer.remaining) {
        timer.remaining = left;
        renderTimer();
        saveTimer();
    }
}

function startTimer() {
    if (timer.running) return;
    if (timer.finished) resetTimer(true);

    if ('Notification' in window && Notification.permission === 'default') {
        Notification.requestPermission().catch(() => {});
    }

    timer.running = true;
    timer.endTime = Date.now() + timer.remaining * 1000;

    if (timerInterval) clearInterval(timerInterval);
    timerInterval = setInterval(timerTick, 250);

    renderTimer();
    saveTimer();
}

function stopTimerInterval() {
    if (timerInterval) {
        clearInterval(timerInterval);
        timerInterval = null;
    }
}

function pauseTimer() {
    if (!timer.running) return;
    timer.running = false;
    timer.endTime = null;
    stopTimerInterval();
    renderTimer();
    saveTimer();
}

function resetTimer(full) {
    timer.running = false;
    timer.finished = false;
    timer.endTime = null;
    stopTimerInterval();
    if (full) {
        timer.cycle = 1;
        timer.phase = 'work';
    }
    timer.remaining = phaseLengthSecs();
    renderTimer();
    saveTimer();
}

// Advances work -> break -> work ... Returns false when the whole session is
// finished, true when there is another phase to run.
function advancePhase() {
    if (timer.phase === 'work') {
        timer.sessionsDone += 1;
        if (timer.cycle >= timer.totalCycles) {
            timer.finished = true;
            timer.running = false;
            timer.phase = 'work';
            timer.cycle = 1;
            timer.remaining = 0;
            return false;
        }
        timer.cycle += 1;
        timer.phase = 'break';
    } else {
        timer.phase = 'work';
    }
    timer.remaining = phaseLengthSecs();
    return true;
}

function onPhaseComplete() {
    timer.running = false;
    timer.endTime = null;
    stopTimerInterval();

    const hasNext = advancePhase();
    const endedWork = timer.phase !== 'work';

    renderTimer();
    saveTimer();

    if (hasNext) {
        timerChime(endedWork ? 660 : 990);
        notify(
            endedWork ? 'focus session complete' : 'break over',
            endedWork ? 'Break time - audio paused.' : 'Back to focus.'
        );
        if (endedWork) fadeOutAndPause();
    } else {
        timerChime(1320);
        notify('all cycles complete', `${timer.sessionsDone} focus cycles done.`);
        fadeOutAndPause();
    }
}

function timerChime(freq, duration) {
    try {
        const ctx = ensureAudioCtx();
        if (!ctx) return;
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'square';
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.08, ctx.currentTime + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + duration);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start();
        osc.stop(ctx.currentTime + duration + 0.02);
    } catch (e) {
        console.log('Chime unavailable:', e);
    }
}

function notify(title, body) {
    try {
        if ('Notification' in window && Notification.permission === 'granted') {
            new Notification(title, { body, tag: 'mfp-timer' });
        }
    } catch (e) {
        // Notification constructor is unavailable on some mobile browsers
    }
}

function fadeOutAndPause(seconds = 3) {
    if (!audio.src || audio.paused) return;

    const startVol = audio.muted ? (lastVolume || 0.8) : audio.volume;
    const start = performance.now();

    const step = () => {
        const pct = Math.min(1, (performance.now() - start) / (seconds * 1000));
        audio.volume = startVol * (1 - pct);
        updateVolumeDisplay();
        if (pct < 1 && !audio.paused) {
            requestAnimationFrame(step);
        } else {
            if (!audio.paused) togglePlay();
            audio.volume = startVol;
            updateVolumeDisplay();
            savePreferences();
        }
    };
    requestAnimationFrame(step);
}

function markActivePreset(key) {
    document.querySelectorAll('.presets .pill').forEach(btn => {
        if (btn.dataset.preset) btn.classList.toggle('sel', btn.dataset.preset === key);
    });
    if (btnTimerCustom) btnTimerCustom.classList.toggle('sel', key === 'custom');
}

function setTimerPreset(key) {
    const p = TIMER_PRESETS[key];
    if (!p) return false;

    timer.workMins = p.work;
    timer.brkMins = p.brk;
    timer.totalCycles = p.cycles;
    timer.presetKey = key;
    if (ctWork) ctWork.value = p.work;
    if (ctBreak) ctBreak.value = p.brk;
    if (ctCycles) ctCycles.value = p.cycles;

    resetTimer(true);
    markActivePreset(key);
    saveTimer();
    return true;
}

function applyCustomTimer() {
    const w = clampInt(ctWork.value, 1, 240, 25);
    const b = clampInt(ctBreak.value, 1, 120, 5);
    const c = clampInt(ctCycles.value, 1, 20, 4);

    if (ctWork) ctWork.value = w;
    if (ctBreak) ctBreak.value = b;
    if (ctCycles) ctCycles.value = c;

    timer.workMins = w;
    timer.brkMins = b;
    timer.totalCycles = c;
    timer.presetKey = 'custom';

    resetTimer(true);
    markActivePreset('custom');
    saveTimer();
}

function toggleCustomTimer() {
    if (!customPanel) return;
    customPanel.classList.toggle('hidden');
    if (!customPanel.classList.contains('hidden') && ctWork) ctWork.focus();
}

function saveTimer() {
    localStorage.setItem('mfp_timer_cfg', JSON.stringify({
        workMins: timer.workMins,
        brkMins: timer.brkMins,
        totalCycles: timer.totalCycles,
        presetKey: timer.presetKey,
        sessionsDone: timer.sessionsDone
    }));
    localStorage.setItem('mfp_timer_run', JSON.stringify({
        cycle: timer.cycle,
        phase: timer.phase,
        remaining: timer.remaining,
        running: timer.running,
        finished: timer.finished,
        endTime: timer.endTime
    }));
}

function loadTimer() {
    try {
        const cfg = JSON.parse(localStorage.getItem('mfp_timer_cfg'));
        if (cfg) {
            timer.workMins = clampInt(cfg.workMins, 1, 240, 25);
            timer.brkMins = clampInt(cfg.brkMins, 1, 120, 5);
            timer.totalCycles = clampInt(cfg.totalCycles, 1, 20, 4);
            timer.presetKey = cfg.presetKey || '25/5';
            timer.sessionsDone = clampInt(cfg.sessionsDone, 0, 9999, 0);
        }
    } catch (e) { /* corrupt config, fall back to defaults */ }

    if (ctWork) ctWork.value = timer.workMins;
    if (ctBreak) ctBreak.value = timer.brkMins;
    if (ctCycles) ctCycles.value = timer.totalCycles;
    markActivePreset(timer.presetKey);

    try {
        const run = JSON.parse(localStorage.getItem('mfp_timer_run'));
        if (run) {
            timer.cycle = clampInt(run.cycle, 1, 20, 1);
            timer.phase = run.phase === 'break' ? 'break' : 'work';
            timer.finished = !!run.finished;

            if (run.running && run.endTime) {
                const left = Math.round((run.endTime - Date.now()) / 1000);
                if (left > 0) {
                    timer.remaining = left;
                    timer.endTime = run.endTime;
                    timer.running = true;
                } else {
                    // Expired while the tab was closed - roll forward one phase.
                    timer.remaining = 0;
                    timer.running = false;
                    if (!timer.finished) advancePhase();
                }
            } else {
                timer.remaining = clampInt(run.remaining, 0, 240 * 60, phaseLengthSecs());
                timer.running = false;
            }
        } else {
            timer.remaining = phaseLengthSecs();
        }
    } catch (e) {
        timer.remaining = phaseLengthSecs();
    }
}

if (btnTimerStart) btnTimerStart.addEventListener('click', startTimer);
if (btnTimerPause) btnTimerPause.addEventListener('click', pauseTimer);
if (btnTimerReset) btnTimerReset.addEventListener('click', () => resetTimer(true));
if (btnTimerSkip) btnTimerSkip.addEventListener('click', onPhaseComplete);
if (btnTimerCustom) btnTimerCustom.addEventListener('click', toggleCustomTimer);
if (btnTimerApply) btnTimerApply.addEventListener('click', applyCustomTimer);

document.querySelectorAll('.presets .pill[data-preset]').forEach(btn => {
    btn.addEventListener('click', () => setTimerPreset(btn.dataset.preset));
});

const btnCmd = document.getElementById('btn-cmd');
if (btnCmd) btnCmd.addEventListener('click', toggleCmdPalette);

[ctWork, ctBreak, ctCycles].forEach(el => {
    if (el) el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            applyCustomTimer();
        }
    });
});

loadSavedPreferences();
loadTimer();
renderTimer();
if (timer.running) startTimer();
updateDspAvailability();
loadCatalog();
