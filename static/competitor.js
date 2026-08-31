/* Competitor Intelligence launcher — isolated, additive, owned by ads-ops.
 *
 * A top-of-dashboard button (Postly/Speakeasy only) that opens the existing competitor
 * dashboard in a NEW WINDOW, locked to the brand of the page you're on. The competitor app
 * is a separate Cloud Run service fed by the Meta Ad Library scraping pipeline. Touches
 * none of the data dashboard's code.
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
    '@media(max-width:560px){.toolbtn span.lbl{display:none}.toolbtn{padding:6px 9px}}'
  ].join('\n');
  document.head.appendChild(style);

  function brand() { return (document.body.dataset.brand || ''); }
  function targetUrl() {
    // embed=1 hides the competitor app's own product toggle; product= locks it to this
    // brand, so opening from Postly shows Postly and from Speakeasy shows Speakeasy.
    var b = brand();
    return URL_BASE + '?embed=1' + (BRANDS[b] ? ('&product=' + encodeURIComponent(b)) : '');
  }

  var btn = document.createElement('button');
  btn.className = 'toolbtn ci';
  btn.type = 'button';
  btn.title = 'Competitor Intelligence — opens in a new window';
  btn.innerHTML = '<span class="ci-dot"></span><span class="lbl">Competitor Intel</span>';
  btn.addEventListener('click', function () {
    window.open(targetUrl(), '_blank', 'noopener');
  });

  function sync() {
    var ok = !!BRANDS[brand()];
    if (ok && !btn.parentNode) host.insertBefore(btn, host.firstChild);
    else if (!ok && btn.parentNode) btn.parentNode.removeChild(btn);
  }
  sync();
  try { new MutationObserver(sync).observe(document.body, { attributes: true, attributeFilter: ['data-brand'] }); }
  catch (e) { /* no observer -> button stays for the initial brand */ }
})();
