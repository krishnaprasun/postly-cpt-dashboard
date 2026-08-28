/* Control layer UI — owned by ads-ops, isolated from the data dashboard.
 *
 * A separate <script> file so a fault here can never break the read views. It adds a
 * "Control" tab (a button with no data-t, which the data dashboard's own handler ignores
 * by design) and swaps its panel into the content area — the header, tabs and brand
 * switcher stay put. Krishna's JS is untouched.
 *
 * Phase 0: id+password login + a placeholder that confirms access. The actual pause /
 * budget / scale / build controls arrive in later phases.
 */
(function () {
  'use strict';
  var tabs = document.getElementById('tabs');
  var panel = document.getElementById('ctrlPanel');
  if (!tabs || !panel) return;                 // hooks absent -> do nothing (safe)

  // Shared, idempotent swap manager: hides the data content and shows one special panel
  // (Control or Competitor) in its place, restoring on return to a data tab. Living on
  // window means Control and Competitor coordinate — only one special view at a time —
  // without either file knowing about the other's internals.
  var cpx = window.__cpx || (window.__cpx = (function () {
    var CONTENT = ['trendsWrap', 'matrixWrap', 'tblWrap', 'ctl', 'sctl', 'gctl', 'lctl'];
    var saved = null, activeEl = null;
    function hideData() {
      if (saved) return;                       // already hidden by the other panel
      saved = {};
      CONTENT.forEach(function (id) {
        var el = document.getElementById(id);
        if (el) { saved[id] = el.style.display; el.style.display = 'none'; }
      });
    }
    function showData() {
      if (!saved) return;
      CONTENT.forEach(function (id) {
        var el = document.getElementById(id);
        if (el) el.style.display = saved[id] || '';
      });
      saved = null;
    }
    return {
      activate: function (el) {
        if (activeEl && activeEl !== el) activeEl.style.display = 'none';
        hideData(); el.style.display = ''; activeEl = el;
      },
      deactivate: function () {
        if (activeEl) { activeEl.style.display = 'none'; activeEl = null; }
        showData();
      },
      active: function () { return activeEl; }
    };
  })());

  var style = document.createElement('style');
  style.textContent = [
    '#ctrlPanel{--cp-gap:16px}',
    '.cpwrap{max-width:640px;margin:8px auto 40px;padding:0 4px}',
    '.cpcard{background:var(--panel,#fff);border:1px solid var(--line,#e6e6e6);',
    '  border-radius:12px;padding:22px 22px 24px}',
    '.cpcard h3{margin:0 0 6px;font-size:17px;font-weight:680;color:var(--ink,#1a1c2e)}',
    '.cphead{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}',
    '.cpmuted{color:var(--muted,#787e91);font-size:13.5px;line-height:1.55;margin:0 0 14px}',
    '.cpform{display:flex;flex-direction:column;gap:12px;margin-top:6px}',
    '.cpform label{display:flex;flex-direction:column;gap:5px;font-size:12px;',
    '  letter-spacing:.02em;text-transform:uppercase;color:var(--muted,#787e91);font-weight:600}',
    '.cpform input{font:inherit;font-size:15px;padding:11px 12px;border:1px solid var(--line,#d8dee4);',
    '  border-radius:9px;background:var(--panel2,#fbfbfd);color:var(--ink,#1a1c2e);width:100%}',
    '.cpform input:focus{outline:2px solid var(--accent,#0b5c63);outline-offset:1px;border-color:transparent}',
    '.cpform button{margin-top:4px;font:inherit;font-weight:650;font-size:15px;padding:12px 14px;',
    '  border:0;border-radius:9px;background:var(--accent,#0b5c63);color:#fff;cursor:pointer}',
    '.cpform button:disabled{opacity:.6;cursor:default}',
    '.cperr{color:var(--crit,#a93226);font-size:13px;font-weight:600}',
    '.cplink{background:none;border:0;color:var(--muted,#787e91);font:inherit;font-size:12.5px;',
    '  cursor:pointer;text-decoration:underline;padding:0}',
    '.cplink:hover{color:var(--ink,#1a1c2e)}',
    '@media(max-width:520px){.cpwrap{margin-top:4px}.cpcard{padding:18px 16px 20px;border-radius:10px}}'
  ].join('\n');
  document.head.appendChild(style);

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function brand() { return (document.body.dataset.brand || ''); }
  function myBtn() { return tabs.querySelector('[data-ctrl]'); }
  function card(inner) { return '<div class="cpwrap"><div class="cpcard">' + inner + '</div></div>'; }

  function enter() {
    for (var i = 0; i < tabs.children.length; i++) tabs.children[i].classList.remove('on');
    var b = myBtn(); if (b) b.classList.add('on');
    cpx.activate(panel);
    render();
  }
  function leave() {
    var b = myBtn(); if (b) b.classList.remove('on');
    cpx.deactivate();
  }

  // Capture phase => runs before the data dashboard's own #tabs onclick, so leaving a
  // special view restores the content before its handler re-renders the chosen tab.
  tabs.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('button') : null;
    if (!btn) return;
    if (btn.hasAttribute('data-ctrl')) { e.preventDefault(); enter(); }
    else if (btn.dataset && btn.dataset.t) { leave(); }
  }, true);

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    opts.credentials = 'same-origin';
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        return { ok: r.ok, status: r.status, data: d };
      });
    });
  }

  function render() {
    panel.innerHTML = card('<div class="cpmuted">Checking access…</div>');
    api('/api/control/status').then(function (r) {
      var s = r.data || {};
      if (!s.enabled) {
        panel.innerHTML = card('<h3>Controls not enabled yet</h3><p class="cpmuted">The control '
          + 'layer is deployed but no admin credential is set. Ops needs to add it before sign-in works.</p>');
        return;
      }
      if (!s.authed) { renderLogin(''); return; }
      renderHome(s.user);
    }).catch(function () {
      panel.innerHTML = card('<h3>Control unavailable</h3><p class="cpmuted">Could not reach the control service. Try again shortly.</p>');
    });
  }

  function renderLogin(err) {
    panel.innerHTML = card(
      '<h3>Control access</h3>'
      + '<p class="cpmuted">Sign in to pause, adjust budgets and run builds for <b>' + esc(brand() || 'this brand') + '</b>.</p>'
      + '<form id="cpLogin" class="cpform" autocomplete="on">'
      + '<label>ID<input name="id" autocomplete="username" autofocus></label>'
      + '<label>Password<input name="pass" type="password" autocomplete="current-password"></label>'
      + (err ? '<div class="cperr">' + esc(err) + '</div>' : '')
      + '<button type="submit">Sign in</button>'
      + '</form>');
    var f = document.getElementById('cpLogin');
    f.addEventListener('submit', function (e) {
      e.preventDefault();
      var btn = f.querySelector('button'); btn.disabled = true; btn.textContent = 'Signing in…';
      api('/api/control/login', { method: 'POST', body: JSON.stringify({ id: f.id.value, pass: f.pass.value }) })
        .then(function (res) {
          if (res.ok) render();
          else renderLogin((res.data && res.data.error) || 'Sign-in failed.');
        })
        .catch(function () { renderLogin('Network error — try again.'); });
    });
  }

  function renderHome(user) {
    panel.innerHTML = card(
      '<div class="cphead"><h3>Controls · ' + esc(brand()) + '</h3>'
      + '<button id="cpOut" class="cplink">Sign out' + (user ? ' (' + esc(user) + ')' : '') + '</button></div>'
      + '<p class="cpmuted">Signed in — access confirmed. Ad-set <b>pause</b>, <b>budget</b> and <b>scaling</b> '
      + 'controls, the daily flag-report execution, and build triggers arrive in the next update.</p>');
    document.getElementById('cpOut').addEventListener('click', function () {
      api('/api/control/logout', { method: 'POST' }).then(render);
    });
  }
})();
