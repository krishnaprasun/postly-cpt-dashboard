/* Control layer UI — owned by ads-ops, isolated from the data dashboard.
 *
 * A top-of-dashboard "Meta Control" button that opens the control panel as a FULL-SCREEN
 * view inside the page. Gated by an id+password login (Google SSO later). A separate
 * <script> file so a fault here can never break the read views; the shared .toolbtn /
 * .tool-ov styles come from competitor.js (loaded alongside).
 *
 * Phase 0: id+password login + a placeholder that confirms access. The actual pause /
 * budget / scale / build controls arrive in later phases.
 */
(function () {
  'use strict';
  var host = document.getElementById('toolBtns');
  if (!host) return;

  var style = document.createElement('style');
  style.textContent = [
    '.mc-wrap{max-width:640px;margin:26px auto 40px;padding:0 16px}',
    '.mc-card{background:var(--panel,#fff);border:1px solid var(--line,#e6e6e6);border-radius:12px;padding:22px 22px 24px}',
    '.mc-card h3{margin:0 0 6px;font-size:17px;font-weight:680;color:var(--ink,#1a1c2e)}',
    '.mc-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}',
    '.mc-muted{color:var(--muted,#787e91);font-size:13.5px;line-height:1.55;margin:0 0 14px}',
    '.mc-form{display:flex;flex-direction:column;gap:12px;margin-top:6px}',
    '.mc-form label{display:flex;flex-direction:column;gap:5px;font-size:12px;letter-spacing:.02em;',
    '  text-transform:uppercase;color:var(--muted,#787e91);font-weight:600}',
    '.mc-form input{font:inherit;font-size:15px;padding:11px 12px;border:1px solid var(--line,#d8dee4);',
    '  border-radius:9px;background:var(--panel2,#fbfbfd);color:var(--ink,#1a1c2e);width:100%}',
    '.mc-form input:focus{outline:2px solid var(--accent,#0b5c63);outline-offset:1px;border-color:transparent}',
    '.mc-form button{margin-top:4px;font:inherit;font-weight:650;font-size:15px;padding:12px 14px;',
    '  border:0;border-radius:9px;background:var(--accent,#0b5c63);color:#fff;cursor:pointer}',
    '.mc-form button:disabled{opacity:.6;cursor:default}',
    '.mc-err{color:var(--crit,#a93226);font-size:13px;font-weight:600}',
    '.mc-link{background:none;border:0;color:var(--muted,#787e91);font:inherit;font-size:12.5px;',
    '  cursor:pointer;text-decoration:underline;padding:0}',
    '.mc-link:hover{color:var(--ink,#1a1c2e)}'
  ].join('\n');
  document.head.appendChild(style);

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function brand() { return (document.body.dataset.brand || ''); }
  function card(inner) { return '<div class="mc-wrap"><div class="mc-card">' + inner + '</div></div>'; }

  var btn = document.createElement('button');
  btn.className = 'toolbtn';
  btn.type = 'button';
  btn.title = 'Meta Ad Control — pause, budgets, scaling and builds';
  btn.innerHTML = '<span>⚙</span><span class="lbl">Meta Control</span>';
  host.appendChild(btn);

  var ov = null, body = null;
  function open() {
    close();
    ov = document.createElement('div');
    ov.className = 'tool-ov';
    ov.innerHTML =
      '<div class="tool-bar"><div class="tool-ttl">⚙ Meta Ad Control · <span class="sub">' + esc(brand()) + '</span></div>'
      + '<button class="tool-x" type="button">Close ✕</button></div>'
      + '<div class="tool-frame" id="mcBody" style="overflow:auto"></div>';
    document.body.appendChild(ov);
    document.body.style.overflow = 'hidden';
    body = ov.querySelector('#mcBody');
    ov.querySelector('.tool-x').addEventListener('click', close);
    document.addEventListener('keydown', onEsc);
    render();
  }
  function close() {
    if (ov && ov.parentNode) ov.parentNode.removeChild(ov);
    ov = null; body = null;
    document.body.style.overflow = '';
    document.removeEventListener('keydown', onEsc);
  }
  function onEsc(e) { if (e.key === 'Escape') close(); }
  btn.addEventListener('click', open);

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    opts.credentials = 'same-origin';
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) { return { ok: r.ok, status: r.status, data: d }; });
    });
  }

  function render() {
    if (!body) return;
    body.innerHTML = card('<div class="mc-muted">Checking access…</div>');
    api('/api/control/status').then(function (r) {
      if (!body) return;
      var s = r.data || {};
      if (!s.enabled) {
        body.innerHTML = card('<h3>Controls not enabled yet</h3><p class="mc-muted">The control layer is '
          + 'deployed but no admin credential is set. Ops needs to add it before sign-in works.</p>');
        return;
      }
      if (!s.authed) { renderLogin(''); return; }
      renderHome(s.user);
    }).catch(function () {
      if (body) body.innerHTML = card('<h3>Control unavailable</h3><p class="mc-muted">Could not reach the control service. Try again shortly.</p>');
    });
  }

  function renderLogin(err) {
    if (!body) return;
    body.innerHTML = card(
      '<h3>Control access</h3>'
      + '<p class="mc-muted">Sign in to pause, adjust budgets and run builds for <b>' + esc(brand() || 'this brand') + '</b>.</p>'
      + '<form id="mcLogin" class="mc-form" autocomplete="on">'
      + '<label>ID<input name="id" autocomplete="username" autofocus></label>'
      + '<label>Password<input name="pass" type="password" autocomplete="current-password"></label>'
      + (err ? '<div class="mc-err">' + esc(err) + '</div>' : '')
      + '<button type="submit">Sign in</button></form>');
    var f = document.getElementById('mcLogin');
    f.addEventListener('submit', function (e) {
      e.preventDefault();
      var b = f.querySelector('button'); b.disabled = true; b.textContent = 'Signing in…';
      api('/api/control/login', { method: 'POST', body: JSON.stringify({ id: f.id.value, pass: f.pass.value }) })
        .then(function (res) { if (res.ok) render(); else renderLogin((res.data && res.data.error) || 'Sign-in failed.'); })
        .catch(function () { renderLogin('Network error — try again.'); });
    });
  }

  function renderHome(user) {
    if (!body) return;
    body.innerHTML = card(
      '<div class="mc-head"><h3>Controls · ' + esc(brand()) + '</h3>'
      + '<button id="mcOut" class="mc-link">Sign out' + (user ? ' (' + esc(user) + ')' : '') + '</button></div>'
      + '<p class="mc-muted">Signed in — access confirmed. Ad-set <b>pause</b>, <b>budget</b> and <b>scaling</b> '
      + 'controls, the daily flag-report execution, and build triggers arrive in the next update.</p>');
    document.getElementById('mcOut').addEventListener('click', function () {
      api('/api/control/logout', { method: 'POST' }).then(render);
    });
  }
})();
