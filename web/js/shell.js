/* INSIA 에이전트 스튜디오 — app shell: top navigation, hash router, login view, boot.
 *
 * Routes: #/studio (default) · #/library[/<item id>] · #/calendar · #/brand · #/usage · #/login
 * Loaded last, after every view registered itself in INSIA.views.
 */
(function () {
  'use strict';

  var I = window.INSIA;
  var U = I.util;
  var ws = I.ws;
  var el = U.el;
  var VIEWS = ['studio', 'library', 'calendar', 'brand', 'usage', 'login'];
  var current = null;
  var booted = false;
  var loginBack = null;
  var deferredPlay = false;   // demo replay waits until the studio is first shown

  function parseHash() {
    var h = '';
    try { h = decodeURIComponent(location.hash || ''); } catch (e) { h = location.hash || ''; }
    var m = /^#\/([a-z]+)(?:\/(.+))?$/.exec(h);
    if (!m || VIEWS.indexOf(m[1]) < 0) return null;
    return { view: m[1], param: m[2] || '' };
  }
  function hashFor(view, param) { return '#/' + view + (param ? '/' + encodeURIComponent(param) : ''); }

  /** Navigate. opts: {focus (default true), replace, back (login return route)} */
  function go(view, param, opts) {
    opts = opts || {};
    if (VIEWS.indexOf(view) < 0) view = 'studio';
    if (view === 'login') loginBack = opts.back || (current && current.view !== 'login' ? current : null);
    var want = hashFor(view, param);
    try {
      if (location.hash !== want) {
        if (opts.replace && history.replaceState) history.replaceState(null, '', want);
        else { location.hash = want; return; } // hashchange calls show()
      }
    } catch (e) { /* sandboxed frame without history: route in memory */ }
    show(view, param || '', opts.focus !== false);
  }
  ws.go = go;

  function show(view, param, focus) {
    // workspace views need the server; while locked they all send the user to the login card
    if (ws.mode === 'auth' && view !== 'studio' && view !== 'login') {
      loginBack = { view: view, param: param };
      view = 'login';
      param = '';
    }
    var prev = current;
    current = { view: view, param: param };
    ws.route = current;
    document.getElementById('app').dataset.view = view;
    VIEWS.forEach(function (v) {
      var sec = document.getElementById('view-' + v);
      if (sec) sec.hidden = v !== view;
    });
    Array.prototype.forEach.call(document.querySelectorAll('#appnav a'), function (a) {
      if (a.dataset.view === view || (view === 'login' && loginBack && a.dataset.view === loginBack.view)) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
    if (prev && prev.view !== view && I.views[prev.view] && I.views[prev.view].hide) I.views[prev.view].hide();
    I.studio.setVisible(view === 'studio');
    if (view === 'studio' && deferredPlay) {
      deferredPlay = false;
      // only the demo recording auto-plays; a server run the stage switched to meanwhile stays put
      var src = I.studio.source ? I.studio.source() : 'recorded';
      if (src === 'sample' || src === 'recorded') I.play();
    }
    var container = document.getElementById('view-' + view);
    if (view === 'login') renderLogin(container);
    else if (view !== 'studio' && I.views[view]) I.views[view].show(container, param, { focus: focus, prev: prev });
    if (focus) {
      var h = container.querySelector('.view-title, #studioTitle');
      if (h && view !== 'library') ws.ui.focusHeading(h);
      if (view === 'studio') window.scrollTo(0, 0);
    }
  }

  // ------------------------------------------------------------------ login (token mode)
  function renderLogin(container) {
    var input = el('input', { id: 'loginToken', name: 'token', type: 'password', autocomplete: 'current-password', required: true, spellcheck: 'false' });
    var err = el('p', { class: 'form-error', role: 'alert', hidden: !ws.authMessage || ws.authMessage === '로그인이 필요해요', text: ws.authMessage || '' });
    var submit = el('button', { type: 'submit', class: 'btn btn--primary', text: '로그인' });
    var form = el('form', { class: 'login-form', novalidate: true }, [
      el('label', { class: 'field', for: 'loginToken' }, [el('span', { text: '접근 토큰' }), input]),
      err,
      el('div', { class: 'login-actions' }, [submit, el('a', { href: '#/studio', class: 'btn btn--ghost', text: '토큰 없이 데모만 보기' })])
    ]);
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var token = input.value.trim();
      if (!token) { err.textContent = '토큰을 입력해 주세요.'; err.hidden = false; input.focus(); return; }
      submit.disabled = true;
      submit.textContent = '확인하는 중…';
      fetch('/api/login', {
        method: 'POST', mode: 'same-origin', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ token: token })
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) throw new Error((j && j.error) || '토큰이 맞지 않아요.');
          return I.studio.detectServer();
        });
      }).then(function (info) {
        if (!info || info.authRequired) throw new Error('로그인했지만 서버가 아직 토큰을 받지 않아요. 브라우저가 쿠키를 막고 있는지 확인해 주세요.');
        input.value = '';
        enterLiveMode(info);
        ws.ui.toast('로그인했어요.', 'success');
        var back = loginBack && loginBack.view !== 'login' ? loginBack : { view: 'library', param: '' };
        loginBack = null;
        go(back.view, back.param, { replace: true });
      }).catch(function (ex) {
        err.textContent = ex && ex.message ? ex.message : '로그인하지 못했어요.';
        err.hidden = false;
        input.focus();
        input.select();
      }).then(function () {
        submit.disabled = false;
        submit.textContent = '로그인';
      });
    });
    ws.mount(container, el('div', { class: 'login-card' }, [
      el('p', { class: 'eyebrow', text: '보호된 서버' }),
      el('h2', { class: 'view-title', id: 'loginTitle', tabindex: '-1', text: '접근 토큰을 넣어 주세요' }),
      el('p', { class: 'view-sub', text: '이 서버는 접근 토큰으로 잠겨 있어요. 서버를 켤 때 정한 INSIA_ACCESS_TOKEN 값(또는 --token 값)을 넣으면 보관함·캘린더·브랜드·사용량을 쓸 수 있어요. 토큰은 이 브라우저의 쿠키로만 기억해요.' }),
      form
    ]));
    setTimeout(function () { if (document.activeElement === document.body || !container.contains(document.activeElement)) input.focus(); }, 0);
  }

  function enterLiveMode(info) {
    ws.mode = 'live';
    ws.health = info;
    ws.authMessage = '';
    I.studio.setServer(info);
    Object.keys(I.views).forEach(function (k) { if (I.views[k].reset) I.views[k].reset(); });
    if (I.views.library && I.views.library.prefetch) I.views.library.prefetch();
    syncAuthUi();
    if (ws.runs) ws.runs.attach();
  }

  /** Header controls that depend on the server mode: 로그아웃 (token mode), 실행 기록, run bar. */
  function syncAuthUi() {
    var out = document.getElementById('btnLogout');
    if (out) out.hidden = !(ws.mode === 'live' && ws.health && ws.health.token_required);
    if (ws.runs) ws.runs.sync();
  }
  ws.syncAuthUi = syncAuthUi;

  function logout() {
    var btn = document.getElementById('btnLogout');
    if (btn) btn.disabled = true;
    fetch('/api/logout', {
      method: 'POST', mode: 'same-origin', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: '{}'
    }).catch(function () { return null; }).then(function () {
      if (btn) btn.disabled = false;
      ws.health = null;
      ws.authMessage = '';
      ws.mode = 'auth';
      I.studio.setServer({ authRequired: true });
      Object.keys(I.views).forEach(function (k) { if (I.views[k].reset) I.views[k].reset(); });
      var badge = document.getElementById('navCountLibrary');
      if (badge) badge.hidden = true;
      syncAuthUi();
      ws.ui.toast('로그아웃했어요. 이 브라우저의 로그인 쿠키를 지웠어요.', 'info');
      go('login', '', { back: { view: 'library', param: '' } });
    });
  }

  // ------------------------------------------------------------------ boot
  function start() {
    var out = document.getElementById('btnLogout');
    if (out) out.addEventListener('click', logout);
    Array.prototype.forEach.call(document.querySelectorAll('#appnav a'), function (a) {
      a.addEventListener('click', function (e) {
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        go(a.dataset.view, '');
      });
    });
    window.addEventListener('hashchange', function () {
      var r = parseHash();
      if (r) show(r.view, r.param, true);
    });
    I.ready.then(function (res) {
      var info = res && res.server;
      if (info && info.authRequired) ws.mode = 'auth';
      else if (info) { ws.mode = 'live'; ws.health = info; }
      else ws.mode = 'demo';
      booted = true;
      var r = parseHash();
      if (ws.mode === 'live' && I.views.library && I.views.library.prefetch) I.views.library.prefetch();
      syncAuthUi();
      if (ws.mode === 'live' && ws.runs) ws.runs.attach();
      // opened straight into another screen: start the stage replay when the studio is first shown
      if (r && r.view !== 'studio' && I.player.playing) { I.pause(); deferredPlay = true; }
      if (ws.mode === 'auth') {
        go('login', '', { replace: true, focus: false, back: r && r.view !== 'login' ? r : null });
      } else if (r && !(r.view === 'login')) {
        show(r.view, r.param, false);
      } else {
        show('studio', '', false);
      }
    });
  }

  ws.isBooted = function () { return booted; };
  start();
})();
