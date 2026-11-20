// Tabs, form helpers and job polling.
(function () {
  'use strict';
  const showTab = name => {
    document.querySelectorAll('.tabs button').forEach(x => x.classList.toggle('on', x.dataset.tab === name));
    document.querySelectorAll('.panel').forEach(p => p.classList.toggle('on', p.getAttribute('data-panel') === name));
    document.dispatchEvent(new Event('dopplergeo:tab'));
  };
  document.querySelectorAll('.tabs button').forEach(b => b.addEventListener('click', () => {
    showTab(b.dataset.tab);
    try { history.replaceState(null, '', '?tab=' + b.dataset.tab); } catch (e) { /* ignore */ }
  }));

  // step shortcuts at the top of the page
  document.querySelectorAll('.steplink[data-goto]').forEach(b => b.addEventListener('click', () => {
    showTab(b.getAttribute('data-goto'));
    document.querySelector('.tabs').scrollIntoView({behavior: 'smooth'});
  }));

  // library pickers hide the matching upload field
  document.querySelectorAll('select[data-toggles]').forEach(sel => {
    const sync = () => {
      const form = sel.closest('form');
      form.querySelectorAll('.' + sel.dataset.toggles).forEach(el => { el.hidden = !!sel.value; });
      if (sel.name === 'recording') {
        const opt = sel.selectedOptions[0], st = form.querySelector('[name="start_utc"]');
        if (st && opt && opt.dataset.start) st.placeholder = new Date(parseFloat(opt.dataset.start) * 1000).toISOString();
      }
    };
    sel.addEventListener('change', sync); sync();
  });

  // does the chosen flight data cover the chosen recording's time and the search area?
  const lf = document.getElementById('locate-form');
  if (lf) {
    const msg = document.getElementById('cover-msg');
    const fmt = t => new Date(t * 1000).toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
    const check = () => {
      const r = lf.querySelector('select[name=recording]').selectedOptions[0];
      const d = lf.querySelector('select[name=dataset]').selectedOptions[0];
      const typed = lf.querySelector('[name=start_utc]').value.trim();
      let start = r && r.dataset.start ? parseFloat(r.dataset.start) : null;
      if (typed) { const t = Date.parse(typed); if (!isNaN(t)) start = t / 1000; }
      const dur = r && r.dataset.dur ? parseFloat(r.dataset.dur) : 0;
      const out = [];
      let level = 'ok';
      if (d && d.value) {
        const t0 = parseFloat(d.dataset.tmin), t1 = parseFloat(d.dataset.tmax);
        if (start !== null) {
          if (start + dur < t0 || start > t1) {
            level = 'bad';
            out.push(`The recording (${fmt(start)}) is outside this flight data (${fmt(t0)} → ${fmt(t1)}). ` +
                     'Get flight data for the day of the recording in step 1.');
          } else out.push(`✓ The flight data covers the recording's time (${fmt(start)}).`);
        } else if (r && r.value) {
          level = 'warn';
          out.push('This recording has no start time, so every flight in the data will be tried. Enter the start time below if you know it.');
        }
        const bb = (d.dataset.bbox || '').split(',').map(parseFloat);
        const la = parseFloat(lf.querySelector('[name=center_lat]').value), lo = parseFloat(lf.querySelector('[name=center_lon]').value);
        if (bb.length === 4 && isFinite(la) && isFinite(lo)) {
          if (la < bb[0] || la > bb[2] || lo < bb[1] || lo > bb[3]) {
            level = 'bad';
            out.push('The search area is outside the area this flight data covers.');
          } else out.push('✓ The search area is inside the flight data.');
        }
      }
      msg.hidden = !out.length;
      msg.className = 'check-cover ' + level;
      msg.innerHTML = out.map(t => `<div class="${t.startsWith('✓') ? 'good' : ''}">${t}</div>`).join('');
    };
    // choosing a recording auto-selects flight data that covers its time (the most recent such dataset)
    const recSel = lf.querySelector('select[name=recording]'), dsSel = lf.querySelector('select[name=dataset]');
    const autoPick = () => {
      const r = recSel.selectedOptions[0];
      if (!r || !r.dataset.start) return;
      const t = parseFloat(r.dataset.start), dur = parseFloat(r.dataset.dur || '0');
      const fits = [...dsSel.options].filter(o => o.value && parseFloat(o.dataset.tmin) <= t + dur && parseFloat(o.dataset.tmax) >= t);
      if (fits.length) { dsSel.value = fits[0].value; dsSel.dispatchEvent(new Event('change')); }
    };
    recSel.addEventListener('change', autoPick);
    if (!dsSel.value) {  // a single dataset needs no choosing
      const real = [...dsSel.options].filter(o => o.value);
      if (real.length === 1) { dsSel.value = real[0].value; dsSel.dispatchEvent(new Event('change')); }
    }
    autoPick();
    lf.addEventListener('change', check); lf.addEventListener('input', check); check();
  }

  // "Use in Locate" from the recordings list
  document.querySelectorAll('.use-rec').forEach(b => b.addEventListener('click', () => {
    const sel = document.querySelector('form[data-panel=locate] select[name=recording]');
    sel.value = b.dataset.name; sel.dispatchEvent(new Event('change'));
    showTab('locate'); window.scrollTo({top: 0, behavior: 'smooth'});
  }));

  // collector source switches between URL and point fields
  const src = document.getElementById('col-source');
  if (src) {
    const sync = () => {
      const pt = src.value !== 'receiver';
      document.querySelectorAll('.col-point').forEach(e => { e.hidden = !pt; });
      document.querySelectorAll('.col-receiver').forEach(e => { e.hidden = pt; });
      const iv = src.closest('form').querySelector('[name=interval_s]');
      if (iv) iv.value = src.value === 'adsb.lol' ? '5' : '1';
    };
    src.addEventListener('change', sync); sync();
  }

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
        const pr = s.progress;
        if (pr) {
          const now = document.getElementById('now'); now.textContent = pr.now || ''; now.hidden = !pr.now;
          const bar = document.getElementById('pbar');
          bar.hidden = pr.percent === null || s.state === 'done';
          if (pr.percent !== null) bar.firstElementChild.style.width = pr.percent + '%';
          document.getElementById('progress').innerHTML = pr.steps.map(x => {
            const det = x.done ? x.detail : (x.active ? x.active_detail : null);
            return `<li class="${x.done ? 'done' : (x.active ? 'active' : '')}"><span class="label">${x.label}</span>` +
                   (det ? `<span class="detail">${det}</span>` : '') + '</li>';
          }).join('');
        }
        if (s.error) { const e = document.getElementById('error'); e.textContent = s.error; e.hidden = false; }
        if ((state === 'queued' || state === 'running') && (s.state === 'done' || s.state === 'failed')) { location.reload(); return; }
        state = s.state;
        if (s.state === 'queued' || s.state === 'running') setTimeout(poll, 1500);
      } catch (e) { setTimeout(poll, 4000); }
    };
    poll();
  }
})();
