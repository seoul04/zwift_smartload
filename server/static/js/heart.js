/**
 * The four heart graphs: ECG, Beats, Trends and RMSSD.
 *
 * A browser port of the `my_heart` Android panels, drawn on plain canvas so they
 * read the way they do on the phone rather than as generic line charts. All the
 * arithmetic — the R-R statistics, the beat reference finder, the RMSSD tile
 * grid — lives on the server (`src/heart`), which ports the app's Kotlin
 * detector. This file is only layout and paint.
 *
 * The panels are tabs on the dashboard: only the active one polls and draws, so
 * an unseen graph costs nothing. The server keeps accumulating regardless, so
 * switching back shows the whole window immediately.
 */

(function () {
    'use strict';

    const API_BASE = '';
    const UPDATE_INTERVAL = 250;   // ms between polls for the active graph

    // Colours, matching the dark dashboard and the app's roles.
    const C = {
        bg: '#2a2a2a',
        muted: '#888888',
        text: '#e0e0e0',
        ecg: '#4CAF50',
        grid: 'rgba(224, 224, 224, 0.12)',
        // The stack ramps oldest (faint) to newest (solid); see beatStroke().
        beat: '#4CAF50',
        beatOldAlpha: 0.12,
        hr: '#4CAF50',
        r30: '#FF9800',
        r300: '#64B5F6',
        tileEmpty: 'rgba(224, 224, 224, 0.08)',
    };

    const FONT_SMALL = '11px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';

    // ------------------------------------------------------------------
    // Shared state
    // ------------------------------------------------------------------

    /** Which pane is showing; only it polls. */
    let activePane = 'live';

    /** Per-pane timers. */
    const timers = {};
    const inFlight = {};

    /** Latest frame per pane, so a resize can repaint without new data. */
    const frames = {
        ecg: null,
        beats: null,
        trends: null,
        rmssd: null,
    };

    /** Heart config echoed by the server, so the steppers show what it holds. */
    let heartStatus = null;

    // ------------------------------------------------------------------
    // Small helpers
    // ------------------------------------------------------------------

    function el(id) {
        return document.getElementById(id);
    }

    async function getJSON(path) {
        const response = await fetch(`${API_BASE}${path}`);
        if (!response.ok) throw new Error(`${path}: ${response.status}`);
        return response.json();
    }

    /**
     * Stepper values the user has chosen, kept on the server but only for the
     * life of that process: `POST /api/heart/config` never writes config.conf,
     * so a restart drops everything back to the file's defaults.
     *
     * The browser remembers them instead, and re-applies them on load. That
     * survives a server restart, a redeploy, or the browser being closed —
     * none of which the server's own state can span. Storage is best-effort:
     * a private window or a browser with it disabled throws, and the graphs
     * simply open on the server's defaults as before.
     */
    const STORE_KEY = 'zrelay.heart.settings';

    /** The retune parameters, and the numeric coercion each expects. */
    const CONFIG_FIELDS = {
        ecg_window: Number,
        trend_width: Number,
        beats_limit: Number,
        rmssd_width: Number,
        rmssd_side: Number,
    };

    function readStoredSettings() {
        try {
            const raw = localStorage.getItem(STORE_KEY);
            if (!raw) return {};
            const parsed = JSON.parse(raw);
            const out = {};
            Object.keys(CONFIG_FIELDS).forEach((key) => {
                const v = Number(parsed[key]);
                // Guard the restore path: a corrupted or hand-edited entry must
                // not send NaN to the server.
                if (parsed[key] !== undefined && isFinite(v)) out[key] = v;
            });
            return out;
        } catch (e) {
            return {};
        }
    }

    function storeSettings(params) {
        try {
            const merged = Object.assign(readStoredSettings(), params);
            localStorage.setItem(STORE_KEY, JSON.stringify(merged));
        } catch (e) {
            // No storage (private window, disabled, quota) — not worth failing
            // the graph over.
        }
    }

    async function postConfig(params) {
        const query = new URLSearchParams(params).toString();
        try {
            heartStatus = await postJSON(`/api/heart/config?${query}`);
            // Only record what the server accepted.
            storeSettings(params);
            // The affected graph refetches immediately rather than waiting a tick.
            refreshActivePane();
        } catch (e) {
            console.error('Heart config failed:', e);
        }
    }

    /**
     * Re-apply remembered settings once, before the first pane is drawn.
     *
     * They go out as one request so the first poll does not race a retune, and
     * the response is taken as the starting status. The server clamps every
     * field, so a value left over from an older build cannot inject something
     * out of range.
     */
    async function restoreSettings() {
        const stored = readStoredSettings();
        const keys = Object.keys(stored);
        if (keys.length === 0) return;
        const query = new URLSearchParams(stored).toString();
        try {
            heartStatus = await postJSON(`/api/heart/config?${query}`);
        } catch (e) {
            // The server may have restarted with a stricter schema; the graphs
            // stay on its defaults rather than retrying in a loop.
            console.error('Heart config restore failed:', e);
        }
    }

    /**
     * POST with no body — the settings ride in the query string.
     *
     * The retune routes are POST rather than GET because they change server
     * state; a GET would be cacheable and could replay from a prefetch.
     */
    async function postJSON(path) {
        const response = await fetch(`${API_BASE}${path}`, { method: 'POST' });
        if (!response.ok) throw new Error(`${path}: ${response.status}`);
        return response.json();
    }

    /** Fit the canvas to its wrapper at device pixel ratio, so text is sharp. */
    function fitCanvas(canvas) {
        const wrap = canvas.parentElement;
        const dpr = window.devicePixelRatio || 1;
        const w = wrap.clientWidth;
        const h = wrap.clientHeight;
        if (w <= 0 || h <= 0) return null;
        if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
            canvas.width = Math.round(w * dpr);
            canvas.height = Math.round(h * dpr);
        }
        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);
        ctx.fillStyle = C.bg;
        ctx.fillRect(0, 0, w, h);
        return { ctx, w, h };
    }

    function setEmpty(id, text) {
        const node = el(id);
        if (!node) return;
        if (text) {
            node.textContent = text;
            node.style.display = 'flex';
        } else {
            node.style.display = 'none';
        }
    }

    /** Draw text with its box, returning the measured size (canvas has no layout). */
    function label(ctx, text, x, y, color, align, baseline) {
        ctx.font = FONT_SMALL;
        ctx.fillStyle = color || C.muted;
        ctx.textAlign = align || 'left';
        ctx.textBaseline = baseline || 'alphabetic';
        ctx.fillText(text, x, y);
    }

    function measure(ctx, text) {
        ctx.font = FONT_SMALL;
        return ctx.measureText(text).width;
    }

    function fmtTick(v) {
        const r = Math.round(v);
        if (Math.abs(v - r) < 0.05) return String(r);
        return (Math.round(v * 10) / 10).toFixed(1);
    }

    /** A tick's distance from now: seconds under a minute, otherwise whole minutes. */
    function fmtAge(ageMs) {
        const sec = Math.round(ageMs / 1000);
        if (sec < 60) return `${sec}s`;
        return `${Math.round(sec / 60)}m`;
    }

    /** Round tick step in ms: about four divisions across the window. */
    function niceTimeStepMs(rawMs) {
        const sec = rawMs / 1000;
        let step;
        if (sec <= 5) step = 5;
        else if (sec <= 15) step = 15;
        else if (sec <= 30) step = 30;
        else if (sec <= 60) step = 60;
        else if (sec <= 120) step = 120;
        else if (sec <= 300) step = 300;
        else if (sec <= 600) step = 600;
        else step = 900;
        return step * 1000;
    }

    /** Parse `#rrggbb` into [r, g, b]; null when the string is something else. */
    function hexToRgb(hex) {
        const m = /^#?([0-9a-f]{6})$/i.exec(hex);
        if (!m) return null;
        const n = parseInt(m[1], 16);
        return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
    }

    /** `rgba()` for one colour at an alpha. */
    function withAlpha(hex, alpha) {
        const rgb = hexToRgb(hex);
        if (!rgb) return hex;
        return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
    }

    /**
     * Blend two `#rrggbb` colours, t = 0 at a and 1 at b.
     *
     * The tile legend interpolates between stops, so this is what turns three
     * anchor colours into a continuous scale. A stop that is not a plain hex (a
     * CSS name, say) is passed through instead of guessed at.
     */
    function mix(a, b, t) {
        const ca = hexToRgb(a);
        const cb = hexToRgb(b);
        if (!ca || !cb) return t < 0.5 ? a : b;
        const k = Math.min(Math.max(t, 0), 1);
        const ch = (i) => Math.round(ca[i] + (cb[i] - ca[i]) * k);
        return `rgb(${ch(0)}, ${ch(1)}, ${ch(2)})`;
    }

    /**
     * Stroke colour for a wave of the given age, 0 (oldest) to 1 (newest).
     *
     * The fade is applied as alpha rather than an interpolation toward the
     * background: the stack keeps one hue so the traces read as one group, and a
     * bright newest complex still stands out of it.
     */
    function beatStroke(age) {
        const a = C.beatOldAlpha + (1 - C.beatOldAlpha) * Math.min(Math.max(age, 0), 1);
        return withAlpha(C.beat, a);
    }

    /** Colour for one RMSSD value, from the panel's stops (linear between them). */
    function rmssdColor(v, stops) {
        if (!stops || stops.length === 0) return '#555';
        if (v <= stops[0][0]) return stops[0][1];
        const last = stops[stops.length - 1];
        if (v >= last[0]) return last[1];
        for (let i = 1; i < stops.length; i++) {
            if (v <= stops[i][0]) {
                const a = stops[i - 1];
                const b = stops[i];
                const t = (v - a[0]) / (b[0] - a[0]);
                return mix(a[1], b[1], t);
            }
        }
        return last[1];
    }

    // ------------------------------------------------------------------
    // Readouts shared by the four panes
    // ------------------------------------------------------------------

    /**
     * The metric line above each graph: heart rate, mean RR and both RMSSDs.
     *
     * The app keeps this row in landscape so nothing needed mid-recording is
     * stranded behind a rotation; here it sits above every panel, which is the
     * same idea with all the screen available.
     */
    function renderReadout(paneId) {
        const node = document.querySelector(`#pane-${paneId} [data-readout]`);
        if (!node || !heartStatus) return;
        const s = heartStatus;
        const beat = (v) => (v > 0 ? String(v) : '—');
        node.innerHTML =
            row('HR', beat(s.hr_bpm), 'bpm') +
            row('Mean RR', beat(s.mean_rr_ms), 'ms') +
            row('RMSSD30', beat(s.rmssd30_ms), 'ms') +
            row('RMSSD300', beat(s.rmssd300_ms), 'ms') +
            row('Beats', beat(s.beats), '');
    }

    function row(labelText, value, unit) {
        return `<span><span class="ro-label">${labelText}</span>` +
            `<span class="ro-value">${value}</span>` +
            `<span class="ro-unit">${unit}</span></span>`;
    }

    // ------------------------------------------------------------------
    // ECG: the scrolling strip
    // ------------------------------------------------------------------

    function drawEcg(frame) {
        const canvas = el('ecg-canvas');
        if (!canvas) return;
        const fitted = fitCanvas(canvas);
        if (!fitted) return;
        const { ctx, w, h } = fitted;

        if (!frame || frame.empty || !frame.samples || frame.samples.length < 2) {
            setEmpty('ecg-empty', 'ECG starting…');
            return;
        }
        setEmpty('ecg-empty', null);

        const gutterL = 6;
        const gutterB = 18;
        const left = gutterL;
        const right = w;
        const top = 4;
        const bottom = h - gutterB;
        const plotW = right - left;
        const plotH = bottom - top;
        if (plotW <= 1 || plotH <= 1) return;

        // Baseline grid: quarters of the panel height, so the trace can be read
        // against a fixed frame even though the gain floats.
        ctx.strokeStyle = C.grid;
        ctx.lineWidth = 1;
        for (let i = 1; i < 4; i++) {
            const y = top + (plotH * i) / 4;
            ctx.beginPath();
            ctx.moveTo(left, y);
            ctx.lineTo(right, y);
            ctx.stroke();
        }

        // Every sample is drawn at its true fractional position, offset by
        // `left_fraction` — the sub-sample distance the window has slid. That is
        // what makes the scroll continuous instead of stepping one sample at a
        // time, and it still walks every sample rather than decimating to one per
        // pixel column, because a decimated R/S apex changes height as the pixel
        // grid crosses it.
        const samples = frame.samples;
        const n = samples.length;
        const midY = top + plotH / 2;
        const gain = plotH / 2 / Math.max(frame.peak_uv, 1);
        const span = Math.max(n - 1, 1);
        const leftFraction = frame.left_fraction || 0;
        const firstReal = frame.first_real || 0;

        ctx.beginPath();
        let started = false;
        for (let i = firstReal; i < n; i++) {
            const x = left + ((i + leftFraction) / span) * plotW;
            const y = midY - samples[i] * gain;
            if (!started) {
                ctx.moveTo(x, y);
                started = true;
            } else {
                ctx.lineTo(x, y);
            }
        }
        ctx.strokeStyle = C.ecg;
        ctx.lineWidth = 2;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        ctx.stroke();

        // The window and the gain, so the trace is legible.
        const seconds = Math.round(frame.window_ms / 1000);
        label(ctx, `${seconds}s`, right, bottom + 14, C.muted, 'right');
        label(ctx, `±${Math.round(frame.peak_uv)} µV`, left, bottom + 14, C.muted, 'left');

        if (frozenEcg) {
            const text = 'PAUSED';
            ctx.font = FONT_SMALL;
            const tw = ctx.measureText(text).width;
            const padX = 6;
            const padY = 3;
            const bx = right - tw - padX * 2;
            const by = top;
            ctx.fillStyle = 'rgba(76, 175, 80, 0.85)';
            ctx.fillRect(bx, by, tw + padX * 2, 14 + padY * 2);
            ctx.fillStyle = '#10240f';
            ctx.textAlign = 'left';
            ctx.textBaseline = 'top';
            ctx.fillText(text, bx + padX, by + padY);
        }
    }

    /**
     * Freeze holds the frame on screen without stopping the clock behind it, so
     * resuming shows the live signal instead of sliding to catch up — the same
     * rule as the app's frozen strip.
     */
    let frozenEcg = false;

    /**
     * Fetch the strip at the display rate.
     *
     * The playback clock lives on the server, so a frame has to be fetched per
     * tick — but the trace moves between packets only if those frames keep
     * coming, which is why this runs on requestAnimationFrame instead of the
     * 250 ms shared interval. Each frame advances the playhead by the elapsed
     * wall time and slides the window by a fraction of a sample, so the trace
     * scrolls continuously however bursty the sensor is.
     *
     * While frozen no frame is taken (the one on screen stays put) but the call
     * still goes out so the server keeps its clock with the data.
     */
    let ecgRunning = false;

    function startEcgLoop() {
        if (ecgRunning) return;
        ecgRunning = true;
        const tick = async () => {
            if (!ecgRunning || activePane !== 'ecg') {
                ecgRunning = false;
                return;
            }
            // A slow trap must not queue frames: skip this tick.
            if (!inFlight.ecg) {
                inFlight.ecg = true;
                try {
                    await fetchEcgFrame();
                } finally {
                    inFlight.ecg = false;
                }
            }
            if (ecgRunning && activePane === 'ecg') requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
    }

    function stopEcgLoop() {
        ecgRunning = false;
    }

    async function fetchEcgFrame() {
        try {
            const path = frozenEcg ? '/api/heart/ecg?frozen=1' : '/api/heart/ecg';
            const response = await fetch(`${API_BASE}${path}`);
            if (response.status === 204) return;    // frozen: keep the frame shown
            if (!response.ok) throw new Error(`${path}: ${response.status}`);
            const frame = await response.json();
            frames.ecg = frame;
            if (frame.presets) ecgPresets = frame.presets;
            syncEcgStepper(frame.window_rr);
            drawEcg(frame);
        } catch (e) {
            console.error('ECG poll failed:', e);
        }
    }

    let ecgPresets = [3, 5, 8, 10, 15, 20];

    function syncEcgStepper(current) {
        const value = el('ecg-window-value');
        if (!value || current === undefined || current === null) return;
        value.textContent = `${Math.round(current)} RR`;
        const idx = ecgPresets.findIndex((p) => p >= current);
        const at = idx >= 0 ? idx : ecgPresets.length - 1;
        setStepperEnds('ecg-window-stepper', at, ecgPresets.length);
    }

    /** Enable/disable a stepper's two ends; a no-op when the markup is absent. */
    function setStepperEnds(stepperId, index, count) {
        const stepper = el(stepperId);
        if (!stepper) return;
        const back = stepper.querySelector('[data-step="-1"]');
        const forward = stepper.querySelector('[data-step="1"]');
        if (back) back.disabled = index <= 0;
        if (forward) forward.disabled = index >= count - 1;
    }

    // ------------------------------------------------------------------
    // Beats: the superposed stack
    // ------------------------------------------------------------------

    function drawBeats(frame) {
        const canvas = el('beats-canvas');
        if (!canvas) return;
        const fitted = fitCanvas(canvas);
        if (!fitted) return;
        const { ctx, w, h } = fitted;

        if (!frame || !frame.waves || frame.waves.length === 0) {
            setEmpty('beats-empty', "Waiting for beats…\n(the sensor's R-R clock draws each wave)");
            return;
        }
        setEmpty('beats-empty', null);

        const pad = h * 0.06;
        const usable = h - 2 * pad;
        const midY = pad + usable / 2;
        const scale = (usable / 2) / Math.max(frame.reach, 1e-6);

        // Every wave holds the same slice of ECG — the same sampling window, at
        // the same scale — so they are all drawn at one width. That is what makes
        // them superpose into a single readable stack rather than fanning out: a
        // wave drawn wider than its neighbours would put its QRS descent at a
        // different x for the same sample, and a dozen such traces read as a
        // bowtie.
        //
        // The width is the *window's*, not the interval's. Sized by interval, a
        // slower beat stretched the same 256 samples over more pixels, so at any
        // given x each trace showed a different amount of ECG.
        const R_AT = frame.r_at !== undefined ? frame.r_at : 85;
        const gridLen = frame.grid_len || 256;
        const R_FRAC = frame.r_frac !== undefined ? frame.r_frac : 1 / 3;
        // The whole grid spreads over the plot width, so the step is the width
        // over the grid. Dividing by R's grid index instead — as if R were always
        // the middle — made the grid too coarse whenever R sat early in the
        // window, which ran the T wave off the right edge.
        const pxPerSample = w / (gridLen - 1);

        // R is not at the middle of the plot: the window gives a third of its
        // room before R and two thirds after, so R is drawn at that fraction of
        // the width. Centring it instead ran the T wave off the right edge — the
        // exact truncation this whole placement exists to avoid.
        const rX = w * R_FRAC;
        const waves = frame.waves;

        for (let i = 0; i < waves.length; i++) {
            const wave = waves[i];
            const samples = wave.samples;
            if (!samples || samples.length <= R_AT) continue;
            const base = wave.baseline || 0;

            // Each wave is drawn on its *own* detected stroke, not on the grid
            // index R was nominally sliced at. Slicing puts R at `r_at` only to
            // within the jitter of the beat clock, which is derived from the
            // sensor's packet-quantised R–R stream; drawing every wave on the
            // nominal centre would leave each R wherever that jitter put it, so
            // the stack splits into a couple of columns instead of superposing.
            // Pinning each wave's `peak` to the shared line puts every R there by
            // construction. A flat wave reports `r_at`, so nothing is invented for
            // one with no stroke to speak of.
            const centre = wave.peak !== undefined ? wave.peak : R_AT;

            // Oldest fades toward the background, newest is drawn solid: the
            // ramp is what makes a fresh complex stand out from the stack.
            const color = beatStroke(wave.age);
            ctx.beginPath();
            // Samples off the plot are clipped by starting the path at the first
            // one actually on screen.
            let started = false;
            for (let g = 0; g < samples.length; g++) {
                const x = rX + (g - centre) * pxPerSample;
                if (x < 0 || x > w) continue;
                const y = midY - (samples[g] - base) * scale;
                if (!started) {
                    ctx.moveTo(x, y);
                    started = true;
                } else {
                    ctx.lineTo(x, y);
                }
            }
            if (!started) continue;
            ctx.strokeStyle = color;
            ctx.lineWidth = i === waves.length - 1 ? 2.5 : 1.4;
            ctx.lineJoin = 'round';
            ctx.lineCap = 'round';
            ctx.stroke();
        }

        const countNode = el('beats-count');
        if (countNode) {
            countNode.textContent = `showing ${frame.shown} of ${frame.available}`;
        }
        const limitNode = el('beats-limit-value');
        if (limitNode) limitNode.textContent = String(frame.limit);

        // Steppers move along the app's ladder (1, 2, 4, 8 … 64) rather than one
        // beat at a time: the useful question is the order of magnitude.
        const steps = frame.steps || [1, 2, 4, 8, 16, 32, 64];
        const at = Math.max(steps.indexOf(frame.limit), 0);
        setStepperEnds('beats-stepper', at, steps.length);
    }

    async function pollBeats() {
        try {
            const frame = await getJSON('/api/heart/beats');
            frames.beats = frame;
            drawBeats(frame);
        } catch (e) {
            console.error('Beats poll failed:', e);
        }
    }

    // ------------------------------------------------------------------
    // Trends: HR and both RMSSDs
    // ------------------------------------------------------------------

    function drawTrends(frame) {
        const canvas = el('trends-canvas');
        if (!canvas) return;
        const fitted = fitCanvas(canvas);
        if (!fitted) return;
        const { ctx, w, h } = fitted;

        const gutterB = 18;
        // Both gutters are reserved even when only one axis has labels, so the
        // plot rect does not shift when a series starts or stops reporting.
        const gutterL = 34;
        const gutterR = 34;
        const left = gutterL;
        const right = w - gutterR;
        const top = 4;
        const bottom = h - gutterB;
        const plotW = right - left;
        const plotH = bottom - top;
        if (plotW <= 1 || plotH <= 1) return;

        if (!frame || frame.window_ms <= 0) {
            setEmpty('trends-empty', 'Waiting for beats…');
            return;
        }

        const hrAxis = frame.hr_axis;
        const msAxis = frame.rmssd_axis;

        // Horizontal grid on the RMSSD axis; the HR axis is labelled but not
        // gridded, so the panel does not become a mesh of two ladders.
        ctx.strokeStyle = C.grid;
        ctx.lineWidth = 1;
        for (const t of axisTicks(msAxis)) {
            const y = bottom - ((t - msAxis.min) / axisSpan(msAxis)) * plotH;
            ctx.beginPath();
            ctx.moveTo(left, y);
            ctx.lineTo(right, y);
            ctx.stroke();
        }

        // Right-hand ms labels.
        for (const t of axisTicks(msAxis)) {
            const y = bottom - ((t - msAxis.min) / axisSpan(msAxis)) * plotH;
            label(ctx, fmtTick(t), right + 4, y + 4, C.muted, 'left');
        }
        // Left-hand bpm labels.
        for (const t of axisTicks(hrAxis)) {
            const y = bottom - ((t - hrAxis.min) / axisSpan(hrAxis)) * plotH;
            label(ctx, fmtTick(t), left - 4, y + 4, C.muted, 'right');
        }

        // Time axis, in seconds-ago: "-Nm" at round positions plus "now".
        const windowMs = frame.window_ms;
        const stepMs = niceTimeStepMs(windowMs / 4);
        ctx.strokeStyle = 'rgba(224, 224, 224, 0.25)';
        for (let ageMs = 0; ageMs <= windowMs; ageMs += stepMs) {
            const x = right - (ageMs / windowMs) * plotW;
            ctx.beginPath();
            ctx.moveTo(x, top);
            ctx.lineTo(x, bottom);
            ctx.stroke();
            if (ageMs > 0) {
                label(ctx, `-${fmtAge(ageMs)}`, x, bottom + 14, C.muted, 'center');
            }
        }
        label(ctx, 'now', right, bottom + 14, C.muted, 'right');

        if (frame.count < 2) {
            setEmpty('trends-empty', 'Waiting for beats…');
            return;
        }
        setEmpty('trends-empty', null);

        const start = frame.now_ms - windowMs;
        const xAt = (i) => left + ((frame.times_ms[i] - start) / windowMs) * plotW;

        drawSeries(ctx, frame.hr_bpm, frame.count, hrAxis, top, bottom, plotH, C.hr, xAt);
        drawSeries(ctx, frame.rmssd30_ms, frame.count, msAxis, top, bottom, plotH, C.r30, xAt);
        drawSeries(ctx, frame.rmssd300_ms, frame.count, msAxis, top, bottom, plotH, C.r300, xAt);
    }

    /** One curve; null breaks the line, so warm-up gaps stay visible as gaps. */
    function drawSeries(ctx, values, count, axis, top, bottom, plotH, color, xAt) {
        const span = axisSpan(axis);
        if (span <= 0) return;
        let open = false;
        ctx.beginPath();
        for (let i = 0; i < count; i++) {
            const v = values[i];
            if (v === null || v === undefined || !isFinite(v)) {
                open = false;
                continue;
            }
            const x = xAt(i);
            const y = bottom - ((v - axis.min) / span) * plotH;
            if (open) ctx.lineTo(x, y);
            else ctx.moveTo(x, y);
            open = true;
        }
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.6;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        ctx.stroke();
    }

    function axisSpan(axis) {
        return axis && axis.max > axis.min ? axis.max - axis.min : 0;
    }

    function axisTicks(axis) {
        if (!axis || !axis.step || axis.step <= 0 || axisSpan(axis) <= 0) return [];
        const out = [];
        const n = Math.round(axisSpan(axis) / axis.step);
        for (let i = 0; i <= n; i++) out.push(axis.min + i * axis.step);
        return out;
    }

    async function pollTrends() {
        try {
            const frame = await getJSON('/api/heart/trends');
            frames.trends = frame;
            syncTrendStepper(frame);
            drawTrends(frame);
        } catch (e) {
            console.error('Trends poll failed:', e);
        }
    }

    function syncTrendStepper(frame) {
        const node = el('trends-width-value');
        if (!node || frame.width_s === undefined) return;
        const minutes = Math.round(frame.width_s / 60);
        node.textContent = `${minutes} min`;
        const presets = frame.presets_min || [1, 2, 5, 10, 20, 30, 45, 60];
        const idx = presets.findIndex((p) => p >= minutes);
        const at = idx >= 0 ? idx : presets.length - 1;
        setStepperEnds('trends-stepper', at, presets.length);
    }

    // ------------------------------------------------------------------
    // RMSSD: the exponential tile panel
    // ------------------------------------------------------------------

    function drawRmssd(frame) {
        const canvas = el('rmssd-canvas');
        if (!canvas) return;
        const fitted = fitCanvas(canvas);
        if (!fitted) return;
        const { ctx, w, h } = fitted;

        if (!frame || !frame.tiles) return;

        const side = frame.side;
        const legendH = 26;
        const gutterL = 34;
        const gutterB = 18;
        // Gap between tiles: the visible margin between the rectangles.
        const gap = 6;
        const left = gutterL;
        const right = w;
        const top = legendH;
        const bottom = h - gutterB;
        const gridW = right - left;
        const gridH = bottom - top;
        if (gridW <= 0 || gridH <= 0) return;

        // A uniform tile grid: every tile is the same size on both axes. The
        // exponential part lives in *time* — an old column covers a longer span —
        // not in the drawn area, so the eye reads colour rather than size.
        const tileW = (gridW - gap * (side - 1)) / side;
        const tileH = (gridH - gap * (side - 1)) / side;
        if (tileW <= 0 || tileH <= 0) return;

        const colX = [];
        let x = left;
        for (let c = 0; c < side; c++) {
            colX.push(x);
            x += tileW + gap;
        }

        const stops = frame.color_stops || [];
        for (let r = 0; r < side; r++) {
            for (let c = 0; c < side; c++) {
                const y = top + r * (tileH + gap);
                const tile = frame.tiles[r * side + c] || { filled: false };
                ctx.fillStyle = tile.filled ? rmssdColor(tile.v, stops) : C.tileEmpty;
                ctx.fillRect(colX[c], y, tileW, tileH);
            }
        }

        // Window length per row, on the left gutter (top row = longest N).
        const nAt = frame.n_at || [];
        for (let r = 0; r < side; r++) {
            const y = top + r * (tileH + gap) + tileH / 2;
            label(ctx, String(nAt[r] !== undefined ? nAt[r] : ''), left - 6, y + 4, C.muted, 'right');
        }

        // Time under the columns: each label is that column's *left* edge, i.e.
        // how long ago the oldest moment it covers was. With 480 s over 4 columns
        // that reads 480, 224, 96, then "now".
        const startS = frame.column_start_s || [];
        for (let c = 0; c < side; c++) {
            const text = c === side - 1 ? 'now' : String(Math.round(startS[c] || 0));
            const tw = measure(ctx, text);
            const tx = Math.min(Math.max(colX[c] + tileW - tw, left), right - tw);
            label(ctx, text, tx, bottom + 14, C.muted, 'left');
        }

        // Legend: the RMSSD colour scale, same stops as the tiles.
        const lo = stops.length ? stops[0][0] : 0;
        const hi = stops.length ? stops[stops.length - 1][0] : 1;
        const barLeft = left;
        const barW = gridW * 0.6;
        const barH = 6;
        const barTop = 4;
        const segs = 60;
        const segW = barW / segs;
        for (let i = 0; i < segs; i++) {
            const v = lo + ((hi - lo) * (i + 0.5)) / segs;
            ctx.fillStyle = rmssdColor(v, stops);
            ctx.fillRect(barLeft + i * segW, barTop, segW + 0.5, barH);
        }
        for (let i = 0; i < stops.length; i++) {
            const sx = barLeft + ((stops[i][0] - lo) / (hi - lo)) * barW;
            const text = `${Math.round(stops[i][0])}${i === stops.length - 1 ? ' ms' : ''}`;
            const tw = measure(ctx, text);
            label(ctx, text, Math.min(Math.max(sx - tw / 2, 0), w - tw), barTop + barH + 11, C.muted, 'left');
        }
        label(ctx, 'N RR', right, barTop + 9, C.muted, 'right');

        // A hairline under the legend keeps it visually separate from the tiles.
        ctx.strokeStyle = 'rgba(224, 224, 224, 0.15)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(left, legendH - 3);
        ctx.lineTo(right, legendH - 3);
        ctx.stroke();

        const filled = frame.tiles.filter((t) => t.filled).length;
        if (filled === 0 || frame.rr_count < frame.n_shortest) {
            setEmpty('rmssd-empty', `RMSSD panel warming up…\n(needs ≥${frame.n_shortest + 1} beats)`);
        } else {
            setEmpty('rmssd-empty', null);
        }
    }

    async function pollRmssd() {
        try {
            const frame = await getJSON('/api/heart/rmssd');
            frames.rmssd = frame;
            syncRmssdControls(frame);
            drawRmssd(frame);
        } catch (e) {
            console.error('RMSSD poll failed:', e);
        }
    }

    const RMSSD_WIDTH_PRESETS = [120, 240, 480, 900, 1800, 3600];

    function syncRmssdControls(frame) {
        const widthNode = el('rmssd-width-value');
        const sideNode = el('rmssd-side-value');
        if (widthNode) {
            const s = frame.width_s;
            widthNode.textContent = s < 60 ? `${s}s` : `${Math.round(s / 60)}min`;
        }
        if (sideNode) sideNode.textContent = `${frame.side}×${frame.side}`;

        const presets = frame.width_presets_s || RMSSD_WIDTH_PRESETS;
        const idx = presets.findIndex((p) => p >= frame.width_s);
        const at = idx >= 0 ? idx : presets.length - 1;
        setStepperEnds('rmssd-width-stepper', at, presets.length);

        const lo = frame.min_side || 2;
        const hi = frame.max_side || 8;
        const sideStepper = el('rmssd-side-stepper');
        if (sideStepper) {
            const back = sideStepper.querySelector('[data-step="-1"]');
            const forward = sideStepper.querySelector('[data-step="1"]');
            if (back) back.disabled = frame.side <= lo;
            if (forward) forward.disabled = frame.side >= hi;
        }
    }

    // ------------------------------------------------------------------
    // Tabs
    // ------------------------------------------------------------------

    /** The panes of the dashboard, in tab order. */
    const PANES = ['live', 'ecg', 'beats', 'trends', 'rmssd'];

    /**
     * The pane named in the URL, so a graph can be linked to directly.
     *
     * `#beats` is the canonical form. `?tab=beats` is accepted as well, because
     * a link that went through a tool which strips the fragment still works.
     * Anything unrecognised falls back to the live tab rather than leaving the
     * page with every pane hidden.
     */
    function paneFromUrl() {
        const hash = (location.hash || '').slice(1).trim().toLowerCase();
        const param = (new URLSearchParams(location.search).get('tab') || '')
            .trim()
            .toLowerCase();
        const name = hash || param;
        return PANES.indexOf(name) >= 0 ? name : 'live';
    }

    /**
     * Reflect the shown pane in the URL.
     *
     * `replace` is used when the URL is merely being normalised (on load, or
     * after an unknown name was served as live); a real tab click pushes, so
     * Back walks through the graphs the user visited instead of leaving the
     * page. The query string is carried over untouched, since `?tab=` may be
     * the very thing that got us here.
     */
    function writePaneToUrl(name, replace) {
        const target = `#${name}`;
        if (location.hash === target) return;
        const url = `${location.pathname}${location.search}${target}`;
        if (replace) history.replaceState(null, '', url);
        else history.pushState(null, '', url);
    }

    /** A tab click: show the pane and make the URL say so. */
    function selectPane(name) {
        writePaneToUrl(showPane(name), false);
        syncLiveChart();
    }

    /**
     * Re-layout the Plotly chart when its pane becomes visible.
     *
     * Plotly sizes itself from the container at draw time, and a hidden pane
     * measures zero. Opening the page on `#beats` (or switching back to Live)
     * would otherwise leave the chart collapsed, so it is measured again once
     * the pane is actually on screen.
     */
    function syncLiveChart() {
        const chart = el('plotly-chart');
        if (!chart || activePane !== 'live') return;
        if (typeof Plotly === 'undefined' || !chart.data) return;
        try {
            Plotly.Plots.resize(chart);
        } catch (e) {
            // A chart mid-initialisation has nothing to resize yet; the next
            // redraw will pick up the real size.
        }
    }

    /**
     * Show one pane and return the one actually shown.
     *
     * Returns a value so the caller knows what the URL should now record: an
     * unknown name is served as `live`, and the link must not keep claiming
     * otherwise.
     */
    function showPane(name) {
        if (PANES.indexOf(name) < 0) name = 'live';
        activePane = name;
        document.querySelectorAll('#graph-tabs .tab').forEach((tab) => {
            tab.classList.toggle('active', tab.dataset.pane === name);
        });
        document.querySelectorAll('.pane').forEach((pane) => {
            pane.classList.toggle('active', pane.id === `pane-${name}`);
        });

        // Only the visible graph polls. Its timer starts fresh, so a tab left
        // open for a while does not fire a burst of catch-up requests on return.
        Object.keys(timers).forEach((key) => {
            clearInterval(timers[key]);
            delete timers[key];
        });
        stopEcgLoop();

        if (name === 'live') return name;
        // The strip runs on the animation frame, not the shared interval.
        if (name === 'ecg') {
            renderReadout(name);
            startEcgLoop();
            return name;
        }
        // Paint the metric row from the last status straight away, so the pane
        // does not open with a blank strip while the first poll is in flight.
        renderReadout(name);

        const pollers = {
            beats: pollBeats,
            trends: pollTrends,
            rmssd: pollRmssd,
        };
        const poll = pollers[name];
        if (!poll) return name;

        poll();
        timers[name] = setInterval(() => {
            // A slow response must not stack requests behind it.
            if (inFlight[name]) return;
            inFlight[name] = true;
            Promise.resolve(poll()).finally(() => { inFlight[name] = false; });
        }, UPDATE_INTERVAL);
        return name;
    }

    function refreshActivePane() {
        if (activePane === 'live') return;
        showPane(activePane);
    }

    function redraw() {
        if (activePane === 'ecg' && frames.ecg) drawEcg(frames.ecg);
        if (activePane === 'beats' && frames.beats) drawBeats(frames.beats);
        if (activePane === 'trends' && frames.trends) drawTrends(frames.trends);
        if (activePane === 'rmssd' && frames.rmssd) drawRmssd(frames.rmssd);
    }

    // ------------------------------------------------------------------
    // Controls
    // ------------------------------------------------------------------

    function wireStepper(stepperId, onChange) {
        const stepper = el(stepperId);
        if (!stepper) return;
        stepper.querySelectorAll('.step-btn').forEach((btn) => {
            btn.addEventListener('click', () => {
                if (btn.disabled) return;
                onChange(Number(btn.dataset.step));
            });
        });
    }

    function stepPreset(presets, current, dir) {
        const idx = presets.findIndex((p) => p >= current);
        const at = idx >= 0 ? idx : presets.length - 1;
        const next = Math.min(Math.max(at + dir, 0), presets.length - 1);
        return presets[next];
    }

    function wireControls() {
        document.querySelectorAll('#graph-tabs .tab').forEach((tab) => {
            tab.addEventListener('click', (event) => {
                // The href is there so the link can be copied or opened in a new
                // tab; the click itself is handled in place, so the browser does
                // not jump-scroll to the anchor. Modified clicks (new tab, new
                // window) keep their default behaviour, since leaving this page
                // is exactly what the user asked for.
                if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) {
                    return;
                }
                event.preventDefault();
                selectPane(tab.dataset.pane);
            });
        });

        // A hand-edited URL, or Back/Forward between tab links, switches panes
        // too. `popstate` covers history navigation (a tab click uses
        // pushState, which does not fire `hashchange`), and `hashchange`
        // covers a fragment typed straight into the address bar. showPane() is
        // idempotent, so re-applying the current one is cheap and keeps the
        // visible graph matching the URL.
        const followUrl = () => {
            const name = paneFromUrl();
            if (name !== activePane) writePaneToUrl(showPane(name), true);
            syncLiveChart();
        };
        window.addEventListener('popstate', followUrl);
        window.addEventListener('hashchange', followUrl);

        wireStepper('ecg-window-stepper', (dir) => {
            const current = frames.ecg ? frames.ecg.window_rr : 10;
            postConfig({ ecg_window: stepPreset(ecgPresets, current, dir) });
        });

        wireStepper('beats-stepper', (dir) => {
            const limit = frames.beats ? frames.beats.limit : 64;
            const steps = frames.beats ? frames.beats.steps : [1, 2, 4, 8, 16, 32, 64];
            postConfig({ beats_limit: stepPreset(steps, limit, dir) });
        });

        wireStepper('trends-stepper', (dir) => {
            const presets = frames.trends ? frames.trends.presets_min : [1, 2, 5, 10, 20, 30, 45, 60];
            const minutes = frames.trends ? Math.round(frames.trends.width_s / 60) : 10;
            postConfig({ trend_width: stepPreset(presets, minutes, dir) * 60 });
        });

        wireStepper('rmssd-width-stepper', (dir) => {
            const presets = frames.rmssd ? frames.rmssd.width_presets_s : RMSSD_WIDTH_PRESETS;
            const current = frames.rmssd ? frames.rmssd.width_s : 480;
            postConfig({ rmssd_width: stepPreset(presets, current, dir) });
        });

        wireStepper('rmssd-side-stepper', (dir) => {
            const frame = frames.rmssd;
            const side = frame ? frame.side : 4;
            const lo = frame ? frame.min_side : 2;
            const hi = frame ? frame.max_side : 8;
            const next = Math.min(Math.max(side + dir, lo), hi);
            postConfig({ rmssd_side: next });
        });

        // Tapping the ECG freezes the frame on screen; the clock keeps running
        // behind it, so resuming shows live samples rather than catching up.
        const ecgCanvas = el('ecg-canvas');
        if (ecgCanvas) {
            ecgCanvas.addEventListener('click', () => {
                frozenEcg = !frozenEcg;
                const hint = el('ecg-hint');
                if (hint) hint.textContent = frozenEcg ? 'Frozen — tap to resume' : 'Tap the plot to freeze';
                // Resuming simply lets the next frames land: the server kept its
                // clock with the data, so the strip is already up to date rather
                // than needing to slide forward.
                if (frozenEcg) redraw();
            });
        }

        // Repaint on resize without re-fetching: the frames are already held.
        let resizeTimer = null;
        window.addEventListener('resize', () => {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(redraw, 100);
        });
    }

    async function loadHeartStatus() {
        try {
            heartStatus = await getJSON('/api/heart/status');
        } catch (e) {
            console.error('Heart status failed:', e);
        }
    }

    async function start() {
        wireControls();
        await loadHeartStatus();
        // Remembered stepper values are re-applied before the first pane opens,
        // so a restarted server shows the tuning the user left behind rather
        // than the config.conf defaults.
        await restoreSettings();
        // Open on whichever pane the URL names, so /#beats is a shareable link
        // to the beat graph. showPane() reports what it actually opened, and the
        // write-back replaces rather than pushes, so the link does not gain a
        // history entry on load.
        writePaneToUrl(showPane(paneFromUrl()), true);
        syncLiveChart();
        setInterval(async () => {
            await loadHeartStatus();
            if (activePane !== 'live') renderReadout(activePane);
        }, 1000);
    }

    document.addEventListener('DOMContentLoaded', start);
})();
