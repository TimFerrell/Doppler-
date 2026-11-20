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
  const S = {spec: null, img: null, reveal: 0, ridge: null, ridgeBorn: 0, flyovers: [], rejected: [], scanning: false,
             expected: null};

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
  const X = t => { const d = window.devicePixelRatio || 1, w = cv.width - (PAD.l + PAD.r) * d;
    return PAD.l * d + (t - S.spec.t0) / (S.spec.t1 - S.spec.t0) * w; };
  const Y = f => { const d = window.devicePixelRatio || 1, h = cv.height - (PAD.t + PAD.b) * d;
    return PAD.t * d + (1 - (f - S.spec.f0) / (S.spec.f1 - S.spec.f0)) * h; };

  function drawSpec() {
    const d = window.devicePixelRatio || 1;
    g.clearRect(0, 0, cv.width, cv.height);
    g.fillStyle = '#05060a'; g.fillRect(0, 0, cv.width, cv.height);
    if (!S.spec || !S.img) {
      requestAnimationFrame(drawSpec);
      return;
    }
    const x0 = PAD.l * d, y0 = PAD.t * d, w = cv.width - (PAD.l + PAD.r) * d, h = cv.height - (PAD.t + PAD.b) * d;
    const rv = animate ? ease((now() - S.reveal) / 1400) : 1;
    g.save(); g.beginPath(); g.rect(x0, y0, w * rv, h); g.clip();
    g.drawImage(S.img, x0, y0, w, h);
    g.restore();
    if (rv < 1) {  // glowing reveal edge
      const ex = x0 + w * rv;
      const grd = g.createLinearGradient(ex - 40 * d, 0, ex + 4 * d, 0);
      grd.addColorStop(0, 'rgba(95,211,255,0)'); grd.addColorStop(1, 'rgba(95,211,255,.9)');
      g.fillStyle = grd; g.fillRect(ex - 40 * d, y0, 44 * d, h);
    }
    // axes
    g.fillStyle = 'rgba(255,255,255,.55)'; g.font = `${11 * d}px system-ui`; g.textAlign = 'right';
    for (let f = Math.ceil(S.spec.f0 / 50) * 50; f <= S.spec.f1; f += 50) {
      g.fillText(f + ' Hz', x0 - 6 * d, Y(f) + 4 * d);
      g.fillStyle = 'rgba(255,255,255,.06)'; g.fillRect(x0, Y(f), w, 1); g.fillStyle = 'rgba(255,255,255,.55)';
    }
    g.textAlign = 'center';
    const span = S.spec.t1 - S.spec.t0, tstep = span > 600 ? 120 : span > 240 ? 60 : span > 90 ? 20 : span > 40 ? 10 : 5;
    for (let t = 0; t <= S.spec.t1; t += tstep) if (t >= S.spec.t0) g.fillText(t + ' s', X(t), cv.height - 8 * d);
    // detection scan beam
    if (S.scanning && animate) {
      const k = ((now() / 1400) % 1);
      const bx = x0 + w * k;
      const grd = g.createLinearGradient(bx - 60 * d, 0, bx, 0);
      grd.addColorStop(0, 'rgba(255,255,255,0)'); grd.addColorStop(1, 'rgba(255,255,255,.22)');
      g.fillStyle = grd; g.fillRect(bx - 60 * d, y0, 60 * d, h);
    }
    // rejected stretches
    for (const r of S.rejected) {
      const a = animate ? ease((now() - r.born) / 500) : 1;
      g.fillStyle = `rgba(255,90,90,${0.10 * a})`;
      g.fillRect(X(r.window_s[0]), y0, X(r.window_s[1]) - X(r.window_s[0]), h);
    }
    // tone points
    if (S.ridge) {
      const n = S.ridge.t.length, k = animate ? ease((now() - S.ridgeBorn) / 1600) : 1;
      g.fillStyle = 'rgba(95,211,255,.85)'; g.shadowColor = 'rgba(95,211,255,.8)'; g.shadowBlur = 6 * d;
      for (let i = 0; i < n * k; i++) { g.beginPath(); g.arc(X(S.ridge.t[i]), Y(S.ridge.f[i]), 1.6 * d, 0, 6.3); g.fill(); }
      g.shadowBlur = 0;
    }
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
        g.fillText(`Plane ${idx + 1} · closest at ${fo.t_cpa.toFixed(1)} s`, Math.min(cx + 12 * d, cv.width - 190 * d), cy - 12 * d);
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
      E.near.forEach((nm, j) => {
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
    requestAnimationFrame(drawSpec);
  }
  requestAnimationFrame(drawSpec);

  // ------------------------------------------------------------------ map + animated heat cells
  const map = USMap.create($('lv-map'));
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
  const heat = new Heat().addTo(map);

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
      if (e.stage === 'flights') S.scanning = false;
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
      S.ridge = e; S.ridgeBorn = now();
      tick(`Strongest tone traced at ${fmtInt(e.t.length)} moments`);
      return 1300;
    },
    rejected(e) {
      e.items.forEach((r, i) => S.rejected.push({...r, born: now() + i * 120}));
      if (e.items.length) tick(`${e.items.length} stretch${e.items.length > 1 ? 'es' : ''} without a clean flyover`, 'warn');
      return 400;
    },
    flyover(e) {
      S.scanning = false;
      S.flyovers.push({...e, born: now()});
      $('lv-speccap').textContent = `${S.flyovers.length} flyover${S.flyovers.length > 1 ? 's' : ''} found`;
      tick(`<b>Plane ${e.i + 1}</b>: ~${Math.round(e.v)} m/s, closest ~${(e.d / 1000).toFixed(1)} km, engine ${e.f0.toFixed(1)} Hz`, 'good');
      return 1500;
    },
    expected(e) {
      S.scanning = false;
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
