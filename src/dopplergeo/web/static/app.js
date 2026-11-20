// Tabs on the index page
document.querySelectorAll('.tabs button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.tabs button').forEach(x => x.classList.toggle('on', x === b));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('on', p.dataset.panel === b.dataset.tab));
}));

// Job page: poll status + log until the job finishes, then reload to show results
const jobEl = document.getElementById('job');
if (jobEl) {
  const id = jobEl.dataset.job, logEl = document.getElementById('log');
  let state = jobEl.dataset.state;
  const poll = async () => {
    try {
      const r = await fetch(`/jobs/${id}/status.json`, {cache: 'no-store'});
      const s = await r.json();
      const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 20;
      logEl.textContent = s.log || '(no output yet)';
      if (atBottom) logEl.scrollTop = logEl.scrollHeight;
      const st = document.getElementById('state');
      st.textContent = s.state; st.className = 'state ' + s.state;
      if (s.error) { const e = document.getElementById('error'); e.textContent = s.error; e.hidden = false; }
      if ((state === 'queued' || state === 'running') && (s.state === 'done' || s.state === 'failed')) { location.reload(); return; }
      state = s.state;
      if (s.state === 'queued' || s.state === 'running') setTimeout(poll, 1500);
    } catch (e) { setTimeout(poll, 4000); }
  };
  poll();
}

// Candidate maps: local equirectangular projection, no tiles (nothing leaves this machine)
const NS = 'http://www.w3.org/2000/svg';
const ramp = t => {
  const stops = [[59, 76, 192], [90, 180, 172], [246, 224, 94], [232, 89, 12]];
  const x = Math.min(Math.max(t, 0), 1) * (stops.length - 1), i = Math.min(Math.floor(x), stops.length - 2), f = x - i;
  const c = stops[i].map((v, k) => Math.round(v + (stops[i + 1][k] - v) * f));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
};
const el = (name, attrs, parent) => { const e = document.createElementNS(NS, name);
  for (const k in attrs) e.setAttribute(k, attrs[k]); parent.appendChild(e); return e; };

document.querySelectorAll('svg.map').forEach(async svg => {
  const r = await fetch(svg.dataset.src); if (!r.ok) { svg.remove(); return; }
  const d = await r.json(), P = d.points, n = P.lat.length; if (!n) return;
  const lat0 = P.lat[0], lon0 = P.lon[0], k = Math.cos(lat0 * Math.PI / 180) * 111320, m = 110540;
  const xs = P.lon.map(v => (v - lon0) * k), ys = P.lat.map(v => (v - lat0) * m);
  let pts = xs.map((x, i) => [x, ys[i]]);
  if (d.truth) pts.push([(d.truth[1] - lon0) * k, (d.truth[0] - lat0) * m]);
  const minx = Math.min(...pts.map(p => p[0])), maxx = Math.max(...pts.map(p => p[0]));
  const miny = Math.min(...pts.map(p => p[1])), maxy = Math.max(...pts.map(p => p[1]));
  const W = 800, H = 500, pad = 24, span = Math.max(maxx - minx, (maxy - miny) * W / H, 200);
  const s = (W - 2 * pad) / span, cx = (minx + maxx) / 2, cy = (miny + maxy) / 2;
  const X = x => W / 2 + (x - cx) * s, Y = y => H / 2 - (y - cy) * s;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const smin = Math.min(...P.score), smax = Math.max(...P.score);
  const g = el('g', {}, svg);
  const idx = [...Array(n).keys()].sort((a, b) => P.score[a] - P.score[b]);
  const sz = Math.max(2.5, Math.min(9, 100 * s * 0.9));
  for (const i of idx) {
    const t = (P.score[i] - smin) / ((smax - smin) || 1);
    const c = el('rect', {x: X(xs[i]) - sz / 2, y: Y(ys[i]) - sz / 2, width: sz, height: sz, fill: ramp(t)}, g);
    el('title', {}, c).textContent = `#${P.rank[i]} ${P.lat[i].toFixed(6)}, ${P.lon[i].toFixed(6)}  score ${P.score[i].toFixed(0)}  ${P.combo[i]}`;
  }
  el('circle', {cx: X(xs[0]), cy: Y(ys[0]), r: 11, fill: 'none', stroke: 'var(--danger)', 'stroke-width': 2.5}, svg);
  if (d.truth) {
    const tx = X((d.truth[1] - lon0) * k), ty = Y((d.truth[0] - lat0) * m);
    el('text', {x: tx, y: ty + 6, 'text-anchor': 'middle', style: 'font-size:18px;fill:var(--text)'}, svg).textContent = '★';
  }
  // scale bar
  const nice = [100, 200, 500, 1000, 2000, 5000, 10000, 20000].find(v => v * s > 80) || 50000;
  el('line', {x1: pad, y1: H - pad, x2: pad + nice * s, y2: H - pad, stroke: 'var(--muted)', 'stroke-width': 2}, svg);
  el('text', {x: pad, y: H - pad - 6}, svg).textContent = nice >= 1000 ? `${nice / 1000} km` : `${nice} m`;
});
