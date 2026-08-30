/* Competitor Intelligence — isolated, additive, owned by ads-ops.
 *
 * A top-of-dashboard button (Postly/Speakeasy only) that opens the existing competitor
 * dashboard as a FULL-SCREEN view inside the page (not a browser tab). The competitor app
 * is a separate Cloud Run service fed by the Meta Ad Library scraping pipeline; it's
 * embedded brand-locked (its own product toggle hidden via embed=1). Touches none of the
 * data dashboard's code.
 */
(function () {
  'use strict';
  var URL_BASE = 'https://postly-intel-api-360124450287.asia-south1.run.app/';
  var BRANDS = { postly: 1, speakeasy: 1 };
  var host = document.getElementById('toolBtns');
  if (!host) return;

  var style = document.createElement('style');
  style.textContent = [
    '.toolbtns{display:flex;gap:8px;align-items:center}',
    '@property --ci-a{syntax:"<angle>";initial-value:0deg;inherits:false}',
    '.toolbtn{position:relative;display:inline-flex;align-items:center;gap:6px;',
    '  padding:6px 13px;border-radius:999px;cursor:pointer;font:inherit;font-size:12.5px;',
    '  font-weight:650;white-space:nowrap;border:1px solid var(--line,#e0e0e0);',
    '  background:var(--panel,#fff);color:var(--ink,#12333a)}',
    '.toolbtn:hover{color:var(--accent,#0b5c63);border-color:var(--accent,#0b5c63)}',
    '.toolbtn.ci{border:0}',
    '.toolbtn.ci::before{content:"";position:absolute;inset:-1px;border-radius:999px;padding:2px;',
    '  background:conic-gradient(from var(--ci-a),transparent 0 62%,#22d3ee 80%,#3b82f6 92%,transparent 100%);',
    '  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);',
    '  -webkit-mask-composite:xor;mask-composite:exclude;animation:ci-spin 2.4s linear infinite;pointer-events:none}',
    '@keyframes ci-spin{to{--ci-a:360deg}}',
    '.ci-dot{width:7px;height:7px;border-radius:50%;background:#22d3ee;box-shadow:0 0 6px 1px #22d3ee;',
    '  animation:ci-pulse 1.6s ease-in-out infinite}',
    '@keyframes ci-pulse{0%,100%{opacity:1}50%{opacity:.35}}',
    '@media(prefers-reduced-motion:reduce){.toolbtn.ci::before,.ci-dot{animation:none}}',
    /* full-screen in-app view */
    '.tool-ov{position:fixed;inset:0;z-index:9998;background:var(--ground,#f3f6f7);display:flex;flex-direction:column}',
    '.tool-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;',
    '  padding:11px 18px;border-bottom:1px solid var(--line,#e6e6e6);background:var(--panel,#fff);flex:0 0 auto}',
    '.tool-ttl{display:flex;align-items:center;gap:9px;font-weight:700;font-size:14.5px;color:var(--ink,#111)}',
    '.tool-ttl .sub{color:var(--muted,#777);font-weight:600;text-transform:capitalize}',
    '.tool-x{border:1px solid var(--line,#e0e0e0);background:var(--panel2,#fafafa);border-radius:8px;',
    '  padding:6px 13px;font:inherit;font-size:13px;font-weight:600;cursor:pointer;color:var(--muted,#555)}',
    '.tool-x:hover{color:var(--ink,#111)}',
    '.tool-frame{flex:1;width:100%;border:0;background:var(--panel,#fff)}',
    '@media(max-width:560px){.toolbtn span.lbl{display:none}.toolbtn{padding:6px 9px}}'
  ].join('\n');
  document.head.appendChild(style);

  function brand() { return (document.body.dataset.brand || ''); }
  function targetUrl() {
    var b = brand();
    return URL_BASE + '?embed=1' + (BRANDS[b] ? ('&product=' + encodeURIComponent(b)) : '');
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  var btn = document.createElement('button');
  btn.className = 'toolbtn ci';
  btn.type = 'button';
  btn.title = 'Competitor Intelligence — Meta Ad Library activity for tracked competitors';
  btn.innerHTML = '<span class="ci-dot"></span><span class="lbl">Competitor Intel</span>';

  var ov = null;
  function open() {
    close();
    ov = document.createElement('div');
    ov.className = 'tool-ov';
    ov.innerHTML =
      '<div class="tool-bar"><div class="tool-ttl"><span class="ci-dot"></span>Competitor Intelligence'
      + ' · <span class="sub">' + esc(brand()) + '</span></div>'
      + '<button class="tool-x" type="button">Close ✕</button></div>'
      + '<iframe class="tool-frame" title="Competitor Intelligence" referrerpolicy="no-referrer" src="' + esc(targetUrl()) + '"></iframe>';
    document.body.appendChild(ov);
    document.body.style.overflow = 'hidden';
    ov.querySelector('.tool-x').addEventListener('click', close);
    document.addEventListener('keydown', onEsc);
  }
  function close() {
    if (ov && ov.parentNode) ov.parentNode.removeChild(ov);
    ov = null;
    document.body.style.overflow = '';
    document.removeEventListener('keydown', onEsc);
  }
  function onEsc(e) { if (e.key === 'Escape') close(); }
  btn.addEventListener('click', open);

  // Mount only for brands with competitor data; keep the open view synced to the brand.
  function sync() {
    var ok = !!BRANDS[brand()];
    if (ok && !btn.parentNode) host.insertBefore(btn, host.firstChild);
    else if (!ok && btn.parentNode) { btn.parentNode.removeChild(btn); close(); return; }
    if (ok && ov) {                                   // brand switched while open -> follow
      ov.querySelector('.tool-ttl .sub').textContent = brand();
      var f = ov.querySelector('iframe'); if (f && f.getAttribute('src') !== targetUrl()) f.setAttribute('src', targetUrl());
    }
  }
  sync();
  try { new MutationObserver(sync).observe(document.body, { attributes: true, attributeFilter: ['data-brand'] }); }
  catch (e) { /* no observer -> button stays for the initial brand */ }
})();
