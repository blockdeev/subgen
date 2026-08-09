(function () {
  const { t } = window.SubGenI18n;
  const $ = (id) => document.getElementById(id);

  let currentJobId = null;
  let currentMode = 'srt';
  let ws = null;
  let pollInterval = null;
  let lastPayload = null; // para re-renderizar si el usuario cambia el idioma de la UI a mitad de un job

  function apiBase() {
    return ''; // la API sirve el frontend desde el mismo origen
  }

  function wsUrl(jobId) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    return `${proto}://${location.host}/ws/jobs/${jobId}`;
  }

  function showError(message) {
    const el = $('errorMsg');
    el.textContent = message;
    el.classList.add('active');
    setTimeout(() => el.classList.remove('active'), 8000);
  }

  function fmtEta(seconds) {
    if (seconds == null || seconds < 0 || !isFinite(seconds)) return '';
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    const time = m > 0 ? `${m}m ${s}s` : `${s}s`;
    return t('progress.eta', { time });
  }

  const PIPELINE_STEPS = {
    video: ['downloading', 'transcribing', 'translating', 'burning', 'completed'],
    srt: ['downloading', 'transcribing', 'translating', 'completed'],
  };
  const STEP_ICONS = { downloading: '📥', transcribing: '🎙️', translating: '🌐', burning: '🔥', completed: '✅' };

  function buildPipeline(mode) {
    const steps = PIPELINE_STEPS[mode];
    const gridClass = steps.length === 5 ? 'pipeline-5' : 'pipeline-4';
    $('pipelineContainer').innerHTML = `<div class="pipeline ${gridClass}">${steps
      .map(
        (s) => `<div class="pipeline-step" data-step="${s}">
            <span class="pipeline-icon">${STEP_ICONS[s]}</span>
            <span class="pipeline-label">${t('pipeline.' + s)}</span>
          </div>`
      )
      .join('')}</div>`;
  }

  function updatePipeline(status) {
    const steps = PIPELINE_STEPS[currentMode];
    const idx = steps.indexOf(status);
    document.querySelectorAll('.pipeline-step').forEach((el) => {
      const si = steps.indexOf(el.dataset.step);
      el.classList.remove('active', 'done');
      if (idx === -1) return;
      if (si < idx) el.classList.add('done');
      else if (si === idx) el.classList.add('active');
    });
  }

  function renderPayload(payload) {
    lastPayload = payload;
    const progress = payload.progress != null ? payload.progress : 0;
    $('progressFill').style.width = progress + '%';
    $('progressPct').textContent = Math.round(progress) + '%';

    const message = payload.message_key
      ? t(payload.message_key, payload.message_params || {})
      : t('status.' + payload.status) || payload.status;
    $('progressMsg').textContent = message;
    $('progressEta').textContent = fmtEta(payload.eta_seconds);

    updatePipeline(payload.status);

    if (payload.status === 'completed') {
      stopUpdates();
      showResult(payload.result);
      resetBtns();
    } else if (payload.status === 'error') {
      stopUpdates();
      showError(message);
      $('progressArea').classList.remove('active');
      resetBtns();
    }
  }

  function connectWebSocket(jobId) {
    try {
      ws = new WebSocket(wsUrl(jobId));
    } catch (err) {
      startPolling(jobId);
      return;
    }

    let gotFirstMessage = false;
    const connectTimeout = setTimeout(() => {
      if (!gotFirstMessage) {
        ws && ws.close();
        startPolling(jobId);
      }
    }, 3000);

    ws.addEventListener('message', (event) => {
      gotFirstMessage = true;
      clearTimeout(connectTimeout);
      try {
        renderPayload(JSON.parse(event.data));
      } catch (e) { /* ignora mensajes malformados */ }
    });

    ws.addEventListener('close', () => {
      clearTimeout(connectTimeout);
      // Si el job todavía no terminó y el socket se cayó, caemos a polling.
      if (currentJobId === jobId && !pollInterval && lastPayload && !['completed', 'error'].includes(lastPayload.status)) {
        startPolling(jobId);
      }
    });

    ws.addEventListener('error', () => {
      clearTimeout(connectTimeout);
      if (!gotFirstMessage) startPolling(jobId);
    });
  }

  function startPolling(jobId) {
    if (pollInterval) return;
    pollInterval = setInterval(async () => {
      try {
        const resp = await fetch(`${apiBase()}/api/jobs/${jobId}`);
        const data = await resp.json();
        renderPayload(data);
      } catch (err) { /* reintenta en el próximo tick */ }
    }, 1500);
  }

  function stopUpdates() {
    if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
    if (ws) { ws.close(); ws = null; }
  }

  async function startProcess(mode) {
    const url = $('urlInput').value.trim();
    const lang = $('langSelect').value;
    if (!url) { showError(t('error.url_required')); $('urlInput').focus(); return; }

    currentMode = mode;
    lastPayload = null;

    document.querySelectorAll('.action-btn').forEach((b) => (b.disabled = true));

    buildPipeline(mode);
    $('progressArea').classList.add('active');
    $('resultArea').classList.remove('active');
    $('errorMsg').classList.remove('active');
    $('progressFill').style.width = '0%';
    $('progressMsg').textContent = t('status.sending');
    $('progressPct').textContent = '0%';
    $('progressEta').textContent = '';

    try {
      const resp = await fetch(`${apiBase()}/api/jobs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, target_lang: lang, mode }),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        showError(data.error || t('error.connection'));
        resetBtns();
        return;
      }
      const data = await resp.json();
      currentJobId = data.job_id;
      connectWebSocket(currentJobId);
    } catch (err) {
      showError(t('error.connection'));
      resetBtns();
    }
  }

  async function triggerDownload(kind) {
    try {
      const resp = await fetch(`${apiBase()}/api/jobs/${currentJobId}/download/${kind}`);
      if (!resp.ok) { showError(t('error.connection')); return; }
      const data = await resp.json();
      window.location.href = data.url;
    } catch (err) {
      showError(t('error.connection'));
    }
  }

  function showResult(r) {
    if (!r) return;
    $('resultArea').classList.add('active');
    const dur = r.duration ? `${Math.floor(r.duration / 60)}m ${Math.round(r.duration % 60)}s` : '';
    $('resultTitle').textContent = r.title;
    $('resultMeta').textContent = t('result.segmentsCount', { count: r.segments_count }) + (dur ? ` · ${dur}` : '');

    let dl = '';
    if (r.mode === 'video' && r.video_filename) {
      dl = `<div class="dl-row">
        <button class="dl-btn dl-btn-srt" data-download="srt">
          <span class="dl-icon">📄</span><span class="dl-label">${t('download.srt.label')}</span><span class="dl-desc">${t('download.srt.desc')}</span>
        </button>
        <button class="dl-btn dl-btn-video" data-download="video">
          <span class="dl-icon">🎬</span><span class="dl-label">${t('download.video.label')}</span><span class="dl-desc">${t('download.video.desc', { size: r.video_size_mb })}</span>
        </button>
      </div>`;
    } else {
      dl = `<div class="dl-row single">
        <button class="dl-btn dl-btn-srt" data-download="srt">
          <span class="dl-icon">📄</span><span class="dl-label">${t('download.srt.label')}</span><span class="dl-desc">${t('download.srt.desc')}</span>
        </button>
      </div>`;
    }
    $('downloadRow').innerHTML = dl;

    if (r.preview_segments && r.preview_segments.length) {
      let html = '';
      r.preview_segments.forEach((seg, i) => {
        const s = Math.floor(seg.start / 60) + ':' + String(Math.floor(seg.start % 60)).padStart(2, '0');
        const e = Math.floor(seg.end / 60) + ':' + String(Math.floor(seg.end % 60)).padStart(2, '0');
        html += `<div class="sub-line"><span class="sub-index">${i + 1}</span> <span class="sub-time">${s} → ${e}</span><br><span class="sub-text">${seg.text}</span><br>${seg.text_original ? `<span class="sub-original">${seg.text_original}</span>` : ''}</div>`;
      });
      $('previewBox').innerHTML = html;
    } else {
      $('previewBox').innerHTML = '';
    }

    $('resultArea').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function resetBtns() {
    document.querySelectorAll('.action-btn').forEach((b) => (b.disabled = false));
  }

  document.querySelectorAll('.action-btn').forEach((btn) => {
    btn.addEventListener('click', () => startProcess(btn.dataset.mode));
  });

  $('downloadRow').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-download]');
    if (btn) triggerDownload(btn.dataset.download);
  });

  $('urlInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') startProcess('srt'); });

  window.addEventListener('subgen:langchange', () => {
    if (lastPayload) renderPayload(lastPayload);
  });

  window.SubGenI18n.initLangToggle();
  window.SubGenTheme.initThemeToggle();
})();
