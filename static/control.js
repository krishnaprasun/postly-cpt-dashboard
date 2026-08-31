/* Meta Ad Control launcher — isolated, additive, owned by ads-ops.
 *
 * A top-of-dashboard "Meta Control" button that opens the standalone Meta Ad Control
 * dashboard (pause / budget / scaling / builds) in a NEW WINDOW. That dashboard has its
 * own Google sign-in and email allowlist, so no credentials are collected here. A separate
 * <script> file so a fault here can never break the read views; the shared .toolbtn style
 * comes from competitor.js (loaded alongside).
 */
(function () {
  'use strict';
  var CONTROL_URL = 'https://postly-dashboard-360124450287.asia-south1.run.app/login';
  var host = document.getElementById('toolBtns');
  if (!host) return;

  var btn = document.createElement('button');
  btn.className = 'toolbtn';
  btn.type = 'button';
  btn.title = 'Meta Ad Control — pause, budgets, scaling and builds (opens in a new window)';
  btn.innerHTML = '<span>⚙</span><span class="lbl">Meta Control</span>';
  btn.addEventListener('click', function () {
    window.open(CONTROL_URL, '_blank', 'noopener');
  });
  host.appendChild(btn);
})();
