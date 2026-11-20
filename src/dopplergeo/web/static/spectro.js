// Recording spectrogram viewer: image with axes, audio playback with a live playhead, click to seek.
(function () {
  'use strict';
  const ease = t => 1 - Math.pow(1 - Math.min(Math.max(t, 0), 1), 3);
  async function open(name) {
    const r = await fetch(`/recordings/${encodeURIComponent(name)}/spectrogram.json`);
    const m = document.createElement('div');
    m.className = 'specmodal';
    m.innerHTML = `<div class="box" role="dialog" aria-label="Spectrogram of ${name}"><div class="bar"><b>${name}</b>
      <span class="hint">Engine tones of passing props appear as lines sliding down in pitch. Click to jump there.</span>
      <button class="x" aria-label="Close">×</button></div><canvas></canvas><audio controls preload="auto"></audio></div>`;
    document.body.appendChild(m);
    const close = () => { audio.pause(); m.remove(); document.removeEventListener('keydown', esc); };
    const esc = e => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', esc);
    m.addEventListener('click', e => { if (e.target === m) close(); });
    m.querySelector('.x').addEventListener('click', close);
    const cv = m.querySelector('canvas'), g = cv.getContext('2d'), audio = m.querySelector('audio');
    if (!r.ok) { m.querySelector('.hint').textContent = 'Could not render this recording.'; return; }
    const d = await r.json();
    audio.src = d.audio;
    const img = new Image();
    img.src = d.image;
    await img.decode().catch(() => {});
    const dpr = window.devicePixelRatio || 1;
    const fit = () => { const b = cv.getBoundingClientRect(); cv.width = b.width * dpr; cv.height = b.height * dpr; };
    fit(); window.addEventListener('resize', fit);
    const P = {l: 46 * dpr, r: 8 * dpr, t: 8 * dpr, b: 24 * dpr};
    const W = () => cv.width - P.l - P.r, H = () => cv.height - P.t - P.b;
    const tx = t => P.l + (t - d.t0) / (d.t1 - d.t0) * W();
    const t0 = performance.now();
    const draw = () => {
      if (!document.body.contains(m)) return;
      g.fillStyle = '#05060a'; g.fillRect(0, 0, cv.width, cv.height);
      const k = ease((performance.now() - t0) / 900);
      g.save(); g.beginPath(); g.rect(P.l, P.t, W() * k, H()); g.clip(); g.drawImage(img, P.l, P.t, W(), H()); g.restore();
      g.fillStyle = 'rgba(255,255,255,.55)'; g.font = `${11 * dpr}px system-ui`; g.textAlign = 'right';
      for (let f = Math.ceil(d.f0 / 50) * 50; f <= d.f1; f += 50) {
        const y = P.t + (1 - (f - d.f0) / (d.f1 - d.f0)) * H();
        g.fillText(f + ' Hz', P.l - 6 * dpr, y + 4 * dpr);
      }
      g.textAlign = 'center';
      const span = d.t1 - d.t0, st = span > 600 ? 120 : span > 240 ? 60 : span > 90 ? 20 : span > 40 ? 10 : 5;
      for (let t = 0; t <= d.t1; t += st) g.fillText(t + ' s', tx(t), cv.height - 6 * dpr);
      const x = tx(audio.currentTime || 0);  // playhead
      const grd = g.createLinearGradient(x - 50 * dpr, 0, x, 0);
      grd.addColorStop(0, 'rgba(95,211,255,0)'); grd.addColorStop(1, 'rgba(95,211,255,.35)');
      g.fillStyle = grd; g.fillRect(x - 50 * dpr, P.t, 50 * dpr, H());
      g.fillStyle = '#5fd3ff'; g.shadowColor = '#5fd3ff'; g.shadowBlur = 12 * dpr; g.fillRect(x - dpr, P.t, 2 * dpr, H()); g.shadowBlur = 0;
      g.fillStyle = '#fff'; g.textAlign = 'left'; g.fillText((audio.currentTime || 0).toFixed(1) + ' s', Math.min(x + 6 * dpr, cv.width - 50 * dpr), P.t + 14 * dpr);
      requestAnimationFrame(draw);
    };
    requestAnimationFrame(draw);
    cv.addEventListener('click', e => {
      const b = cv.getBoundingClientRect(), px = (e.clientX - b.left) * dpr;
      audio.currentTime = Math.max(0, Math.min(d.t1, d.t0 + (px - P.l) / W() * (d.t1 - d.t0)));
      audio.play().catch(() => {});
    });
  }
  // inline viewer (job pages): same drawing, inside the page
  async function inline(box) {
    const r = await fetch(box.dataset.src);
    if (!r.ok) { box.textContent = 'The recording could not be rendered.'; return; }
    const d = await r.json();
    box.innerHTML = '<canvas></canvas><audio controls preload="none"></audio>';
    const cv = box.querySelector('canvas'), g = cv.getContext('2d'), audio = box.querySelector('audio');
    audio.src = d.audio;
    const img = new Image(); img.src = d.image; await img.decode().catch(() => {});
    const dpr = window.devicePixelRatio || 1;
    const fit = () => { const b = cv.getBoundingClientRect(); cv.width = b.width * dpr; cv.height = b.height * dpr; };
    fit(); window.addEventListener('resize', fit);
    const P = {l: 46 * dpr, r: 8 * dpr, t: 8 * dpr, b: 24 * dpr}, t0 = performance.now();
    const W = () => cv.width - P.l - P.r, H = () => cv.height - P.t - P.b, tx = t => P.l + (t - d.t0) / (d.t1 - d.t0) * W();
    const draw = () => {
      g.fillStyle = '#05060a'; g.fillRect(0, 0, cv.width, cv.height);
      const k = ease((performance.now() - t0) / 1100);
      g.save(); g.beginPath(); g.rect(P.l, P.t, W() * k, H()); g.clip(); g.drawImage(img, P.l, P.t, W(), H()); g.restore();
      g.fillStyle = 'rgba(255,255,255,.55)'; g.font = `${11 * dpr}px system-ui`; g.textAlign = 'right';
      for (let f = Math.ceil(d.f0 / 50) * 50; f <= d.f1; f += 50) g.fillText(f + ' Hz', P.l - 6 * dpr, P.t + (1 - (f - d.f0) / (d.f1 - d.f0)) * H() + 4 * dpr);
      g.textAlign = 'center';
      const span = d.t1 - d.t0, st = span > 600 ? 120 : span > 240 ? 60 : span > 90 ? 20 : span > 40 ? 10 : 5;
      for (let t = 0; t <= d.t1; t += st) g.fillText(t + ' s', tx(t), cv.height - 6 * dpr);
      if (!audio.paused || audio.currentTime > 0) {
        const x = tx(audio.currentTime);
        g.fillStyle = '#5fd3ff'; g.shadowColor = '#5fd3ff'; g.shadowBlur = 12 * dpr; g.fillRect(x - dpr, P.t, 2 * dpr, H()); g.shadowBlur = 0;
      }
      requestAnimationFrame(draw);
    };
    requestAnimationFrame(draw);
    cv.addEventListener('click', e => {
      const b = cv.getBoundingClientRect();
      audio.currentTime = Math.max(0, Math.min(d.t1, d.t0 + ((e.clientX - b.left) * dpr - P.l) / W() * (d.t1 - d.t0)));
      audio.play().catch(() => {});
    });
  }
  document.querySelectorAll('.inlinespec').forEach(inline);
  document.querySelectorAll('.spec-rec').forEach(b => b.addEventListener('click', () => open(b.dataset.name)));
  window.openSpectrogram = open;
})();
