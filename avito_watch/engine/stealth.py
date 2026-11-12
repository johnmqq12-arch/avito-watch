"""Stealth-патчи страницы. Задача — не «обмануть всех», а не выделяться
среди обычных браузеров очевидными признаками автоматизации.
"""
from __future__ import annotations

STEALTH_JS = r"""
(() => {
  const patch = (obj, prop, value) => {
    try { Object.defineProperty(obj, prop, { get: () => value, configurable: true }); } catch (e) {}
  };

  // navigator.webdriver — главный маркер Playwright/Selenium
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined, configurable: true });
  } catch (e) {}
  patch(navigator, 'webdriver', undefined);

  patch(navigator, 'languages', ['ru-RU', 'ru', 'en-US', 'en']);
  patch(navigator, 'hardwareConcurrency', 8);
  patch(navigator, 'deviceMemory', 8);
  patch(navigator, 'maxTouchPoints', 0);

  // plugins / mimeTypes — в headless они пустые
  const mkPlugin = (name, filename, description) => {
    const p = { name, filename, description, length: 1 };
    p[0] = { type: 'application/pdf', suffixes: 'pdf', description };
    return p;
  };
  const plugins = [
    mkPlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    mkPlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    mkPlugin('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
  ];
  plugins.item = i => plugins[i];
  plugins.namedItem = n => plugins.find(p => p.name === n);
  plugins.refresh = () => {};
  patch(navigator, 'plugins', plugins);
  patch(navigator, 'mimeTypes', Object.assign([
    { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
  ], { item: () => null, namedItem: () => null }));

  // window.chrome
  if (!window.chrome) {
    window.chrome = { runtime: {}, loadTimes: () => ({}), csi: () => ({}), app: { isInstalled: false } };
  }

  // permissions.query не должен противоречить Notification.permission
  if (navigator.permissions && navigator.permissions.query) {
    const orig = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (params) =>
      (params && params.name === 'notifications')
        ? Promise.resolve({ state: Notification.permission })
        : orig(params);
  }

  // WebGL vendor/renderer — типичный headless выдаёт SwiftShader
  const spoofGL = (proto) => {
    if (!proto || !proto.getParameter) return;
    const orig = proto.getParameter;
    proto.getParameter = function (p) {
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel Iris OpenGL Engine';
      return orig.apply(this, arguments);
    };
  };
  try { spoofGL(WebGLRenderingContext.prototype); } catch (e) {}
  try { spoofGL(WebGL2RenderingContext.prototype); } catch (e) {}

  // размеры экрана/окна не должны быть нулевыми
  try {
    patch(screen, 'availWidth', screen.width);
    patch(screen, 'availHeight', screen.height - 40);
    patch(window, 'outerWidth', window.innerWidth);
    patch(window, 'outerHeight', window.innerHeight + 85);
  } catch (e) {}

  // iframe.contentWindow.chrome
  try {
    const desc = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
      get: function () {
        const w = desc.get.call(this);
        try { if (w && !w.chrome) w.chrome = window.chrome; } catch (e) {}
        return w;
      },
    });
  } catch (e) {}

  // не палим сами себя в стеках и toString
  try {
    const nativeToString = Function.prototype.toString;
    const cache = new WeakMap();
    Function.prototype.toString = function () {
      if (cache.has(this)) return cache.get(this);
      return nativeToString.call(this);
    };
    cache.set(Function.prototype.toString, 'function toString() { [native code] }');
  } catch (e) {}
})();
"""

HUMANIZE_JS = r"""
(() => {
  // небольшая естественная активность: без неё страница выглядит «мёртвой»
  try {
    window.scrollBy({ top: Math.round(120 + Math.random() * 400), behavior: 'smooth' });
    document.dispatchEvent(new MouseEvent('mousemove', {
      clientX: Math.round(100 + Math.random() * 600),
      clientY: Math.round(100 + Math.random() * 400),
      bubbles: true,
    }));
  } catch (e) {}
})();
"""