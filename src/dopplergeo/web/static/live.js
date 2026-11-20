// Animated live view of a locate run: spectrogram + flyover detection on the left, the search on a
// US map on the right. Tails /jobs/<id>/live.json while the job runs; finished jobs open on the final
// picture with a Replay button.
(function () {
  'use strict';
  const root = document.getElementById('live');
  if (!root) return;
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const $ = id => document.getElementById(id);
  const ease = t => 1 - Math.pow(1 - Math.min(Math.max(t, 0), 1), 3);
  const now = () => performance.now();
  const fmtInt = n => Math.round(n).toLocaleString();

  // ------------------------------------------------------------------ state
  let pos = 0, base = null, finishedSeen = false;
  let animate = (root.dataset.state !== 'done' && root.dataset.state !== 'failed') ||
                !!new URLSearchParams(location.search).get('replay');
  const queue = [];
  let busyUntil = 0;
  const S = {spec: null, img: null, reveal: 0, ridges: [], flyovers: [], rejected: [], scanning: false,
             expected: null, win: null, trials: [], hits: [], cand: null, bestCand: null, nHits: 0};
  // a long backlog (e.g. opening a page mid-run) plays quickly instead of crawling one window at a time
  const pace = ms => (!animate ? 0 : queue.length > 150 ? 0 : queue.length > 40 ? ms / 4 : ms);

  // ------------------------------------------------------------------ stage chips + ticker + counters
  const stages = ['audio', 'detect', 'flights', 'search', 'rank'];
  function setStage(st, done = false) {
    const i = stages.indexOf(st);
    root.querySelectorAll('#lv-stages span').forEach((el, k) => {
      el.classList.toggle('done', done ? k <= i : k < i);
      el.classList.toggle('active', !done && k === i);
    });
  }
  function tick(msg, cls = '') {
    const box = $('lv-log');
    const d = document.createElement('div');
    d.className = 'lvline ' + cls + (animate ? ' in' : '');
    d.innerHTML = msg;
    box.prepend(d);
    while (box.children.length > 7) box.lastChild.remove();
  }
  const counters = {};
  function countTo(id, value, fmt = fmtInt) {
    const el = $(id);
    const c = counters[id] || (counters[id] = {from: 0, to: 0, t0: 0});
    c.from = c.cur ?? 0; c.to = value; c.t0 = now();
    if (!animate) { el.textContent = fmt(value); c.cur = value; return; }
    const step = () => {
      const k = ease((now() - c.t0) / 700);
      c.cur = c.from + (c.to - c.from) * k;
      el.textContent = fmt(c.cur);
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ------------------------------------------------------------------ spectrogram canvas
  const cv = $('lv-spec'), g = cv.getContext('2d');
  function sizeCanvas() {
    const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    cv.width = Math.max(300, r.width * dpr); cv.height = Math.max(200, r.height * dpr);
  }
  sizeCanvas();
  window.addEventListener('resize', sizeCanvas);
  const PAD = {l: 46, r: 10, t: 12, b: 26};
  // ---- zoom: the visible part of the spectrogram glides to each drop as it is found, then back out
  let V = null, Z = null;  // V: current view {t0,t1,f0,f1}; Z: running zoom animation
  const fullView = () => ({t0: S.spec.t0, t1: S.spec.t1, f0: S.spec.f0, f1: S.spec.f1});
  const isLog = () => S.spec.scale === 'log';
  const fq = (f, v) => isLog() ? Math.log(Math.max(f, 1) / v.f0) / Math.log(v.f1 / v.f0) : (f - v.f0) / (v.f1 - v.f0);
  function view() {
    if (!Z) return V || fullView();
    const k = ease((now() - Z.start) / Z.dur), a = Z.from, b = Z.to;
    const lerp = (p, q) => p + (q - p) * k, glerp = (p, q) => isLog() ? p * Math.pow(q / p, k) : lerp(p, q);
    const v = {t0: lerp(a.t0, b.t0), t1: lerp(a.t1, b.t1), f0: glerp(a.f0, b.f0), f1: glerp(a.f1, b.f1)};
    if (k >= 1) { V = b; Z = null; }
    return v;
  }
  let zoomBackAt = 0, zoomLabel = '';
  function zoomTo(t, f, label, hold) {
    if (!animate || !S.spec || !t || t.length < 2) return;
    const F = fullView();
    const tl = Math.min(...t), th = Math.max(...t), fl = Math.min(...f), fh = Math.max(...f);
    const padT = Math.max(4, (th - tl) * 0.15), ratio = isLog() ? 1.5 : 1;
    const to = {t0: Math.max(F.t0, tl - padT), t1: Math.min(F.t1, th + padT),
                f0: Math.max(F.f0, isLog() ? fl / ratio : fl - (fh - fl)), f1: Math.min(F.f1, isLog() ? fh * ratio : fh + (fh - fl))};
    Z = {from: view(), to, start: now(), dur: 850};
    zoomLabel = label; zoomBackAt = now() + hold;
  }
  function zoomOut() {
    if (!S.spec || (!V && !Z)) return;
    Z = {from: view(), to: fullView(), start: now(), dur: 700};
    zoomLabel = ''; zoomBackAt = 0;
    setTimeout(() => { if (!Z) V = null; }, 760);
  }
  let CV = null;  // view used by the frame being drawn
  const X = t => { const d = window.devicePixelRatio || 1, w = cv.width - (PAD.l + PAD.r) * d, v = CV || fullView();
    return PAD.l * d + (t - v.t0) / (v.t1 - v.t0) * w; };
  const Y = f => { const d = window.devicePixelRatio || 1, h = cv.height - (PAD.t + PAD.b) * d;
    return PAD.t * d + (1 - fq(f, CV || fullView())) * h; };

  function drawCurve(t, f, color, lw, dash, glow) {
    if (!Array.isArray(t) || !Array.isArray(f) || t.length < 2) return;
    g.save(); g.strokeStyle = color; g.lineWidth = lw; g.setLineDash(dash || []);
    if (glow) { g.shadowColor = color; g.shadowBlur = glow; }
    g.beginPath(); g.moveTo(X(t[0]), Y(f[0]));
    for (let i = 1; i < t.length; i++) g.lineTo(X(t[i]), Y(f[i]));
    g.stroke(); g.restore();
  }

  function drawSpec() {
    const d = window.devicePixelRatio || 1;
    g.clearRect(0, 0, cv.width, cv.height);
    g.fillStyle = '#05060a'; g.fillRect(0, 0, cv.width, cv.height);
    if (!S.spec || !S.img) {
      requestAnimationFrame(drawSpec);
      return;
    }
    const x0 = PAD.l * d, y0 = PAD.t * d, w = cv.width - (PAD.l + PAD.r) * d, h = cv.height - (PAD.t + PAD.b) * d;
    if (zoomBackAt && now() > zoomBackAt) zoomOut();
    CV = view();
    const F = fullView(), iw = S.img.naturalWidth, ih = S.img.naturalHeight;
    const sx = (CV.t0 - F.t0) / (F.t1 - F.t0) * iw, sw = (CV.t1 - CV.t0) / (F.t1 - F.t0) * iw;
    const sy = (1 - fq(CV.f1, F)) * ih, sh = (fq(CV.f1, F) - fq(CV.f0, F)) * ih;
    const rv = animate ? ease((now() - S.reveal) / 1400) : 1;
    g.save(); g.beginPath(); g.rect(x0, y0, w * rv, h); g.clip();
    g.imageSmoothingEnabled = true;
    g.drawImage(S.img, sx, sy, Math.max(1, sw), Math.max(1, sh), x0, y0, w, h);
    g.restore();
    if (rv < 1) {  // glowing reveal edge
      const ex = x0 + w * rv;
      const grd = g.createLinearGradient(ex - 40 * d, 0, ex + 4 * d, 0);
      grd.addColorStop(0, 'rgba(95,211,255,0)'); grd.addColorStop(1, 'rgba(95,211,255,.9)');
      g.fillStyle = grd; g.fillRect(ex - 40 * d, y0, 44 * d, h);
    }
    // axes
    g.fillStyle = 'rgba(255,255,255,.55)'; g.font = `${11 * d}px system-ui`; g.textAlign = 'right';
    const cand = isLog() ? [40, 50, 60, 70, 80, 90, 100, 120, 140, 160, 180, 200, 250, 300, 400, 500, 600, 800, 1000, 1500, 2000, 3000]
      : Array.from({length: 60}, (_, k) => (Math.ceil(CV.f0 / 10) + k) * 10);
    const ticks = [];
    for (const f of cand.filter(f => f >= CV.f0 && f <= CV.f1)) if (!ticks.length || Y(ticks[ticks.length - 1]) - Y(f) > 22 * d) ticks.push(f);
    for (const f of ticks) {
      g.fillText((f >= 1000 ? f / 1000 + 'k' : f) + ' Hz', x0 - 6 * d, Y(f) + 4 * d);
      g.fillStyle = 'rgba(255,255,255,.06)'; g.fillRect(x0, Y(f), w, 1); g.fillStyle = 'rgba(255,255,255,.55)';
    }
    g.textAlign = 'center';
    const span = CV.t1 - CV.t0, tstep = span > 600 ? 120 : span > 240 ? 60 : span > 90 ? 20 : span > 40 ? 10 : span > 15 ? 5 : 2;
    for (let t = Math.ceil(CV.t0 / tstep) * tstep; t <= CV.t1; t += tstep) g.fillText(t + ' s', X(t), cv.height - 8 * d);
    // everything below stays inside the plot, also while zoomed
    g.save(); g.beginPath(); g.rect(x0, y0, w, h); g.clip();
    if (zoomLabel || Z) {  // zoom badge
      const zl = zoomLabel || 'zooming out';
      g.font = `600 ${11 * d}px system-ui`; g.textAlign = 'right';
      const tw = g.measureText(zl).width;
      g.fillStyle = 'rgba(8,11,17,.85)'; g.fillRect(x0 + w - tw - 14 * d, y0 + 6 * d, tw + 10 * d, 18 * d);
      g.fillStyle = '#7ee6a0'; g.fillText(zl, x0 + w - 9 * d, y0 + 19 * d);
      g.textAlign = 'left';
    }
    // search: the candidate flight's predicted pitch curve vs the recording
    const drawCand = (C, color, label, dash) => {
      if (!C) return;
      C.curves.forEach((cv_, j) => {
        if (!cv_.t || cv_.t.length < 2) return;
        drawCurve(cv_.t, cv_.f, color, 2.5 * d, dash, 12 * d);
        const fl = C.flights[j] || {}, mid = Math.floor(cv_.t.length * 0.2);
        g.font = `600 ${11 * d}px system-ui`; g.textAlign = 'left';
        const txt = `${label} ${fl.icao24 || ''}${fl.type_code ? ' · ' + fl.type_code : ''}`;
        const lx = Math.max(x0 + 4 * d, Math.min(X(cv_.t[mid]), x0 + w - g.measureText(txt).width - 8 * d));
        const ly = Math.max(y0 + 34 * d, Y(cv_.f[mid]) - 10 * d);
        g.fillStyle = 'rgba(8,11,17,.8)'; g.fillRect(lx - 3 * d, ly - 12 * d, g.measureText(txt).width + 6 * d, 16 * d);
        g.fillStyle = color; g.fillText(txt, lx, ly);
      });
    };
    if (S.cand && S.cand !== S.bestCand) drawCand(S.cand, 'rgba(95,211,255,.95)', 'trying', [8 * d, 6 * d]);
    drawCand(S.bestCand, '#7ee6a0', S.cand && S.cand !== S.bestCand ? 'best so far' : 'best match', []);
    if (S.win && S.scanning) {
      const W = S.win, wx0 = X(W.s), wx1 = X(W.e), col = W.band === 'jet' ? '255,190,90' : '95,211,255';
      g.fillStyle = `rgba(${col},.10)`; g.fillRect(wx0, y0, wx1 - wx0, h);
      g.strokeStyle = `rgba(${col},.9)`; g.lineWidth = 1.5 * d; g.strokeRect(wx0, y0 + 1, wx1 - wx0, h - 2);
      const [blo, bhi] = W.band === 'jet' ? [300, 3000] : [60, 300];
      g.fillStyle = `rgba(${col},.07)`; g.fillRect(wx0, Y(bhi), wx1 - wx0, Y(blo) - Y(bhi));
      g.font = `600 ${11 * d}px system-ui`; g.textAlign = 'left';
      const lbl = `${W.band === 'jet' ? 'jet band' : 'prop band'} · ${Math.round(W.s)}–${Math.round(W.e)} s · tone ${Math.round(W.cover * 100)} %`;
      const lx = Math.min(wx0 + 4 * d, x0 + w - g.measureText(lbl).width - 8 * d);
      g.fillStyle = 'rgba(8,11,17,.8)'; g.fillRect(lx - 3 * d, y0 + h - 20 * d, g.measureText(lbl).width + 6 * d, 16 * d);
      g.fillStyle = `rgb(${col})`; g.fillText(lbl, lx, y0 + h - 8 * d);
    }
    // rejected stretches
    for (const r of S.rejected) {
      const a = animate ? ease((now() - r.born) / 500) : 1;
      g.fillStyle = `rgba(255,90,90,${0.035 * a})`;
      g.fillRect(X(r.window_s[0]), y0, X(r.window_s[1]) - X(r.window_s[0]), h);
    }
    // tone points
    for (const R of S.ridges) {
      const n = R.t.length, k = animate ? ease((now() - R.born) / 1600) : 1;
      const col = R.band === 'jet' ? '255,190,90' : '95,211,255';
      g.fillStyle = `rgba(${col},.85)`; g.shadowColor = `rgba(${col},.8)`; g.shadowBlur = 6 * d;
      for (let i = 0; i < n * k; i++) { g.beginPath(); g.arc(X(R.t[i]), Y(R.f[i]), 1.6 * d, 0, 6.3); g.fill(); }
      g.shadowBlur = 0;
    }
    // detection: curves fitted in the windows tried (green: fits a passing plane, red dashes: rejected)
    for (const hcv of S.hits) drawCurve(hcv.t, hcv.f, 'rgba(126,230,160,.9)', 2.5 * d, [], 8 * d);
    for (const tr of S.trials) {
      const a = 1 - Math.min(1, (now() - tr.born) / 1600);
      if (a <= 0) continue;
      drawCurve(tr.t, tr.f, tr.ok ? `rgba(126,230,160,${a})` : `rgba(255,120,120,${0.95 * a})`, 3 * d, tr.ok ? [] : [6 * d, 4 * d], tr.ok ? 10 * d : 0);
    }
    S.trials = S.trials.filter(tr => now() - tr.born < 1600);
    // fitted flyover curves
    S.flyovers.forEach((fo, idx) => {
      const k = animate ? ease((now() - fo.born) / 1300) : 1;
      const m = Math.max(2, Math.floor(fo.t.length * k));
      const grd = g.createLinearGradient(X(fo.t[0]), 0, X(fo.t[fo.t.length - 1]), 0);
      grd.addColorStop(0, '#ffd166'); grd.addColorStop(1, '#ff5d8f');
      g.strokeStyle = grd; g.lineWidth = 3 * d; g.shadowColor = '#ff8fab'; g.shadowBlur = 14 * d;
      g.beginPath(); g.moveTo(X(fo.t[0]), Y(fo.f[0]));
      for (let i = 1; i < m; i++) g.lineTo(X(fo.t[i]), Y(fo.f[i]));
      g.stroke(); g.shadowBlur = 0;
      if (k >= 1) {  // closest-approach marker, pulsing
        const cx = X(fo.t_cpa), cy = Y(fo.f0), ph = ((now() - fo.born) / 1200) % 1;
        g.strokeStyle = `rgba(255,255,255,${1 - ph})`; g.lineWidth = 2 * d;
        g.beginPath(); g.arc(cx, cy, (6 + 18 * ph) * d, 0, 6.3); g.stroke();
        g.fillStyle = '#fff'; g.beginPath(); g.arc(cx, cy, 4 * d, 0, 6.3); g.fill();
        g.fillStyle = 'rgba(255,255,255,.92)'; g.font = `600 ${12 * d}px system-ui`; g.textAlign = 'left';
        g.fillText(`${fo.kind === 'jet' ? 'Jet' : 'Plane'} ${idx + 1} · closest at ${fo.t_cpa.toFixed(1)} s`, Math.min(cx + 12 * d, cv.width - 190 * d), cy - 12 * d);
      }
    });
    // what was expected (shown when no flyover was found)
    if (S.expected) {
      const E = S.expected, k = animate ? ease((now() - E.born) / 1600) : 1, dd = d;
      const [blo, bhi] = E.band;
      g.save();
      g.globalAlpha = 0.9 * k; g.strokeStyle = 'rgba(255,255,255,.55)'; g.setLineDash([3 * dd, 5 * dd]); g.lineWidth = dd;
      [blo, bhi].forEach(f => { g.beginPath(); g.moveTo(x0, Y(f)); g.lineTo(x0 + w, Y(f)); g.stroke(); });
      g.fillStyle = 'rgba(255,255,255,.04)'; g.fillRect(x0, Y(bhi), w, Y(blo) - Y(bhi));
      g.setLineDash([]); g.fillStyle = 'rgba(255,255,255,.8)'; g.font = `${11 * dd}px system-ui`; g.textAlign = 'left';
      g.fillText(`searched for engine tones here (${blo}–${bhi} Hz)`, x0 + 6 * dd, Y(bhi) - 5 * dd);
      // near misses
      (E.near_misses || []).forEach((nm, j) => {
        const kk = animate ? ease((now() - E.born - 600 - j * 250) / 900) : 1;
        if (kk <= 0) return;
        g.strokeStyle = '#ff6b6b'; g.lineWidth = 2 * dd; g.setLineDash([7 * dd, 5 * dd]); g.globalAlpha = kk;
        g.beginPath(); nm.t.forEach((t, i) => { if (i === 0) g.moveTo(X(t), Y(nm.f[i])); else g.lineTo(X(t), Y(nm.f[i])); }); g.stroke();
        g.setLineDash([]); g.fillStyle = '#ffb3b3'; g.font = `600 ${11 * dd}px system-ui`;
        const lx = X(nm.t[Math.floor(nm.t.length * 0.15)]), ly = Y(nm.f[Math.floor(nm.f.length * 0.15)]) - 10 * dd;
        g.fillText('closest attempt: ' + (nm.reasons[0] || '').slice(0, 70), Math.max(x0 + 4 * dd, Math.min(lx, x0 + w - 380 * dd)), Math.max(y0 + 28 * dd, ly));
      });
      // the expected shape: marching dashes, drawn in
      const ex = E.expected, m = Math.max(2, Math.floor(ex.t.length * k));
      g.globalAlpha = 1; g.strokeStyle = '#ffffff'; g.lineWidth = 2.5 * dd; g.shadowColor = '#fff'; g.shadowBlur = 10 * dd;
      g.setLineDash([10 * dd, 8 * dd]); g.lineDashOffset = -((now() / 40) % 1000);
      g.beginPath(); g.moveTo(X(ex.t[0]), Y(ex.f[0]));
      for (let i = 1; i < m; i++) g.lineTo(X(ex.t[i]), Y(ex.f[i]));
      g.stroke(); g.setLineDash([]); g.shadowBlur = 0;
      if (k >= 1) {
        const cx = X(ex.t_cpa), cy = Y(ex.f0), ph = (now() / 1300) % 1;
        g.strokeStyle = `rgba(255,255,255,${1 - ph})`; g.lineWidth = 2 * dd;
        g.beginPath(); g.arc(cx, cy, (6 + 22 * ph) * dd, 0, 6.3); g.stroke();
        const bx = Math.min(cx + 16 * dd, x0 + w - 300 * dd), by = Math.max(y0 + 46 * dd, cy - 46 * dd);
        g.fillStyle = 'rgba(8,11,17,.85)'; g.fillRect(bx - 6 * dd, by - 16 * dd, 296 * dd, 40 * dd);
        g.fillStyle = '#fff'; g.font = `700 ${12 * dd}px system-ui`;
        g.fillText('Expected: a line like this, sliding down', bx, by);
        g.font = `${11 * dd}px system-ui`; g.fillStyle = 'rgba(255,255,255,.75)';
        g.fillText('steepest where the plane is nearest (circle)', bx, by + 16 * dd);
      }
      g.restore();
    }
    g.restore();  // plot clip
    requestAnimationFrame(drawSpec);
  }
  requestAnimationFrame(drawSpec);

  // ------------------------------------------------------------------ map + animated heat cells
  const mapEl = $('lv-map');
  const map = mapEl ? USMap.create(mapEl) : null;  // analyse jobs have no map
  const svg = L.svg({padding: 0.5});
  let region = null, bestMk = null, truth = null, scoreMin = Infinity, scoreMax = -Infinity, cellStep = 200;
  const trackLayers = {};
  const Heat = L.Layer.extend({
    onAdd(m) {
      this._m = m; this.cells = this.cells || [];
      this._c = L.DomUtil.create('canvas', 'lv-heat leaflet-zoom-hide');
      m.getPanes().overlayPane.appendChild(this._c);
      m.on('moveend zoomend resize viewreset', this._reset, this);
      this._reset(); this._loop();
    },
    _reset() {
      const sz = this._m.getSize(), d = window.devicePixelRatio || 1;
      this._c.width = sz.x * d; this._c.height = sz.y * d;
      this._c.style.width = sz.x + 'px'; this._c.style.height = sz.y + 'px';
      L.DomUtil.setPosition(this._c, this._m.containerPointToLayerPoint([0, 0]));
    },
    add(lat, lon, score, step) {
      const t = now();
      for (let i = 0; i < lat.length; i++) {
        this.cells.push({lat: lat[i], lon: lon[i], s: score[i], born: animate ? t + Math.random() * 350 : t - 9999, step});
      }
    },
    _loop() {
      const m = this._m, ctx = this._c.getContext('2d'), d = window.devicePixelRatio || 1, t = now();
      ctx.clearRect(0, 0, this._c.width, this._c.height);
      const mpp = 40075016.686 * Math.cos(m.getCenter().lat * Math.PI / 180) / Math.pow(2, m.getZoom() + 8);
      const span = (scoreMax - scoreMin) || 1;
      for (const c of this.cells) {
        const a = ease((t - c.born) / 600);
        if (a <= 0) continue;
        const p = m.latLngToContainerPoint([c.lat, c.lon]);
        const r = Math.max(1.5, (c.step / 2) / mpp) * (1 + 0.8 * (1 - a));
        const q = Math.max(0, (c.s - scoreMin) / span);
        ctx.globalAlpha = 0.25 + 0.7 * a * (0.35 + 0.65 * q);
        ctx.fillStyle = USMap.ramp(q);
        ctx.fillRect((p.x - r) * d, (p.y - r) * d, 2 * r * d, 2 * r * d);
      }
      ctx.globalAlpha = 1;
      requestAnimationFrame(() => this._m && this._loop());
    },
    onRemove(m) { m.off('moveend zoomend resize viewreset', this._reset, this); this._c.remove(); this._m = null; },
  });
  const heat = map ? new Heat().addTo(map) : null;

  function growCircle(lat, lon, radius) {
    if (region) region.remove();
    region = L.circle([lat, lon], {radius: 1, renderer: svg, className: 'lv-region', color: css('--accent'), weight: 2,
                                   fillOpacity: 0.05, interactive: false}).addTo(map);
    const target = L.latLng(lat, lon).toBounds(radius * 2);  // (a circle needs a map before it has bounds)
    if (animate) map.flyToBounds(target, {padding: [20, 20], duration: 1.4}); else map.fitBounds(target, {padding: [20, 20]});
    const t0 = now();
    const step = () => {
      const k = animate ? ease((now() - t0) / 1600) : 1;
      region.setRadius(Math.max(1, radius * k));
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function pulseIcon(cls) {
    return L.divIcon({className: 'lv-pulse ' + cls, html: '<span></span><span></span><i></i>', iconSize: [44, 44]});
  }
  function moveBest(lat, lon) {
    if (!bestMk) { bestMk = L.marker([lat, lon], {icon: pulseIcon('best'), interactive: false, zIndexOffset: 1000}).addTo(map); return; }
    const from = bestMk.getLatLng(), t0 = now();
    if (!animate) { bestMk.setLatLng([lat, lon]); return; }
    const step = () => {
      const k = ease((now() - t0) / 450);
      bestMk.setLatLng([from.lat + (lat - from.lat) * k, from.lng + (lon - from.lng) * k]);
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ------------------------------------------------------------------ event handling
  const handlers = {
    start(e) { if (e.truth) truth = e.truth; tick('Analysis started'); },
    stage(e) {
      setStage(e.stage);
      if (e.stage === 'detect') { S.scanning = true; $('lv-speccap').textContent = 'Listening for engine tones…'; }
      if (e.stage === 'flights') { S.scanning = false; S.win = null; S.hits = []; }
      const msg = {audio: 'Reading the recording', detect: 'Looking for planes passing', flights: 'Loading flight paths for that time',
                   search: 'Searching the area', rank: 'Ranking the candidate spots'}[e.stage];
      if (msg) tick(msg, 'stage');
      return 350;
    },
    spectrogram(e) {
      S.spec = e;
      const img = new Image();
      img.onload = () => { S.img = img; S.reveal = now(); };
      img.src = base + e.image;
      $('lv-speccap').textContent = `${e.duration.toFixed(0)} s of audio, ${e.f0.toFixed(0)}–${e.f1.toFixed(0)} Hz`;
      return 1200;
    },
    ridge(e) {
      S.ridges.push({...e, born: now()});
      tick(e.band === 'jet' ? `Jet-engine band: clear tone at ${fmtInt(e.t.length)} moments`
                            : `Propeller band: clear tone at ${fmtInt(e.t.length)} moments`);
      return 1300;
    },
    analysed(e) {
      tick(e.found ? `<b>${e.found} aircraft pass${e.found === 1 ? '' : 'es'} found</b>: choose where to search below`
                   : '<b>No aircraft found</b>: no search area needed', e.found ? 'good' : 'warn');
      return 0;
    },
    rejected(e) {
      e.items.forEach((r, i) => S.rejected.push({...r, born: now() + i * 120}));
      if (e.items.length) tick(`${e.items.length} stretch${e.items.length > 1 ? 'es' : ''} without a clean flyover`, 'warn');
      return 400;
    },
    window(e) {
      S.scanning = true; S.win = e;
      if (Array.isArray(e.f) && animate) S.trials.push({t: e.t, f: e.f, ok: e.ok, born: now()});
      if (e.ok && Array.isArray(e.f)) {
        S.hits.push({t: e.t, f: e.f}); S.nHits++;
        zoomTo(e.t, e.f, `🔍 possible pass · ${Math.round(e.s)}–${Math.round(e.e)} s`, 1900);
        tick(`Possible ${e.band === 'jet' ? 'jet' : 'plane'} pass around ${Math.round((e.s + e.e) / 2)} s: the tone bends like a Doppler curve`, 'good');
      }
      $('lv-speccap').textContent = `${e.band === 'jet' ? 'Jet band' : 'Propeller band'}: window ${e.i + 1} of ${e.n}` +
        (e.ok ? ' · curve fits' : e.reason ? ' · ' + e.reason.split(' (')[0] : '') +
        (S.nHits ? ` · ${S.nHits} possible pass${S.nHits > 1 ? 'es' : ''}` : '');
      return e.ok && animate && queue.length <= 150 ? 2400 : pace(45);
    },
    candidate(e) {
      S.cand = e;
      if (!S.bestCand || (e.score ?? -Infinity) > (S.bestCand.score ?? -Infinity)) S.bestCand = e;
      const names = e.flights.map(f => `<b>${f.icao24}</b>${f.type_code ? ' (' + f.type_code + ')' : ''}`).join(' + ');
      $('lv-speccap').textContent = `Flight ${e.combo + 1} of ${e.n}: ${e.flights.map(f => f.icao24).join(' + ')}` +
        ' · dashed = its predicted pitch, green = best match so far';
      tick(`Tried ${names}: predicted pitch curve scored ${e.score != null ? Math.round(e.score).toLocaleString() : '—'}` +
           (S.bestCand === e ? ' · <b>best so far</b>' : ''), S.bestCand === e ? 'good' : '');
      return pace(e.n > 30 ? 150 : 900);
    },
    flyover(e) {
      S.scanning = false; S.win = null; S.trials = []; S.hits = [];
      S.flyovers.push({...e, born: now()});
      zoomTo(e.t, e.f, `🔍 ${e.kind === 'jet' ? 'jet' : 'plane'} ${e.i + 1} · closest at ${e.t_cpa.toFixed(1)} s`, 2600);
      $('lv-speccap').textContent = `${S.flyovers.length} flyover${S.flyovers.length > 1 ? 's' : ''} found`;
      tick(`<b>${e.kind === 'jet' ? 'Jet' : 'Propeller plane'} ${e.i + 1}</b>: ~${Math.round(e.v)} m/s, closest ~${(e.d / 1000).toFixed(1)} km, engine tone ${e.f0.toFixed(1)} Hz`, 'good');
      return animate ? 3200 : 0;
    },
    expected(e) {
      S.scanning = false; S.win = null; S.hits = [];
      S.expected = {...e, born: now()};
      $('lv-speccap').textContent = 'No complete flyover found: here is what was expected, and where it looked';
      tick('No complete plane pass found. The dashed white curve shows what one would look like; red dashes are the closest attempts.', 'warn');
      return 2400;
    },
    flights(e) { tick(`${fmtInt(e.segments)} flight paths loaded (${fmtInt(e.rows)} positions)`); return 300; },
    search(e) {
      cellStep = e.step_m;
      growCircle(e.lat, e.lon, e.radius_m);
      tick(`Search area: ${(e.radius_m / 1000).toFixed(1)} km radius, ${fmtInt(e.points)} spots at ${Math.round(e.step_m)} m`);
      return 1500;
    },
    tracks(e) {
      if (!map) return 0;
      e.tracks.forEach((tr, i) => {
        const pl = L.polyline(tr.lat.map((v, k) => [v, tr.lon[k]]), {renderer: svg, className: 'lv-track', color: css('--accent'),
          weight: 2, opacity: 0.85, interactive: true})
          .bindTooltip(`${tr.icao24}${tr.type_code ? ' · ' + tr.type_code : ''}`, {sticky: true}).addTo(map);
        const el = pl.getElement();
        if (el && animate) { el.style.animationDelay = (i * 0.06) + 's'; } else if (el) el.classList.add('static');
        trackLayers[tr.key] = pl;
      });
      tick(`${e.tracks.length} candidate flight${e.tracks.length === 1 ? '' : 's'} passed close enough to be heard`);
      return Math.min(2200, 600 + e.tracks.length * 60);
    },
    combos(e) { countTo('lv-combos', e.n); return 200; },
    combo(e) {
      Object.entries(trackLayers).forEach(([k, pl]) => {
        const el = pl.getElement(); if (!el) return;
        el.classList.toggle('active', e.keys.includes(k)); el.classList.toggle('dim', !e.keys.includes(k));
      });
      if (e.n > 1) tick(`Testing flight set ${e.i + 1} of ${e.n}`);
      return 250;
    },
    scores(e) {
      e.score.forEach(s => { if (s < scoreMin) scoreMin = s; if (s > scoreMax) scoreMax = s; });
      heat.add(e.lat, e.lon, e.score, cellStep);
      countTo('lv-scored', e.total);
      return 120;
    },
    best(e) { moveBest(e.lat, e.lon); $('lv-best').textContent = Math.round(e.score).toLocaleString(); return 60; },
    refine(e) {
      cellStep = e.step_m;
      tick(`Zooming in: ${Math.round(e.step_m)} m grid around the best spots`, 'stage');
      if (bestMk && animate) map.flyTo(bestMk.getLatLng(), Math.min(map.getZoom() + 1, 15), {duration: 1.0});
      return 700;
    },
    result(e) {
      setStage('rank', true);
      S.cand = S.bestCand;
      moveBest(e.lat, e.lon);
      if (animate) map.flyTo([e.lat, e.lon], 14, {duration: 2.0}); else map.setView([e.lat, e.lon], 14);
      setTimeout(() => {
        L.marker([e.lat, e.lon], {icon: L.divIcon({className: 'lv-found', html: '<div class="ring"></div><div class="tag">Most likely location</div>', iconSize: [0, 0]}),
                                  interactive: false}).addTo(map);
        if (truth) L.marker([truth.lat, truth.lon], {icon: L.divIcon({className: 'truthicon', html: '★', iconSize: [22, 22]})})
          .bindTooltip('true location (testing)').addTo(map);
      }, animate ? 1900 : 0);
      tick(`<b>Most likely location</b> ${e.lat.toFixed(5)}, ${e.lon.toFixed(5)}` +
           (e.margin != null ? ` · ${Math.round(e.margin * 100)} % ahead of the next flight set` : ''), 'good');
      return 2200;
    },
    done() { root.classList.add('finished'); tick('Done ✓', 'good'); finishedSeen = true; return 0; },
    failed(e) { root.classList.add('failedrun'); S.scanning = false; tick('Stopped: ' + e.error, 'warn');
                if (!S.expected) $('lv-speccap').textContent = 'Stopped: see the report below'; finishedSeen = true; return 0; },
  };

  function pump() {
    while (queue.length && (!animate || now() >= busyUntil)) {
      const e = queue.shift();
      const h = handlers[e.type];
      const dur = h ? (h(e) || 0) : 0;
      if (animate) busyUntil = now() + dur;
    }
    // tells the job poller it may reload for the full results once the finale has played
    window.__liveIdle = finishedSeen && !queue.length && now() > busyUntil + 2500;
    requestAnimationFrame(pump);
  }
  requestAnimationFrame(pump);

  async function poll() {
    try {
      const r = await fetch(root.dataset.src + '?pos=' + pos, {cache: 'no-store'});
      const d = await r.json();
      if (d.base) base = d.base;
      pos = d.pos; queue.push(...d.events);
      if (d.events.length && pos) setTimeout(poll, 50);
      else if (d.state === 'running' || d.state === 'queued') setTimeout(poll, 700);
      else document.dispatchEvent(new Event('dopplergeo:livecomplete'));
    } catch (err) { setTimeout(poll, 2000); }
  }
  poll();

  // Replay: reload the page view with animation on
  const rp = $('lv-replay');
  if (rp) rp.addEventListener('click', () => { location.href = location.pathname + '?replay=1#live'; });
  root.classList.toggle('animated', animate);
})();
