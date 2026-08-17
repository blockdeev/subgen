/*
 * i18n — selector de idioma de la INTERFAZ (es/en).
 *
 * Ojo: esto es independiente del <select id="langSelect"> que elige el
 * idioma AL QUE SE TRADUCEN LOS SUBTÍTULOS. Ese select no se toca acá.
 *
 * Las claves `status.*` son las mismas que manda el backend en
 * `message_key` (ver worker/app/pipeline/progress_types.py y tasks.py):
 * el backend nunca manda texto armado en español, manda una clave +
 * parámetros, y acá se resuelve al idioma que el usuario eligió para la UI.
 */
(function () {
  const STORAGE_KEY = 'subgen:lang';

  const DICT = {
    es: {
      'header.tagline': 'Pegá la URL de un video en inglés y elegí qué querés obtener.',
      'lang.toggleLabel': 'EN',
      'theme.toggleLabelToDark': 'Cambiar a tema oscuro',
      'theme.toggleLabelToLight': 'Cambiar a tema claro',

      'card.urlTitle': 'URL del video',
      'url.placeholder': 'https://www.youtube.com/watch?v=...',

      'translate.label': 'Traducir a:',
      'translate.hint': 'Esto traduce los SUBTÍTULOS. El idioma de esta página se cambia con el botón "EN" de arriba.',
      'fps.label': 'Calidad de video:',
      'fps.hint': 'Solo aplica en modo video. 30fps es prácticamente indistinguible de 60 para una charla hablada, y quema los subtítulos bastante más rápido.',

      'actions.srt.label': 'Solo subtítulos',
      'actions.srt.desc': 'Genera el archivo .SRT\nMás rápido',
      'actions.video.label': 'Video con subtítulos',
      'actions.video.desc': 'Video .MP4 con subs quemados\nListo para compartir',

      'pipeline.downloading': 'Descarga',
      'pipeline.transcribing': 'Transcripción',
      'pipeline.translating': 'Traducción',
      'pipeline.burning': 'Quemado',
      'pipeline.completed': 'Listo',

      'status.queued': 'En cola...',
      'status.sending': 'Enviando...',
      'status.downloading': 'Descargando... {percent}%',
      'status.downloading_finished': 'Descarga completa',
      'status.loading_model': 'Cargando modelo de transcripción...',
      'status.transcribing': 'Transcribiendo audio...',
      'status.transcribing_progress': 'Transcribiendo... ({count} segmentos)',
      'status.transcribing_done': 'Transcripción completa: {count} segmentos',
      'status.translating': 'Traduciendo...',
      'status.translating_progress': 'Traduciendo... {done}/{total}',
      'status.translating_done': 'Traducción completa',
      'status.burning_start': 'Quemando subtítulos en el video...',
      'status.burning_progress': 'Quemando subtítulos — {percent}%',
      'status.burning_done': '¡Video generado!',
      'status.error_timeout': 'La tarea tardó demasiado y se canceló',
      'status.error_generic': 'Error: {detail}',
      'status.error_download': 'No se pudo descargar el video: {detail}',
      'status.error_burn': 'Error al quemar los subtítulos: {detail}',
      'status.error_storage': 'Error guardando el resultado: {detail}',
      'error.connection': 'Error de conexión con el servidor',
      'error.url_required': 'Ingresá la URL del video',

      'progress.eta': 'faltan ~{time}',
      'progress.cancel': 'Cancelar',
      'status.cancelled': 'Cancelado',

      'result.segmentsCount': '{count} segmentos',

      'download.srt.label': 'Descargar subtítulos',
      'download.srt.desc': 'Archivo .SRT',
      'download.video.label': 'Descargar video',
      'download.video.desc': 'Con subtítulos · {size} MB',

      'preview.title': 'Vista previa',

      'footer.text': 'SubGen usa <a href="https://github.com/SYSTRAN/faster-whisper" target="_blank">Faster-Whisper</a> para transcripción y Google Translate para traducción.<br>Soporta YouTube, Vimeo, Twitter/X, y <a href="https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md" target="_blank">cientos más</a>.',
    },
    en: {
      'header.tagline': 'Paste the URL of an English video and pick what you want to get.',
      'lang.toggleLabel': 'ES',
      'theme.toggleLabelToDark': 'Switch to dark theme',
      'theme.toggleLabelToLight': 'Switch to light theme',

      'card.urlTitle': 'Video URL',
      'url.placeholder': 'https://www.youtube.com/watch?v=...',

      'translate.label': 'Translate to:',
      'translate.hint': 'This translates the SUBTITLES. This page\'s language is changed with the "ES" button above.',
      'fps.label': 'Video quality:',
      'fps.hint': 'Only applies to video mode. 30fps is nearly indistinguishable from 60 for a spoken talk, and burns subtitles considerably faster.',

      'actions.srt.label': 'Subtitles only',
      'actions.srt.desc': 'Generates the .SRT file\nFaster',
      'actions.video.label': 'Video with subtitles',
      'actions.video.desc': '.MP4 video with burned-in subs\nReady to share',

      'pipeline.downloading': 'Download',
      'pipeline.transcribing': 'Transcription',
      'pipeline.translating': 'Translation',
      'pipeline.burning': 'Burning',
      'pipeline.completed': 'Done',

      'status.queued': 'Queued...',
      'status.sending': 'Sending...',
      'status.downloading': 'Downloading... {percent}%',
      'status.downloading_finished': 'Download complete',
      'status.loading_model': 'Loading transcription model...',
      'status.transcribing': 'Transcribing audio...',
      'status.transcribing_progress': 'Transcribing... ({count} segments)',
      'status.transcribing_done': 'Transcription complete: {count} segments',
      'status.translating': 'Translating...',
      'status.translating_progress': 'Translating... {done}/{total}',
      'status.translating_done': 'Translation complete',
      'status.burning_start': 'Burning subtitles into the video...',
      'status.burning_progress': 'Burning subtitles — {percent}%',
      'status.burning_done': 'Video ready!',
      'status.error_timeout': 'The job took too long and was cancelled',
      'status.error_generic': 'Error: {detail}',
      'status.error_download': 'Could not download the video: {detail}',
      'status.error_burn': 'Error burning subtitles: {detail}',
      'status.error_storage': 'Error saving the result: {detail}',
      'error.connection': 'Connection error with the server',
      'error.url_required': 'Enter the video URL',

      'progress.eta': '~{time} left',
      'progress.cancel': 'Cancel',
      'status.cancelled': 'Cancelled',

      'result.segmentsCount': '{count} segments',

      'download.srt.label': 'Download subtitles',
      'download.srt.desc': '.SRT file',
      'download.video.label': 'Download video',
      'download.video.desc': 'With subtitles · {size} MB',

      'preview.title': 'Preview',

      'footer.text': 'SubGen uses <a href="https://github.com/SYSTRAN/faster-whisper" target="_blank">Faster-Whisper</a> for transcription and Google Translate for translation.<br>Supports YouTube, Vimeo, Twitter/X, and <a href="https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md" target="_blank">hundreds more</a>.',
    },
  };

  function detectDefaultLang() {
    const nav = (navigator.language || 'es').slice(0, 2).toLowerCase();
    return DICT[nav] ? nav : 'es';
  }

  function getLang() {
    return localStorage.getItem(STORAGE_KEY) || detectDefaultLang();
  }

  function setLang(lang) {
    if (!DICT[lang]) return;
    localStorage.setItem(STORAGE_KEY, lang);
    document.documentElement.lang = lang;
    applyStaticTranslations(lang);
    window.dispatchEvent(new CustomEvent('subgen:langchange', { detail: { lang } }));
  }

  // t(key, params) — usada tanto para textos estáticos como para los
  // mensajes de progreso que llegan del backend como {message_key, message_params}.
  function t(key, params) {
    const lang = getLang();
    let template = (DICT[lang] && DICT[lang][key]) || DICT.es[key] || key;
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        template = template.replaceAll(`{${k}}`, String(v));
      }
    }
    return template;
  }

  function applyStaticTranslations(lang) {
    document.querySelectorAll('[data-i18n]').forEach((el) => {
      const key = el.getAttribute('data-i18n');
      const html = t(key);
      if (el.hasAttribute('data-i18n-html')) el.innerHTML = html;
      else el.textContent = html;
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
      el.setAttribute('placeholder', t(el.getAttribute('data-i18n-placeholder')));
    });
    const toggleBtn = document.getElementById('langToggleBtn');
    if (toggleBtn) toggleBtn.textContent = t('lang.toggleLabel');
  }

  function initLangToggle() {
    setLang(getLang());
    const btn = document.getElementById('langToggleBtn');
    if (btn) {
      btn.addEventListener('click', () => {
        setLang(getLang() === 'es' ? 'en' : 'es');
      });
    }
  }

  window.SubGenI18n = { t, getLang, setLang, initLangToggle };
})();
