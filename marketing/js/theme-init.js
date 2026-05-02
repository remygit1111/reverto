// Apply theme as early as possible to prevent flash of wrong
// theme on initial paint. Default = dark; localStorage opt-in
// for light. Wrapped in try/catch because localStorage can be
// disabled (private mode, sandbox, quota) and we still want a
// usable page.
//
// This script must be loaded synchronously in <head> BEFORE
// the stylesheet, so the data-theme attribute is set before
// CSS evaluates [data-theme="light"] selectors. Using
// `defer` or `async` would defeat the purpose — the flash
// would happen.
//
// Externalized from inline <head> script to comply with
// strict CSP `script-src 'self'`.

(function () {
  try {
    var t = localStorage.getItem('reverto-theme');
    document.documentElement.dataset.theme = (t === 'light') ? 'light' : 'dark';
  } catch (e) {
    document.documentElement.dataset.theme = 'dark';
  }
})();
