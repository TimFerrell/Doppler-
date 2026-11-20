// Browser microphone recorder: raw PCM via AudioWorklet -> 16-bit WAV with a UTC start time.
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const root = $('recorder');
  if (!root) return;
  const startBtn = $('rec-start'), stopBtn = $('rec-stop'), msg = $('rec-msg'), timeEl = $('rec-time');
  const levelEl = $('rec-level'), devSel = $('rec-device'), canvas = $('rec-spec'), ctx2d = canvas.getContext('2d');
  let ac = null, stream = null, node = null, src = null, analyser = null, chunks = [], nSamples = 0, t0 = null, raf = null;

  const monBtn = $('rec-monitor'), toneEl = $('rec-tone');
  let saving = true;
  if (!window.isSecureContext || !navigator.mediaDevices || !window.AudioContext) {
    const box = $('rec-insecure'), port = box.dataset.httpsPort;
    const url = port ? `https://${location.hostname}:${port}${location.pathname}?tab=record` : null;
    box.innerHTML = '<b>The microphone is blocked on this page.</b> Browsers only allow it on secure (https) pages or on the ' +
      'computer running the server. ' + (url ? `Open <a href="${url}">${url}</a> instead: your browser will warn about the ` +
      'certificate once (it is this server\'s own self-signed one); choose to continue, and recording will work.'
      : 'Ask for the server to be started with https enabled.') + ' Importing a phone video below works without this.';
    box.hidden = false;
    startBtn.disabled = true; monBtn.disabled = true;
    return;
  }

  async function listDevices() {
    try {
      const devs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audioinput');
      const cur = devSel.value;
      devSel.innerHTML = '<option value="">default microphone</option>' + devs.map((d, i) =>
        `<option value="${d.deviceId}">${(d.label || 'microphone ' + (i + 1)).replace(/</g, '&lt;')}</option>`).join('');
      devSel.value = cur;
    } catch (e) { /* ignore */ }
  }
  listDevices();

  const fmt = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

  async function start(save = true) {
    saving = save;
    msg.textContent = '';
    const constraints = {audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1}};
    if (devSel.value) constraints.audio.deviceId = {exact: devSel.value};
    try {
      stream = await navigator.mediaDevices.getUserMedia(constraints);
    } catch (e) {
      msg.textContent = 'Microphone not available: ' + e.message; return;
    }
    listDevices();
    // Ask for 16 kHz (the pipeline's rate); browsers that refuse to mix rates fall back to the device rate.
    try {
      ac = new AudioContext({sampleRate: 16000});
      src = ac.createMediaStreamSource(stream);
    } catch (e) {
      if (ac) ac.close();
      ac = new AudioContext();
      src = ac.createMediaStreamSource(stream);
    }
    await ac.audioWorklet.addModule('/static/recorder-worklet.js');
    node = new AudioWorkletNode(ac, 'dopplergeo-capture');
    analyser = ac.createAnalyser();
    analyser.fftSize = ac.sampleRate <= 16000 ? 16384 : 32768;
    analyser.smoothingTimeConstant = 0;
    analyser.minDecibels = -120; analyser.maxDecibels = -30;
    src.connect(node); src.connect(analyser);
    // keep the graph pulled without making sound
    const mute = ac.createGain(); mute.gain.value = 0; node.connect(mute).connect(ac.destination);
    chunks = []; nSamples = 0; t0 = null;
    node.port.onmessage = e => {
      const f = e.data;
      if (t0 === null) t0 = Date.now() / 1000 - f.length / ac.sampleRate;  // clock time of the first sample
      const s = new Int16Array(f.length);
      let sum = 0;
      for (let i = 0; i < f.length; i++) { const v = Math.max(-1, Math.min(1, f[i])); s[i] = v * 32767; sum += v * v; }
      if (saving) chunks.push(s);
      nSamples += f.length;
      const rms = Math.sqrt(sum / f.length), db = 20 * Math.log10(rms + 1e-9);
      levelEl.style.width = Math.max(0, Math.min(100, (db + 70) * 100 / 70)) + '%';
      levelEl.classList.toggle('clip', rms > 0.7);
      timeEl.textContent = fmt(nSamples / ac.sampleRate);
    };
    ctx2d.fillStyle = '#000'; ctx2d.fillRect(0, 0, canvas.width, canvas.height);
    draw();
    startBtn.disabled = true; monBtn.disabled = true; stopBtn.disabled = false; devSel.disabled = true;
    stopBtn.textContent = saving ? '■ Stop & save' : '■ Stop';
    root.classList.add(saving ? 'recording' : 'monitoring');
    msg.textContent = saving ? `Recording at ${ac.sampleRate} Hz…` : 'Live view: listening, nothing is saved.';
  }

  // scrolling spectrogram, 0-500 Hz: 1 px per 100 ms (~90 s across), each column normalised to its
  // own median so engine lines stand out regardless of overall level
  let fb = null, last = 0;
  function draw(ts) {
    raf = requestAnimationFrame(draw);
    if (!analyser || (ts && ts - last < 100)) return;
    last = ts || 0;
    fb = fb || new Float32Array(analyser.frequencyBinCount);
    analyser.getFloatFrequencyData(fb);
    const W = canvas.width, H = canvas.height, hzPerBin = ac.sampleRate / analyser.fftSize;
    const lo = Math.ceil(20 / hzPerBin), top = Math.floor(500 / hzPerBin);
    const col = Array.from(fb.subarray(lo, top)).sort((a, b) => a - b);
    const med = col[col.length >> 1];
    // strongest tone in the engine band (60-300 Hz), shown when it stands well above the noise
    let kmax = Math.ceil(60 / hzPerBin), vmax = -1e9;
    for (let k = kmax; k <= Math.floor(300 / hzPerBin); k++) if (fb[k] > vmax) { vmax = fb[k]; kmax = k; }
    toneEl.textContent = vmax - med > 12 ? `strongest tone ${(kmax * hzPerBin).toFixed(0)} Hz (${(vmax - med).toFixed(0)} dB above noise)` : 'no clear tone';
    ctx2d.drawImage(canvas, -1, 0);
    for (let y = 0; y < H; y++) {
      const k = Math.floor((1 - y / H) * top);
      const v = Math.max(0, Math.min(1, (fb[k] - med) / 30));
      const c = Math.floor(255 * Math.pow(v, 0.8));
      ctx2d.fillStyle = `rgb(${c},${Math.floor(c * 0.8)},${Math.floor(40 + c * 0.4)})`;
      ctx2d.fillRect(W - 1, y, 1, 1);
    }
  }

  function wav(int16chunks, n, sr) {
    const buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
    const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    str(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, sr, true);
    v.setUint32(28, sr * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, 'data'); v.setUint32(40, n * 2, true);
    let o = 44;
    for (const c of int16chunks) { new Int16Array(buf, o, c.length).set(c); o += c.length * 2; }
    return new Blob([buf], {type: 'audio/wav'});
  }

  let pending = null;  // a finished recording whose upload failed: retried on the next click

  async function upload() {
    msg.textContent = `Saving ${fmt(pending.seconds)}…`;
    try {
      const r = await fetch('/recordings', {method: 'POST', body: pending.form});
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.status);
      pending = null; chunks = [];
      location.href = '/?tab=record';
    } catch (e) {
      msg.textContent = 'Upload failed: ' + e.message + ' — the recording is still in this tab; click Save again.';
      stopBtn.disabled = false;
    }
  }

  async function stop() {
    stopBtn.disabled = true;
    if (pending) return upload();
    cancelAnimationFrame(raf);
    if (!saving) {
      stream.getTracks().forEach(t => t.stop());
      await ac.close();
      root.classList.remove('monitoring');
      startBtn.disabled = false; monBtn.disabled = false; devSel.disabled = false;
      stopBtn.textContent = '■ Stop & save'; toneEl.textContent = ''; levelEl.style.width = '0';
      msg.textContent = 'Live view stopped.';
      return;
    }
    const sr = ac.sampleRate, label = $('rec-label').value.trim();
    const dev = stream.getAudioTracks()[0];
    const devLabel = dev ? dev.label : '';
    stream.getTracks().forEach(t => t.stop());
    await ac.close();
    root.classList.remove('recording');
    if (!nSamples) { msg.textContent = 'Nothing was captured.'; startBtn.disabled = false; devSel.disabled = false; return; }
    const fd = new FormData();
    fd.append('audio', wav(chunks, nSamples, sr), 'recording.wav');
    fd.append('start_utc', String(t0));
    fd.append('label', label);
    fd.append('device', devLabel);
    pending = {form: fd, seconds: nSamples / sr};
    return upload();
  }

  startBtn.addEventListener('click', () => start(true));
  monBtn.addEventListener('click', () => start(false));
  stopBtn.addEventListener('click', stop);
  window.addEventListener('beforeunload', e => { if (root.classList.contains('recording') || pending) { e.preventDefault(); e.returnValue = ''; } });
})();
