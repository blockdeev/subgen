/*
 * theme.js — selector claro/oscuro.
 *
 * El tema INICIAL se aplica con un script inline en el <head> de
 * index.html (antes de este archivo, antes del primer render) para que no
 * haya flash blanco al cargar. Este archivo solo maneja el botón de
 * toggle y la persistencia; reutiliza la misma lógica de detección que el
 * inline script para no duplicar la fuente de verdad del criterio.
 */
(function () {
  const STORAGE_KEY = 'subgen:theme';

  function systemPrefersDark() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  }

  function getStoredTheme() {
    return localStorage.getItem(STORAGE_KEY); // 'dark' | 'light' | null
  }

  function getEffectiveTheme() {
    return getStoredTheme() || (systemPrefersDark() ? 'dark' : 'light');
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const btn = document.getElementById('themeToggleBtn');
    if (btn && window.SubGenI18n) {
      const key = theme === 'dark' ? 'theme.toggleLabelToLight' : 'theme.toggleLabelToDark';
      const glyph = btn.querySelector('.icon-btn-glyph');
      const label = btn.querySelector('.icon-btn-label');
      if (glyph) glyph.textContent = theme === 'dark' ? '🌙' : '☀️';
      if (label) label.textContent = window.SubGenI18n.t(key);
    }
  }

  function setTheme(theme) {
    localStorage.setItem(STORAGE_KEY, theme);
    applyTheme(theme);
  }

  function initThemeToggle() {
    applyTheme(getEffectiveTheme()); // sincroniza labels/emoji con lo que ya aplicó el inline script

    const btn = document.getElementById('themeToggleBtn');
    if (btn) {
      btn.addEventListener('click', () => {
        const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
        setTheme(next);
      });
    }

    // Si el usuario nunca eligió explícitamente, seguimos el sistema en vivo.
    if (!getStoredTheme() && window.matchMedia) {
      window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
        if (!getStoredTheme()) applyTheme(e.matches ? 'dark' : 'light');
      });
    }

    window.addEventListener('subgen:langchange', () => applyTheme(document.documentElement.dataset.theme));
  }

  window.SubGenTheme = { initThemeToggle, getEffectiveTheme };
})();
