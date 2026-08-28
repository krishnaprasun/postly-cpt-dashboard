/* Competitor Intelligence — isolated, additive, owned by ads-ops.
 *
 * Surfaces the existing competitor-intel dashboard (its own Cloud Run app, fed by the
 * Meta Ad Library scraping pipeline) IN PLACE on the Postly and Speakeasy brand pages:
 * clicking the button swaps only the numbers area to the competitor view, keeping the
 * header, brand switcher and tabs above exactly as they are. No redirect, no new tab.
 * Brand-locked to whichever brand page you're on; switching brand up top follows along.
 * Touches none of the data dashboard's code.
 */
(function () {
  'use strict';
  var URL_BASE = 'https://postly-intel-api-360124450287.asia-south1.run.app/';
  var BRANDS = { postly: 1, speakeasy: 1 };            // only these have competitor data
  var tabs = document.getElementById('tabs');
  if (!tabs) return;

  // Shared swap manager (same one control.js sets up): hides the data content and shows
  // one special panel in its place, so Control and Competitor never fight over the area.
  var cpx = window.__cpx || (window.__cpx = (function () {
    var CONTENT = ['trendsWrap', 'matrixWrap', 'tblWrap', 'ctl', 'sctl', 'gctl', 'lctl'];
    var saved = null, activeEl = null;
    function hideData() {
      if (saved) return; saved = {};
      CONTENT.forEach(function (id) { var el = document.getElementById(id); if (el) { saved[id] = el.style.display; el.style.display = 'none'; } });
    }
    function showData() {
      if (!saved) return;
      CONTENT.forEach(function (id) { var el = document.getElementById(id); if (el) el.style.display = saved[id] || ''; });
      saved = null;
    }
    return {
      activate: function (el) { if (activeEl && activeEl !== el) activeEl.style.display = 'none'; hideData(); el.style.display = ''; activeEl = el; },
      deactivate: function () { if (activeEl) { activeEl.style.display = 'none'; activeEl = null; } showData(); },
      active: function () { return activeEl; }
    };
  })());

  var style = document.createElement('style');
  style.textContent = [
    '@property --ci-a{syntax:"<angle>";initial-value:0deg;inherits:false}',
    '.ci-btn{position:relative;display:inline-flex;align-items:center;gap:6px;',
    '  margin-left:auto;padding:5px 12px;border:0;border-radius:999px;cursor:pointer;',
    '  font:inherit;font-size:12px;font-weight:650;color:var(--ink,#0b3b3f);',
    '  background:var(--panel,#fff);white-space:nowrap}',
    '.ci-btn::before{content:"";position:absolute;inset:-2px;border-radius:999px;padding:2px;',
    '  background:conic-gradient(from var(--ci-a),transparent 0 62%,#22d3ee 80%,#3b82f6 92%,transparent 100%);',
    '  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);',
    '  -webkit-mask-composite:xor;mask-composite:exclude;',
    '  animation:ci-spin 2.4s linear infinite;pointer-events:none}',
    '.ci-btn.on{color:var(--accent,#0b5c63)}',
    '@keyframes ci-spin{to{--ci-a:360deg}}',
    '.ci-dot{width:7px;height:7px;border-radius:50%;background:#22d3ee;',
    '  box-shadow:0 0 6px 1px #22d3ee;animation:ci-pulse 1.6s ease-in-out infinite}',
    '@keyframes ci-pulse{0%,100%{opacity:1}50%{opacity:.35}}',
    '@media(prefers-reduced-motion:reduce){.ci-btn::before,.ci-dot{animation:none}}',
    /* in-place panel */
    '#ciPanel{margin-top:6px}',
    '.ci-frame{width:100%;border:0;border-radius:12px;background:var(--panel,#fff);',
    '  box-shadow:0 1px 0 var(--line,#eee);min-height:70vh;display:block}'
  ].join('\n');
  document.head.appendChild(style);

  function brand() { return (document.body.dataset.brand || ''); }
  function targetUrl() {
    var b = brand();
    return URL_BASE + (BRANDS[b] ? ('?brand=' + encodeURIComponent(b)) : '');
  }

  // The button lives in the tab row; no data-t / data-ctrl, so neither the data dashboard's
  // handler nor the control layer treats it as a tab.
  var btn = document.createElement('button');
  btn.className = 'ci-btn';
  btn.type = 'button';
  btn.setAttribute('data-ci', '1');
  btn.title = 'Competitor Intelligence — Meta Ad Library activity for tracked competitors';
  btn.innerHTML = '<span class="ci-dot"></span><span>Competitor Intel</span>';

  // The in-place panel that holds the iframe. Injected next to the data table so it lands
  // in the same content slot; created once, reused.
  var panel = document.createElement('div');
  panel.id = 'ciPanel';
  panel.style.display = 'none';
  panel.innerHTML = '<iframe class="ci-frame" title="Competitor Intelligence" referrerpolicy="no-referrer" loading="lazy"></iframe>';
  var anchor = document.getElementById('ctrlPanel') || document.getElementById('tblWrap');
  if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(panel, anchor.nextSibling);
  else document.body.appendChild(panel);
  var frame = panel.querySelector('iframe');

  function fit() {
    // Fill from the panel's top to the viewport bottom, so the header/tabs stay visible
    // above and the competitor view uses the rest — no inner scrollbar-in-scrollbar.
    var top = panel.getBoundingClientRect().top;
    var h = Math.max(420, Math.round((window.innerHeight || 800) - top - 16));
    frame.style.height = h + 'px';
  }

  function load() {
    var want = targetUrl();
    if (frame.getAttribute('src') !== want) frame.setAttribute('src', want);
  }

  function enter() {
    for (var i = 0; i < tabs.children.length; i++) tabs.children[i].classList.remove('on');
    btn.classList.add('on');
    cpx.activate(panel);
    load(); fit();
  }
  function markOff() { btn.classList.remove('on'); }

  // Capture phase so it runs before the data dashboard's own onclick.
  tabs.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button') : null;
    if (!b) return;
    if (b.hasAttribute('data-ci')) { e.preventDefault(); enter(); }
    else { markOff(); if (b.dataset && b.dataset.t) cpx.deactivate(); }
  }, true);

  window.addEventListener('resize', function () { if (cpx.active() === panel) fit(); });

  // Only mount for brands with competitor data; keep the view in sync when the brand
  // switches up top (the data dashboard sets document.body.dataset.brand).
  function sync() {
    var ok = !!BRANDS[brand()];
    if (ok && !btn.parentNode) tabs.appendChild(btn);
    else if (!ok && btn.parentNode) {
      btn.parentNode.removeChild(btn);
      if (cpx.active() === panel) { cpx.deactivate(); markOff(); }   // leave competitor if brand lost it
      return;
    }
    if (ok && cpx.active() === panel) { load(); fit(); }             // follow the brand switch in place
  }
  sync();
  try {
    new MutationObserver(sync).observe(document.body, { attributes: true, attributeFilter: ['data-brand'] });
  } catch (e) { /* no observer -> button stays for the initial brand, still fine */ }
})();
