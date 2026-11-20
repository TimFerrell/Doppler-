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
        if (s.error) { const e = document.getElementById('error'); e.textContent = s.error; e.hidden = false; }
        if ((state === 'queued' || state === 'running') && (s.state === 'done' || s.state === 'failed')) { location.reload(); return; }
        state = s.state;
        if (s.state === 'queued' || s.state === 'running') setTimeout(poll, 1500);
      } catch (e) { setTimeout(poll, 4000); }
    };
    poll();
  }
})();
