/* Pop-ups and the AI-usage chip.

   Pop-ups: the page's messages (#flashes) shown as toasts in the corner, each marked as what it is (done, note,
   check this, problem). Problems stay until you close them; the rest fade after a while, never while you're
   reading one (hovered or focused). Things that finish in the background (a CV written, research done, the
   application assistant waiting for you, something failing) pop up as they happen, from the nerdbar's feed.

   The chip in the header shows today's AI requests left (or used, without a cap), refreshed after each request. */
(function () {
  var box = document.getElementById('flashes');
  var KINDS = {success: ['✓', 'Done'], info: ['i', 'Note'], warn: ['!', 'Check this'], error: ['✕', 'Problem']};
  var STAY = {success: 7000, info: 9000, warn: 14000};

  function kindOf(node) {
    for (var k in KINDS) { if (node.classList.contains('t-' + k)) return k; }
    return node.classList.contains('error') ? 'error' : 'info';
  }
  function close(node) {
    node.classList.add('leaving');
    setTimeout(function () { node.remove(); }, 220);
  }
  function arm(node) {
    if (node.dataset.armed) return;
    node.dataset.armed = '1';
    var kind = kindOf(node);
    node.classList.add('toast', 't-' + kind);
    var text = document.createElement('div');
    text.className = 'toast-text';
    while (node.firstChild) text.appendChild(node.firstChild);
    var head = document.createElement('div');
    head.className = 'toast-head';
    var icon = document.createElement('span'); icon.className = 'toast-icon'; icon.setAttribute('aria-hidden', 'true'); icon.textContent = KINDS[kind][0];
    var label = document.createElement('span'); label.className = 'toast-kind'; label.textContent = KINDS[kind][1];
    var x = document.createElement('button'); x.type = 'button'; x.className = 'toast-close'; x.setAttribute('aria-label', 'Close'); x.textContent = '×';
    x.addEventListener('click', function () { close(node); });
    head.appendChild(icon); head.appendChild(label); head.appendChild(x);
    node.appendChild(head); node.appendChild(text);
    if (STAY[kind]) {
      var left = STAY[kind], started = Date.now(), timer = null;
      var run = function () { started = Date.now(); timer = setTimeout(function () { close(node); }, left); };
      var hold = function () { clearTimeout(timer); left -= Date.now() - started; };
      node.addEventListener('mouseenter', hold); node.addEventListener('focusin', hold);
      node.addEventListener('mouseleave', run); node.addEventListener('focusout', run);
      run();
    }
    while (box.querySelectorAll('.toast:not(.leaving)').length > 5) close(box.querySelector('.toast:not(.leaving)'));
  }
  function armAll() { if (box) Array.prototype.forEach.call(box.children, arm); }

  function show(kind, message) {
    if (!box || !message) return;
    var node = document.createElement('div');
    node.className = 'flash t-' + (KINDS[kind] ? kind : 'info');
    node.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    node.textContent = message;
    box.appendChild(node);
    arm(node);
  }
  window.CVToast = {show: show};

  if (box) {
    armAll();
    new MutationObserver(armAll).observe(box, {childList: true});  // in-place updates bring new messages
  }

  // Background work that finished or needs you, as it happens (your own clicks aren't repeated back to you).
  var FROM_FEED = {ok: 'success', error: 'error', user: 'warn'};
  window.addEventListener('cv:event', function (e) {
    var ev = e.detail || {}, kind = FROM_FEED[ev.kind];
    if (!kind) return;
    var text = String(ev.text || '');
    if (ev.kind === 'user' && !/^⏸/.test(text)) return;  // "▶ resumed", "✓ you answered": things you just did
    show(kind, (ev.tag ? ev.tag + ': ' : '') + text.replace(/^[✓✗⏸!]\s*/, ''));
  });

  // ---- the AI-usage chip ----
  var chip = document.getElementById('ai-usage'), pending = null;
  function render(u) {
    if (!chip || !u) return;
    chip.className = 'ai-chip st-' + u.state;
    chip.dataset.state = u.state;
    var text = chip.querySelector('.ai-text'), extra = chip.querySelector('[data-ai=extra]');
    text.textContent = '';
    var strong = document.createElement('strong');
    if (u.cap) {
      text.appendChild(document.createTextNode('AI left today '));
      strong.textContent = u.left; text.appendChild(strong);
      var of = document.createElement('span'); of.className = 'faint'; of.textContent = '/' + u.cap; text.appendChild(of);
    } else {
      text.appendChild(document.createTextNode('AI today '));
      strong.textContent = u.used; text.appendChild(strong); text.appendChild(document.createTextNode(' req'));
    }
    var p = u.provider || {}, bits = [];
    if (p.credit_left != null) bits.push('$' + p.credit_left.toFixed(2) + ' credit');
    extra.textContent = bits.length ? '· ' + bits.join(' · ') : '';
    var tokens = (u.estimated ? '~' : '') + u.tokens_in.toLocaleString() + ' in, ' + (u.estimated ? '~' : '') + u.tokens_out.toLocaleString() + ' out';
    chip.title = (u.cap ? u.used + ' of your ' + u.cap + ' AI requests used today (' + u.left + ' left). ' : u.used + ' AI requests today, no daily cap set. ') +
      'Tokens today: ' + tokens + '.' +
      (p.name ? ' ' + p.name + (p.unavailable ? ': its own figures could not be read right now.' :
        (p.free_tier ? ': free tier, about ' + p.free_daily + ' free-model requests a day.' : '') +
        (p.credit_left != null ? ' Credit left: $' + p.credit_left.toFixed(2) + '.' : '')) : '') +
      ' The count resets at midnight. Change the cap in Settings.';
  }
  function refresh() {
    clearTimeout(pending);
    pending = setTimeout(function () {
      fetch('/api/usage', {headers: {'X-Requested-With': 'XMLHttpRequest'}})
        .then(function (r) { return r.json(); }).then(render).catch(function () { /* restarting */ });
    }, 800);
  }
  window.CVUsage = {refresh: refresh};
  if (chip) {
    window.addEventListener('cv:llm', refresh);
    refresh();
    setInterval(function () { if (!document.hidden) refresh(); }, 120000);
  }
})();
