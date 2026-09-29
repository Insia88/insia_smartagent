/* INSIA 에이전트 스튜디오 — API 게시 (LinkedIn · 인스타그램), human-confirmed only.
 *
 * Hooks used by the other views (every hook returns null / does nothing unless the server says publishing is
 * configured, so a workspace that never set it up renders exactly as before):
 *   I.publish.button(ctx)        보관함 "검토와 게시" card: the "…에 API로 게시" button (end of the action row)
 *   I.publish.section(ctx)       below the action row: why the button is off, progress, result, 결과 불명 card
 *   I.publish.panel(key, ctx)    panel 'api_attempts' (게시 기록); 'api_publish' is the dialog (no panel body)
 *   I.publish.lock(ctx)          Korean reason while an attempt is sending / unknown (edit · review · archive off)
 *   I.publish.statusPill(item)   "게시 완료 · API" for items published through the API (else null)
 *   I.publish.connectionsCard(box, opts)   브랜드·자료 "API 게시 연결" card (the only setup entry point)
 *
 * ctx (from library.js): {item, detail, busy, editing, job, getPanel(), setPanel(k), rerender(), refresh(), edit()}.
 * JSON shapes: DESIGN.md 6-2 (GET /api/publish, item `publish` block, preview, attempt). Nothing is ever posted
 * without a person: the dialog shows the preview the server stored, the checkbox must be ticked, and the button
 * turns itself off on the first click. No schedule, no retry, no logos (plain names only: LinkedIn API terms 6.1).
 *
 * Classic script (no modules) so build_artifact.py can inline it; the artifact is demo mode (no publishing UI).
 * Every server or user string reaches the DOM through textContent.
 */
(function () {
  'use strict';

  var I = window.INSIA;
  var U = I.util;
  var ws = I.ws;
  var el = U.el;

  var LABEL = { linkedin: 'LinkedIn', instagram: '인스타그램' };
  var KIND = { linkedin: 'LinkedIn 개인 프로필', instagram: '인스타그램 비즈니스 계정' };
  var VIS = [['PUBLIC', '전체 공개'], ['CONNECTIONS', '1촌 공개']];
  var STATUS_LABEL = { sending: '게시 중', published: '게시함', failed: '실패', unknown: '확인 필요', abandoned: '안 올라감' };
  var CALLBACK = {
    ok: ['success', 'LinkedIn 계정을 연결했어요.'],
    cancelled: ['info', 'LinkedIn 연결을 취소했어요.'],
    expired: ['error', '연결 요청이 만료됐거나 올바르지 않아요. 다시 연결해 주세요.'],
    invalid: ['error', '연결 요청이 만료됐거나 올바르지 않아요. 다시 연결해 주세요.'],
    exchange_failed: ['error', 'LinkedIn에서 연결을 마치지 못했어요. 개발자 앱의 Redirect URL이 아래 주소와 똑같은지 확인해 주세요.']
  };
  var IG_MAX_TAGS = 5;
  // waits between polls: checks at 0.1, 0.5, 1, 2, 4 s, then every 2 s (a LinkedIn post takes about a second, so its
  // result shows at about 1 s instead of 2.85 s; Instagram's minutes of processing poll every 2 s as before)
  var POLL_MS = [100, 400, 500, 1000, 2000];
  var STATUS_TTL = 60 * 1000;

  var P = {
    status: null,        // GET /api/publish (cached)
    statusAt: 0,
    statusLoading: null,
    polls: {},           // itemId -> {attemptId, platform, attempt, timer, n}
    done: {},            // attemptId -> the finished attempt (a stale detail may still call it active)
    results: {},         // itemId -> {status, attempt, platform, firstComment} (this session)
    resolving: {},       // itemId -> 'published' | 'not_published' (the unknown card's open step)
    attempts: {},        // itemId -> list | Error (게시 기록)
    dlg: null,           // dialog state
    conn: { box: null, error: null, paste: null, msgs: [], confirm: '', form: {}, busy: '', igToken: '', editRedirect: false }
  };

  function live() { return ws.mode === 'live'; }
  function label(p) { return LABEL[p] || '플랫폼'; }
  function block(ctx) {
    var b = live() && ctx && ctx.detail ? ctx.detail.publish : null;
    return b && typeof b === 'object' && b.platform ? b : null;
  }
  function pid(itemId) { return '/api/items/' + encodeURIComponent(itemId); }
  function isHttps(u) { return /^https:\/\/\S+$/i.test(String(u || '')); }
  function num(n) { return U.fmtNum(Number(n) || 0); }

  // ------------------------------------------------------------------ status (GET /api/publish)
  function loadStatus(force) {
    if (!live()) return Promise.resolve(null);
    if (!force && P.statusAt && Date.now() - P.statusAt < STATUS_TTL) return Promise.resolve(P.status);
    if (P.statusLoading && !force) return P.statusLoading;
    var req = P.statusLoading = ws.get('/api/publish').then(function (s) {
      if (P.statusLoading === req) P.statusLoading = null;
      P.status = s && typeof s === 'object' ? s : null;
      P.statusAt = Date.now();
      return P.status;
    }, function (err) {
      if (P.statusLoading === req) P.statusLoading = null;
      P.statusAt = Date.now();   // do not ask again on every repaint; the next minute or a reload will
      throw err;
    });
    return req;
  }
  /** Load the status once in the background and repaint the caller when it arrives. */
  function wantStatus(ctx) {
    if (P.statusAt || P.statusLoading) return;
    loadStatus(false).then(function () { if (ctx && ctx.rerender) ctx.rerender(); }, function () { /* keep the generic text */ });
  }
  function platformStatus(p) {
    return P.status && P.status.platforms && P.status.platforms[p] ? P.status.platforms[p] : null;
  }
  function isFake() {
    return !!((P.status && P.status.fake) || (ws.health && ws.health.publish && ws.health.publish.fake));
  }
  function accountText(p, acc) {
    acc = acc || {};
    var name = p === 'instagram' ? (acc.username || acc.name || '') : (acc.name || '');
    var kind = acc.kind || KIND[p] || '';
    if (name) return name + '(' + kind + ')';
    return kind + (acc.id_hint ? ' ' + acc.id_hint : '');
  }

  // ------------------------------------------------------------------ badge
  function statusPill(item) {
    if (!live() || !item || item.status !== 'published') return null;
    var via = String(item.published_via || '');
    if (!/_api$/.test(via) && via !== 'fake') return null;
    var fake = via === 'fake';
    var s = ws.STATUS.published;
    return el('span', {
      class: 'status-pill', 'data-status': 'published', 'data-via': 'api',
      title: fake ? '가짜 게시 모드(테스트용)로 기록했어요. 실제로 올라가지 않았어요.' : '사람이 확인한 뒤 INSIA가 API로 올렸어요'
    }, [el('span', { class: 'sp-dot', 'aria-hidden': 'true' }), s.label + ' · API' + (fake ? ' (가짜)' : '')]);
  }

  // ------------------------------------------------------------------ item block → button state
  function activeAttempt(ctx) {
    var b = block(ctx);
    if (!b) return null;
    var poll = P.polls[ctx.item.id];
    if (poll && poll.attempt && poll.attempt.status === 'sending') return poll.attempt;
    var a = b.active_attempt;
    if (!a || (a.status !== 'sending' && a.status !== 'unknown')) return null;
    var done = a.id ? P.done[a.id] : null;
    if (done) return done.status === 'unknown' ? done : null;   // finished while the detail was still loading
    return a;
  }

  /**
   * Did the person start setting this platform up? LinkedIn: some app info was saved (a missing Client ID/Secret
   * is `not_configured`). Instagram has no app step, so it counts once a token was pasted (an account is known or
   * it needs a new token). Until GET /api/publish arrives, an ambiguous state stays hidden (the reply repaints).
   */
  function platformSetUp(p, state) {
    var ps = platformStatus(p);
    if (p === 'instagram') {
      if (state === 'connected' || state === 'expiring' || state === 'needs_reconnect') return true;
      if (state === 'not_connected' || state === 'not_configured') return false;
      var acc = (ps && ps.account) || {};
      return !!(acc.id_hint || acc.username);   // unavailable: only a connected account gets the (off) button
    }
    if (state !== 'not_configured') return true;
    var app = (ps && ps.app) || {};
    return !!(app.client_id_set || app.client_secret_set);
  }

  /** {show, enabled, note, links} for the API button of this item (DESIGN.md 7-2). */
  function buttonState(ctx) {
    var b = block(ctx);
    var item = ctx.item;
    var p = b.platform;
    var st = item.status;
    var by = b.blocked_by || '';
    var out = { show: false, enabled: false, note: '', link: null, kind: 'info' };
    if (by === 'not_approved' || by === 'archived' || by === 'published') return out;
    if (st !== 'approved' && st !== 'scheduled') return out;
    if (b.state === 'disabled') return out;
    if (!platformSetUp(p, b.state)) return out;   // only the other platform was set up: nothing for this one (7-2)
    out.show = true;
    var active = activeAttempt(ctx);
    if (active) return out;   // progress / 결과 불명 card say the rest
    var conn = { href: '#/brand/connections' };
    if (by === 'version_changed') out.note = '승인한 뒤에 내용이 바뀌었어요. 다시 승인하면 API로 게시할 수 있어요.';
    else if (by === 'published_attempt') { out.note = '게시는 됐지만 보관함 상태를 바꾸지 못했어요. ‘게시 완료 표시’를 눌러 주세요.'; out.kind = 'warn'; }
    else if (by === 'agent_job' || ctx.job) out.note = '에이전트가 이 콘텐츠를 수정하는 중이에요.';
    else if (b.state === 'not_configured') { out.note = label(p) + ' 앱 정보가 없어요.'; out.link = { href: conn.href, text: '연결 설정 열기' }; }
    else if (b.state === 'not_connected') { out.note = label(p) + ' 계정을 연결해야 해요.'; out.link = { href: conn.href, text: label(p) + ' 연결하기' }; }
    else if (b.state === 'needs_reconnect') {
      out.note = p === 'instagram' ? '인스타그램 연결이 끝났어요. 새 토큰을 붙여 넣어 주세요.' : 'LinkedIn 연결이 끝났거나 해제됐어요.';
      out.link = { href: conn.href, text: '다시 연결' };
      out.kind = 'warn';
    } else if (b.state === 'unavailable') {
      var blockers = b.blockers || [];
      if (blockers.indexOf('render_unavailable') >= 0) {
        out.note = '카드 이미지를 그릴 브라우저(Playwright·Chromium)나 한글 글꼴이 없어요.';
        out.link = { href: conn.href, text: '설치 방법' };
      } else if (p === 'instagram' && (blockers.indexOf('public_url_missing') >= 0 || blockers.indexOf('media_port_unavailable') >= 0)) {
        out.note = '인스타그램 API는 이미지를 공개 HTTPS 주소에서 가져가요. 지금 INSIA는 이 컴퓨터에서만 열려 있어서 API로 올릴 수 없어요. ' +
          '아래 ‘카드 이미지 + 캡션’ 묶음을 받아 앱에서 올린 뒤 ‘게시 완료 표시’를 눌러 주세요.';
        out.link = { href: conn.href, text: '이미지만 공개 주소로 여는 방법' };
      } else {
        out.note = b.reason || label(p) + ' API 게시를 지금 쓸 수 없어요.';
        out.link = { href: conn.href, text: '연결 설정 열기' };
      }
    } else if (b.state === 'expiring') {
      var tok = (platformStatus(p) || {}).token || {};
      out.note = b.reason || (typeof tok.days_left === 'number' ? '연결이 ' + tok.days_left + '일 뒤 끝나요.' : '연결이 곧 끝나요.');
      out.link = { href: conn.href, text: '다시 연결' };
      out.kind = 'warn';
    } else if (b.state === 'connected') {
      out.note = accountText(p, (platformStatus(p) || {}).account) + '에 올려요.';
    } else if (b.reason) out.note = b.reason;
    out.enabled = !!b.available && !by && !ctx.job && (b.state === 'connected' || b.state === 'expiring');
    return out;
  }

  function button(ctx) {
    var b = block(ctx);
    if (!b) return null;
    wantStatus(ctx);
    var s = buttonState(ctx);
    if (!s.show) return null;
    var active = activeAttempt(ctx);
    var sending = active && active.status === 'sending';
    return el('button', {
      type: 'button', class: 'btn btn--api', 'data-key': 'act-api-publish', 'data-platform': b.platform, 'aria-haspopup': 'dialog',
      disabled: ctx.busy || ctx.editing || !s.enabled || !!active,
      text: sending ? '게시 중…' : label(b.platform) + '에 API로 게시',
      onclick: function (e) { openDialog(ctx, e.currentTarget); }
    });
  }

  function lock(ctx) {
    var a = activeAttempt(ctx);
    if (!a) return '';
    return a.status === 'sending' ? label(a.platform || block(ctx).platform) + '에 올리는 중이에요. 끝날 때까지 편집·재검수·상태 변경을 할 수 없어요.'
      : '게시 결과를 먼저 정리해 주세요. 정리하기 전에는 편집·재검수·보관을 할 수 없어요.';
  }

  // ------------------------------------------------------------------ section under the action row
  function stepText(a) {
    var step = String(a.step || '');
    var m = /^children (\d+)\/(\d+)$/.exec(step);
    if (m) return '이미지 ' + m[1] + '/' + m[2] + ' 등록';
    return { check: '연결 확인', polling: '인스타그램이 이미지를 처리하는 중', carousel: '캐러셀 만드는 중', write: '게시 요청을 보냈어요', permalink: '게시물 주소를 받는 중' }[step] || step;
  }

  function progressNode(a, p) {
    var pr = a.progress || {};
    var total = Number(pr.total) || 0, done = Math.min(Number(pr.done) || 0, total);
    var step = stepText(a);
    var text = p === 'instagram'
      ? '인스타그램에 올리는 중이에요' + (step ? ' (' + step + ')' : '') + '. 최대 5분쯤 걸릴 수 있어요. 이 창을 닫아도 계속 진행돼요.'
      : 'LinkedIn에 올리는 중이에요… 이 창을 닫아도 계속 진행돼요.';
    return el('div', { class: 'pub-progress', role: 'status', 'data-key': 'api-progress' }, [
      el('p', null, [el('span', { class: 'jb-dot', 'aria-hidden': 'true' }), text]),
      total > 1 ? el('div', { class: 'meter pub-meter', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(total), 'aria-valuenow': String(done), 'aria-label': '게시 진행 ' + done + '/' + total },
        el('span', { class: 'meter-fill', style: 'width:' + Math.round(100 * done / total) + '%' })) : null
    ]);
  }

  function copyRow(text, btnLabel) {
    var status = el('span', { class: 'copy-status', role: 'status' });
    var fallback = el('textarea', { class: 'copy-fallback', rows: '2', readonly: true, 'aria-label': '복사할 내용', hidden: true });
    return el('span', { class: 'pub-copy' }, [
      el('button', { type: 'button', class: 'btn btn--small', text: btnLabel || '복사', onclick: function () { U.copyText(text, status, fallback); } }),
      status, fallback
    ]);
  }

  function linkOut(url) {
    return isHttps(url) ? el('a', { href: url, target: '_blank', rel: 'noopener noreferrer', text: url + ' ↗' }) : el('span', { text: url });
  }

  /** "게시했어요. 게시물 주소를 받지 못했어요." + 주소 입력칸 (PUT /api/publish/attempts/<pa>/permalink). */
  function permalinkForm(ctx, attempt, p) {
    var id = 'pubPermalink';
    var input = el('input', { id: id, type: 'url', inputmode: 'url', placeholder: p === 'instagram' ? 'https://www.instagram.com/p/…' : 'https://www.linkedin.com/feed/update/…', autocomplete: 'off' });
    var err = el('p', { class: 'form-error', role: 'alert', hidden: true });
    var save = el('button', {
      type: 'button', class: 'btn btn--small btn--primary', 'data-key': 'api-permalink-save', text: '저장', onclick: function () {
        var u = input.value.trim();
        if (!isHttps(u)) { err.textContent = 'https://로 시작하는 게시물 주소를 넣어 주세요.'; err.hidden = false; input.focus(); return; }
        save.disabled = true;
        ws.put('/api/publish/attempts/' + encodeURIComponent(attempt.id) + '/permalink', { permalink: u }).then(function () {
          delete P.results[ctx.item.id];
          ws.ui.toast('게시물 주소를 저장했어요.', 'success');
          ctx.refresh();
        }, function (ex) {
          save.disabled = false;
          if (ex.auth) return;
          err.textContent = '저장하지 못했어요: ' + ex.message;
          err.hidden = false;
        });
      }
    });
    return el('div', { class: 'pub-permalink' }, [
      el('label', { class: 'field', for: id }, [el('span', null, ['게시물 주소 ', el('small', { text: '선택 · ' + (p === 'instagram' ? '인스타그램' : 'LinkedIn 내 활동') + '에서 복사해 넣어 주세요' })]), input]),
      err,
      el('div', { class: 'form-foot' }, [save])
    ]);
  }

  function resultNode(ctx, r, p) {
    var a = r.attempt || {};
    var close = el('button', { type: 'button', class: 'btn btn--small btn--ghost notice-action', text: '닫기', onclick: function () { delete P.results[ctx.item.id]; ctx.rerender(); } });
    if (r.status === 'published') {
      var itemFailed = a.item_update_error || (a.state && a.state.item_update_error) || (ctx.item.status !== 'published' && (block(ctx) || {}).blocked_by === 'published_attempt');
      return ws.ui.notice(itemFailed ? 'warn' : 'success', [
        el('div', { class: 'notice-row' }, [el('b', { text: label(p) + '에 게시했어요' + (isFake() ? ' (가짜 게시 모드)' : '') }), close]),
        a.permalink ? el('p', null, ['게시한 주소: ', linkOut(a.permalink)]) : el('p', { text: '게시했어요. 게시물 주소를 받지 못했어요.' }),
        a.permalink && p === 'linkedin' ? el('p', { class: 'notice-foot', text: '링크가 열리지 않으면 LinkedIn 내 활동에서 확인해 주세요.' }) : null,
        itemFailed ? el('p', { text: '게시는 됐지만 보관함 상태를 바꾸지 못했어요. ‘게시 완료 표시’를 눌러 주세요.' }) : null,
        r.firstComment ? el('p', { class: 'pub-first-comment' }, ['이제 첫 댓글로 링크를 달아 주세요: ', el('code', { text: r.firstComment }), ' ', copyRow(r.firstComment)]) : null,
        !a.permalink && a.id ? permalinkForm(ctx, a, p) : null
      ], 'pub-result');
    }
    if (r.status === 'failed') {
      var again = buttonState(ctx);
      return ws.ui.notice('error', [
        el('div', { class: 'notice-row' }, [el('b', { text: '게시하지 못했어요' }), close]),
        el('p', { text: (a.error || '원인을 알 수 없어요.') + ' 아무것도 올라가지 않았어요.' }),
        again.show && again.enabled && !activeAttempt(ctx) ? el('div', { class: 'form-foot' }, el('button', {
          type: 'button', class: 'btn btn--small', 'data-key': 'api-retry', 'aria-haspopup': 'dialog', text: '다시 확인하고 게시',
          disabled: ctx.busy || ctx.editing, onclick: function (e) { openDialog(ctx, e.currentTarget); }
        })) : null,
        el('p', { class: 'notice-foot', text: '다시 게시하면 새 미리보기부터 시작해요. 확인한 뒤에만 올라가요.' })
      ], 'pub-result');
    }
    return null;
  }

  /** 결과 불명 카드 (DESIGN.md 7-5). */
  function unknownCard(ctx, a, p) {
    var itemId = ctx.item.id;
    var step = P.resolving[itemId] || '';
    var err = el('p', { class: 'form-error', role: 'alert', hidden: true });
    function fail(ex) { if (ex.auth) return; err.textContent = ex.message; err.hidden = false; setBusy(false); }
    var buttons = [];
    function setBusy(on) { buttons.forEach(function (b) { b.disabled = on; }); }
    function resolve(outcome, url) {
      setBusy(true);
      var body = { outcome: outcome };
      if (url) body.url = url;
      ws.post('/api/publish/attempts/' + encodeURIComponent(a.id) + '/resolve', body).then(function () {
        delete P.resolving[itemId];
        delete P.polls[itemId];
        delete P.done[a.id];
        delete P.attempts[itemId];
        ws.ui.toast(outcome === 'published' ? '게시된 것으로 정리했어요.' : '올라가지 않은 것으로 정리했어요. 다시 게시하려면 새 미리보기부터 시작해요.', 'success');
        ctx.refresh();
      }, fail);
    }
    function btn(key, text, primary, fn, danger) {
      var b = el('button', { type: 'button', class: 'btn btn--small' + (primary ? ' btn--primary' : '') + (danger ? ' btn--danger' : ''), 'data-key': key, text: text, onclick: fn });
      buttons.push(b);
      return b;
    }
    var nodes = [
      el('b', { text: '게시됐는지 확인하지 못했어요' }),
      el('p', { text: p === 'instagram'
        ? '인스타그램의 응답이 끊겼어요. 게시물이 올라갔을 수도 있어요. ‘인스타그램에서 다시 확인’을 누르거나 인스타그램 앱에서 확인해 주세요.'
        : 'LinkedIn의 응답이 끊겼어요. 글이 올라갔을 수도 있어요. LinkedIn 내 활동에서 확인해 주세요.' }),
      a.error ? el('p', { class: 'notice-foot', text: '기록된 내용: ' + a.error }) : null
    ];
    if (step === 'published') {
      var input = el('input', { id: 'pubResolveUrl', type: 'url', inputmode: 'url', autocomplete: 'off', placeholder: p === 'instagram' ? 'https://www.instagram.com/p/…' : 'https://www.linkedin.com/feed/update/…' });
      nodes.push(el('label', { class: 'field', for: 'pubResolveUrl' }, [el('span', null, ['게시물 주소 ', el('small', { text: '선택' })]), input]));
      nodes.push(err);
      nodes.push(el('div', { class: 'form-foot' }, [
        btn('api-resolve-back', '취소', false, function () { delete P.resolving[itemId]; ctx.rerender(); }),
        btn('api-resolve-published-yes', '올라갔어요로 정리', true, function () {
          var u = input.value.trim();
          if (u && !isHttps(u)) { err.textContent = 'https://로 시작하는 게시물 주소를 넣어 주세요.'; err.hidden = false; input.focus(); return; }
          resolve('published', u);
        })
      ]));
    } else if (step === 'not_published') {
      nodes.push(el('p', { class: 'panel-lead', text: label(p) + (p === 'instagram' ? ' 앱에서 먼저 확인했나요? 같은 게시물이 두 번 올라갈 수 있어요.' : ' 피드에서 먼저 확인했나요? 같은 글이 두 번 올라갈 수 있어요.') }));
      nodes.push(err);
      nodes.push(el('div', { class: 'form-foot' }, [
        btn('api-resolve-back', '취소', false, function () { delete P.resolving[itemId]; ctx.rerender(); }),
        btn('api-resolve-not-yes', '확인했어요, 안 올라갔어요', false, function () { resolve('not_published'); }, true)
      ]));
    } else {
      nodes.push(err);
      nodes.push(el('div', { class: 'form-foot pub-unknown-actions' }, [
        p === 'instagram' ? btn('api-check', '인스타그램에서 다시 확인', true, function () {
          setBusy(true);
          ws.post('/api/publish/attempts/' + encodeURIComponent(a.id) + '/check', {}).then(function (resp) {
            var fresh = ws.unwrap(resp, 'attempt') || {};
            if (fresh.status && fresh.status !== 'unknown') { delete P.done[a.id]; delete P.attempts[itemId]; }
            if (fresh.status === 'published') ws.ui.toast('인스타그램에 올라가 있어요. 게시한 것으로 기록했어요.', 'success');
            else if (fresh.status === 'failed') ws.ui.toast('인스타그램에 올라가지 않았어요. 다시 게시하려면 새 미리보기부터 시작해요.', 'info');
            else ws.ui.toast('아직 확인하지 못했어요. 잠시 뒤 다시 확인하거나 앱에서 확인해 주세요.', 'info');
            ctx.refresh();
          }, fail);
        }) : null,
        btn('api-resolve-published', '올라갔어요 — 주소 넣기(선택)', p !== 'instagram', function () { P.resolving[itemId] = 'published'; ctx.rerender(); focusIn('#pubResolveUrl'); }),
        btn('api-resolve-not', '안 올라갔어요', false, function () { P.resolving[itemId] = 'not_published'; ctx.rerender(); focusIn('[data-key="api-resolve-not-yes"]'); })
      ]));
    }
    return el('div', { class: 'pub-unknown notice', 'data-kind': 'warn', role: 'group', 'aria-label': '게시 결과 확인', 'data-key': 'api-unknown' }, nodes);
  }
  function focusIn(sel) { setTimeout(function () { var n = document.querySelector(sel); if (n) n.focus(); }, 0); }

  function lastAttemptNote(ctx, b, p) {
    var last = b.last_attempt;
    if (!last || !last.status) return null;
    var item = ctx.item;
    if (last.status === 'published') {
      var via = String(item.published_via || '');
      var noAddr = item.status === 'published' && (/_api$/.test(via) || via === 'fake') && !item.published_url;
      if (item.status === 'published' && !noAddr) {
        return p === 'linkedin' && item.published_url ? el('p', { class: 'pub-note', text: 'LinkedIn API로 게시했어요. 링크가 열리지 않으면 LinkedIn 내 활동에서 확인해 주세요.' }) : null;
      }
      if (noAddr && last.id) return ws.ui.notice('info', [el('b', { text: '게시했어요. 게시물 주소를 받지 못했어요.' }), permalinkForm(ctx, last, p)], 'pub-result');
      return null;
    }
    if (last.status === 'failed' && (item.status === 'approved' || item.status === 'scheduled')) {
      return el('p', { class: 'pub-note', 'data-kind': 'error' },
        '마지막 API 게시 시도' + (last.created_at ? '(' + ws.date.dateTime(last.created_at) + ')' : '') + '가 실패했어요: ' + (last.error || '원인을 알 수 없어요.') + ' 아무것도 올라가지 않았어요.');
    }
    if (last.status === 'abandoned' && (item.status === 'approved' || item.status === 'scheduled')) {
      return el('p', { class: 'pub-note', text: '지난 API 게시 시도는 ‘안 올라갔어요’로 정리했어요. 다시 게시하면 새 미리보기부터 시작해요.' });
    }
    return null;
  }

  function section(ctx) {
    var b = block(ctx);
    if (!b) return null;
    wantStatus(ctx);
    var p = b.platform;
    var item = ctx.item;
    var nodes = [];
    var active = activeAttempt(ctx);
    if (active && active.status === 'sending') {
      nodes.push(progressNode(active, p));
      ensurePoll(item.id, active, p, ctx);
    } else if (active && active.status === 'unknown') {
      nodes.push(unknownCard(ctx, active, p));
    }
    var r = P.results[item.id];
    if (r && !(active && active.status === 'unknown')) nodes.push(resultNode(ctx, r, p));
    else if (!active) nodes.push(lastAttemptNote(ctx, b, p));
    var s = buttonState(ctx);
    if (s.show && s.note && !active) {
      nodes.push(el('p', { class: 'pub-note', 'data-kind': s.kind, 'data-key': 'api-note' }, [
        s.note, s.link ? ' ' : null, s.link ? el('a', { href: s.link.href, text: s.link.text }) : null
      ]));
    }
    if (b.last_attempt || b.active_attempt || r) {
      nodes.push(el('div', { class: 'pub-history-toggle' }, el('button', {
        type: 'button', class: 'btn btn--small btn--ghost', 'data-key': 'api-attempts', 'aria-expanded': String(ctx.getPanel() === 'api_attempts'),
        text: ctx.getPanel() === 'api_attempts' ? 'API 게시 기록 닫기' : 'API 게시 기록',
        onclick: function () { ctx.setPanel(ctx.getPanel() === 'api_attempts' ? '' : 'api_attempts'); }
      })));
    }
    nodes = nodes.filter(Boolean);
    if (!nodes.length) return null;
    return el('div', { class: 'pub-section', 'data-platform': p }, nodes);
  }

  // ------------------------------------------------------------------ 게시 기록 (panel 'api_attempts')
  function whoText(by) {
    var s = String(by || '');
    var m = /^dashboard@(.+)$/.exec(s);
    if (m) return '대시보드(' + m[1] + ')';
    m = /^cli:(.+)$/.exec(s);
    if (m) return '터미널(' + m[1] + ')';
    return s;
  }
  function panel(key, ctx) {
    if (key === 'api_publish') return null;   // the dialog itself
    if (key !== 'api_attempts' || !block(ctx)) return null;
    var id = ctx.item.id;
    var list = P.attempts[id];
    var body;
    if (list === undefined) {
      P.attempts[id] = null;
      ws.get(pid(id) + '/publish').then(function (r) { P.attempts[id] = ws.listOf(r, 'attempts'); ctx.rerender(); },
        function (err) { P.attempts[id] = err; ctx.rerender(); });
      body = ws.ui.loading('게시 기록을 불러오는 중이에요…');
    } else if (list === null) {
      body = ws.ui.loading('게시 기록을 불러오는 중이에요…');
    } else if (list instanceof Error) {
      body = ws.ui.errorState(list, function () { delete P.attempts[id]; ctx.rerender(); });
    } else if (!list.length) {
      body = el('p', { class: 'empty', text: '아직 API로 게시한 기록이 없어요.' });
    } else {
      body = el('ol', { class: 'pub-attempts' }, list.map(function (a) {
        return el('li', { class: 'pub-attempt', 'data-status': a.status }, [
          el('div', { class: 'pa-main' }, [
            el('span', { class: 'pa-status', 'data-status': a.status, text: STATUS_LABEL[a.status] || a.status }),
            el('span', { class: 'pa-meta', text: [label(a.platform), 'v' + (a.version || '?'), whoText(a.requested_by)].filter(Boolean).join(' · ') }),
            el('span', { class: 'pa-time', text: ws.date.dateTime(a.created_at) })
          ]),
          a.permalink ? el('p', { class: 'pa-link' }, linkOut(a.permalink)) : null,
          a.error ? el('p', { class: 'pa-error', text: a.error }) : null,
          a.resolved_by ? el('p', { class: 'pa-meta', text: '정리: ' + whoText(a.resolved_by) }) : null
        ]);
      }));
    }
    return el('div', { class: 'action-panel', 'data-panel': 'api_attempts' }, [
      el('p', { class: 'panel-lead', text: 'API 게시 기록 · 시각, 결과, 게시물 주소, 누가 눌렀는지' }),
      body
    ]);
  }

  // ------------------------------------------------------------------ polling an attempt
  function ensurePoll(itemId, attempt, platform, ctx) {
    var poll = P.polls[itemId];
    if (poll && poll.attemptId === attempt.id) { poll.ctx = ctx; return; }
    if (poll && poll.timer) clearTimeout(poll.timer);
    P.polls[itemId] = { attemptId: attempt.id, platform: platform, attempt: attempt, timer: null, n: 0, ctx: ctx, errors: 0 };
    schedulePoll(itemId);
  }
  function schedulePoll(itemId) {
    var poll = P.polls[itemId];
    if (!poll) return;
    var wait = POLL_MS[Math.min(poll.n, POLL_MS.length - 1)] * (poll.errors ? 2 : 1);
    poll.timer = setTimeout(function () { pollOnce(itemId); }, wait);
  }
  function pollOnce(itemId) {
    var poll = P.polls[itemId];
    if (!poll || !live()) return;
    poll.n++;
    ws.get('/api/publish/attempts/' + encodeURIComponent(poll.attemptId)).then(function (resp) {
      var a = ws.unwrap(resp, 'attempt') || {};
      if (P.polls[itemId] !== poll) return;
      poll.errors = 0;
      poll.attempt = a;
      if (a.status === 'sending') {
        schedulePoll(itemId);
        if (poll.ctx) poll.ctx.rerender();
        return;
      }
      finishPoll(itemId, poll, a);
    }, function (err) {
      if (P.polls[itemId] !== poll) return;
      if (err.status === 404 || err.auth) { delete P.polls[itemId]; return; }
      poll.errors++;
      schedulePoll(itemId);
    });
  }
  function finishPoll(itemId, poll, a) {
    var p = a.platform || poll.platform;
    if (poll.timer) clearTimeout(poll.timer);
    delete P.polls[itemId];
    delete P.attempts[itemId];
    if (a.id) P.done[a.id] = a;
    if (a.status === 'published') {
      P.results[itemId] = { status: 'published', attempt: a, platform: p, firstComment: poll.firstComment || '' };
      ws.ui.toast(label(p) + '에 게시했어요.' + (isFake() ? ' (가짜 게시 모드: 실제로 올라가지 않았어요)' : ''), 'success');
    } else if (a.status === 'failed') {
      P.results[itemId] = { status: 'failed', attempt: a, platform: p };
      ws.ui.toast('게시하지 못했어요: ' + (a.error || '원인을 알 수 없어요.'), 'error');
    } else if (a.status === 'unknown') {
      ws.ui.toast('게시됐는지 확인하지 못했어요. 보관함에서 결과를 정리해 주세요.', 'error');
    }
    if (poll.ctx) poll.ctx.refresh();
  }

  // ------------------------------------------------------------------ confirm dialog (<dialog> + showModal)
  function ensureDialog() {
    var d = document.getElementById('pubDialog');
    if (d) return d;
    d = el('dialog', { class: 'sheet pub-dialog', id: 'pubDialog', 'aria-labelledby': 'pubTitle' });
    d.addEventListener('click', function (e) { if (e.target === d) d.close(); });
    d.addEventListener('close', onDialogClose);
    document.body.appendChild(d);
    return d;
  }

  function openDialog(ctx, trigger) {
    var b = block(ctx);
    if (!b || !live()) return;
    var p = b.platform;
    if (P.dlg && P.dlg.timer) clearTimeout(P.dlg.timer);
    P.dlg = {
      ctx: ctx, itemId: ctx.item.id, platform: p, triggerKey: 'act-api-publish', trigger: trigger || null,
      visibility: 'PUBLIC', ai: null, preview: null, loading: false, error: null, errorData: null,
      checked: false, sending: false, sendError: '', sendErrorData: null, expired: false, seq: 0, timer: null
    };
    ctx.setPanel('api_publish');
    var d = ensureDialog();
    renderDialog();
    if (!d.open) d.showModal();
    var t = document.getElementById('pubTitle');
    if (t) t.focus();
    if (p === 'linkedin') requestPreview();   // Instagram waits for the AI label choice (DESIGN.md 14.2)
    loadStatus(false).catch(function () { /* banner only */ });
  }

  function onDialogClose() {
    var d = document.getElementById('pubDialog');
    // the close event arrives as a task: when the dialog was opened again meanwhile, that newer dialog owns P.dlg
    if (d && d.open) return;
    var s = P.dlg;
    if (!s) return;
    if (s.timer) clearTimeout(s.timer);
    P.dlg = null;
    var ctx = s.ctx;
    if (ctx && ctx.getPanel() === 'api_publish') ctx.setPanel('');
    // the action card may have been re-rendered while the dialog was open: find the button again
    var t = s.trigger && document.contains(s.trigger) ? s.trigger : document.querySelector('[data-key="' + s.triggerKey + '"]');
    if (t && !t.disabled) t.focus();
    else { var h = document.getElementById('actTitle'); if (h) { h.setAttribute('tabindex', '-1'); h.focus(); } }
  }

  function closeDialog() { var d = document.getElementById('pubDialog'); if (d && d.open) d.close(); }

  function previewOptions(s) {
    return s.platform === 'instagram' ? { is_ai_generated: s.ai === 'yes' } : { visibility: s.visibility };
  }

  function requestPreview() {
    var s = P.dlg;
    if (!s) return;
    if (s.platform === 'instagram' && s.ai !== 'yes' && s.ai !== 'no') return;
    var seq = ++s.seq;
    s.loading = true;
    s.preview = null;
    s.error = null;
    s.errorData = null;
    s.checked = false;
    s.expired = false;
    s.sendError = '';
    if (s.timer) { clearTimeout(s.timer); s.timer = null; }
    renderDialog();
    ws.post(pid(s.itemId) + '/publish/preview', { platform: s.platform, options: previewOptions(s) }).then(function (pv) {
      if (P.dlg !== s || s.seq !== seq) return;   // closed, or the options changed meanwhile
      s.loading = false;
      s.preview = pv || null;
      armExpiry(s);
      renderDialog();
    }, function (ex) {
      if (P.dlg !== s || s.seq !== seq) return;
      s.loading = false;
      if (ex.auth) { closeDialog(); return; }
      s.error = ex.message;
      s.errorData = ex.data || null;
      renderDialog();
    });
  }

  function armExpiry(s) {
    var pv = s.preview;
    if (!pv || !pv.expires_at) return;
    var left = Date.parse(pv.expires_at) - Date.now();
    if (isNaN(left)) return;
    if (left <= 0) { s.expired = true; return; }
    s.timer = setTimeout(function () {
      if (P.dlg !== s) return;
      s.expired = true;
      s.checked = false;
      renderDialog();
    }, Math.min(left, 2147483000));
  }

  function canSend(s) {
    return !!(s && s.preview && s.preview.can_publish !== false && !(s.preview.errors || []).length && s.checked && !s.expired && !s.sending && !s.loading);
  }

  function send() {
    var s = P.dlg;
    var btn = document.getElementById('pubSend');
    if (!canSend(s)) return;
    s.sending = true;
    if (btn) { btn.disabled = true; btn.textContent = '보내는 중…'; }   // off at once: a second click never sends twice
    var pv = s.preview;
    ws.post(pid(s.itemId) + '/publish', { platform: s.platform, preview_id: pv.preview_id, preview_hash: pv.preview_hash, confirm: true }).then(function (resp) {
      var a = (resp && resp.attempt) || {};
      var ctx = s.ctx;
      var itemId = s.itemId;
      delete P.results[itemId];
      delete P.attempts[itemId];
      if (a.id) {
        var old = P.polls[itemId];
        if (old && old.timer) clearTimeout(old.timer);
        delete P.polls[itemId];
        ensurePoll(itemId, a.status ? a : Object.assign({ status: 'sending' }, a), s.platform, ctx);
        P.polls[itemId].firstComment = pv.first_comment_link || '';
        if (a.status && a.status !== 'sending') finishPoll(itemId, P.polls[itemId], a);
      }
      closeDialog();
      if (ctx) ctx.rerender();
    }, function (ex) {
      if (P.dlg !== s) return;
      s.sending = false;
      if (ex.auth) { closeDialog(); return; }
      s.sendError = ex.message;
      s.sendErrorData = ex.data || null;
      var code = ex.data && ex.data.code;
      if (code === 'preview_expired' || code === 'changed') { s.expired = true; s.checked = false; }
      renderDialog();
      var e = document.getElementById('pubSendError');
      if (e) e.focus();
    });
  }

  function closeIcon() {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M6 6l12 12M18 6L6 18');
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '2');
    path.setAttribute('stroke-linecap', 'round');
    svg.appendChild(path);
    return svg;
  }

  /** "알아 두세요": the server's notices first, then the fixed ones from DESIGN.md 7-3 it did not already say. */
  function noticeList(s) {
    var p = s.platform;
    var pv = s.preview || {};
    var server = (pv.notices || []).filter(function (n) { return n && n.message; });
    var all = server.map(function (n) { return n.message; }).join('\n');
    var codes = server.map(function (n) { return n.code; });
    var fixed = p === 'instagram' ? [
      ['no_delete', /지울 수 없/, '인스타그램 API로 올린 게시물은 INSIA에서 지울 수 없어요. 잘못 올렸다면 인스타그램 앱에서 직접 삭제해야 해요.'],
      ['no_tags', /사람 태그/, '사람 태그·공동 작업자·유료 파트너십 표시는 API로 넣을 수 없어요. 필요하면 앱에서 올려 주세요.'],
      ['one_post', /예약/, '지금 이 게시물 한 건만 올려요. INSIA는 예약·반복 게시를 하지 않아요.'],
      ['manual_done', /게시 완료 표시/, '이미 인스타그램 앱에서 직접 올렸다면 여기서 게시하지 말고 ‘게시 완료 표시’를 눌러 주세요.']
    ] : [
      ['one_post', /예약/, '지금 이 글 한 건만 올려요. INSIA는 예약·반복 게시를 하지 않아요. LinkedIn API 이용약관이 자동 게시를 금지하기 때문이에요.'],
      ['edit_on_platform', /고치거나 지우/, '올린 뒤 고치거나 지우려면 LinkedIn에서 직접 해야 해요.'],
      ['manual_done', /게시 완료 표시/, '이미 LinkedIn에 직접 올렸다면 여기서 게시하지 말고 ‘게시 완료 표시’를 눌러 주세요.']
    ];
    var items = server.map(function (n) {
      return el('li', { 'data-code': n.code || null, 'data-strong': n.code === 'no_delete' ? 'true' : null, text: n.message });
    });
    fixed.forEach(function (f) {
      if (codes.indexOf(f[0]) >= 0 || f[1].test(all)) return;
      items.push(el('li', { 'data-code': f[0], 'data-strong': f[0] === 'no_delete' ? 'true' : null, text: f[2] }));
    });
    if (p === 'linkedin' && pv.first_comment_link && !/첫 댓글/.test(all)) {
      items.push(el('li', { 'data-code': 'first_comment' }, [
        '링크는 본문에 넣지 않았어요. 게시한 뒤 첫 댓글로 직접 달아 주세요: ', el('code', { text: pv.first_comment_link }), ' ', copyRow(pv.first_comment_link)
      ]));
    }
    return el('div', { class: 'pub-notices' }, [el('h3', { class: 'sub-title', text: '알아 두세요' }), el('ul', null, items)]);
  }

  function issueList(list, level) {
    list = (list || []).filter(function (x) { return x && x.message; });
    if (!list.length) return null;
    var isErr = level === 'error';
    return el('div', { class: 'pub-issues', 'data-level': level, role: isErr ? 'alert' : null }, [
      el('h3', { class: 'sub-title', text: isErr ? '고칠 부분' : '확인할 부분' }),
      el('ul', null, list.map(function (x) { return el('li', { 'data-code': x.code || null, text: x.message }); })),
      isErr ? el('p', { class: 'pub-issues-foot' }, [
        '고칠 부분이 있으면 게시할 수 없어요. ',
        el('button', { type: 'button', class: 'linkish', 'data-key': 'pub-edit', text: '편집하기', onclick: function () { var ctx = P.dlg && P.dlg.ctx; closeDialog(); if (ctx && ctx.edit) ctx.edit(); } }),
        '로 고친 뒤 다시 승인해 주세요.'
      ]) : null
    ]);
  }

  function slidesGrid(pv) {
    var slides = pv.slides || [];
    if (!slides.length) return null;
    return el('div', { class: 'pub-slides-wrap' }, [
      el('h3', { class: 'sub-title', text: '보낼 카드 이미지 ' + slides.length + '장 (1080×1350)' }),
      el('ol', { class: 'pub-slides' }, slides.map(function (sl) {
        var src = /^\/api\/publish\/previews\//.test(String(sl.url || '')) ? sl.url : '';
        var img = src ? el('img', { src: src, alt: sl.alt || ('슬라이드 ' + sl.n), width: '108', height: '135', loading: 'lazy', decoding: 'async' }) : el('span', { class: 'pub-slide-missing', text: '이미지 없음' });
        var zoom = el('button', {
          type: 'button', class: 'pub-slide-btn', 'aria-label': '슬라이드 ' + sl.n + ' 크게 보기 (대체텍스트 전체 보기)', title: '눌러서 크게 보기 · 대체텍스트 전체', 'aria-pressed': 'false', onclick: function () {
            var li = zoom.parentNode;
            var on = !li.classList.contains('is-zoomed');
            li.classList.toggle('is-zoomed', on);
            zoom.setAttribute('aria-pressed', String(on));
          }
        }, img);
        return el('li', { class: 'pub-slide' }, [
          zoom,
          el('span', { class: 'pub-slide-n', text: String(sl.n) }),
          el('p', { class: 'pub-alt' }, sl.alt ? [el('b', { text: '대체텍스트 ' }), sl.alt] : [el('b', { text: '대체텍스트 없음' })])
        ]);
      }))
    ]);
  }

  function renderDialog() {
    var s = P.dlg;
    var d = document.getElementById('pubDialog');
    if (!s || !d) return;
    var keep = document.activeElement && d.contains(document.activeElement) ? (document.activeElement.id || document.activeElement.getAttribute('data-key')) : null;
    var p = s.platform;
    var pv = s.preview;
    var body = [];

    if (isFake()) body.push(ws.ui.notice('warn', [el('b', { text: '가짜 게시 모드(테스트용)' }), ' 실제로 올라가지 않아요.'], 'pub-fake'));

    // options first: they decide what the preview is
    if (p === 'instagram') {
      var aiName = 'pubAi';
      body.push(el('fieldset', { class: 'pub-options field', 'data-key': 'pub-ai' }, [
        el('legend', { text: '‘AI 정보’ 라벨' }),
        el('p', { class: 'field-hint', id: 'pubAiHint', text: '게시마다 직접 골라 주세요. 고르면 그 설정으로 미리보기를 만들어요. 바꾸면 미리보기를 다시 만들어요.' }),
        el('div', { class: 'pub-radios', role: 'radiogroup', 'aria-describedby': 'pubAiHint' }, [['yes', '붙여요 — AI로 만든 콘텐츠라고 표시'], ['no', '붙이지 않아요']].map(function (o) {
          var id = 'pubAi-' + o[0];
          var r = el('input', { type: 'radio', name: aiName, id: id, value: o[0], checked: s.ai === o[0], disabled: s.sending || s.loading });
          r.addEventListener('change', function () { if (r.checked && s.ai !== o[0] && !s.loading) { s.ai = o[0]; requestPreview(); } });
          return el('label', { class: 'check', for: id }, [r, ' ' + o[1]]);
        }))
      ]));
    } else {
      body.push(el('fieldset', { class: 'pub-options field', 'data-key': 'pub-vis' }, [
        el('legend', { text: '공개 범위' }),
        el('div', { class: 'pub-radios', role: 'radiogroup' }, VIS.map(function (o) {
          var id = 'pubVis-' + o[0];
          var r = el('input', { type: 'radio', name: 'pubVis', id: id, value: o[0], checked: s.visibility === o[0], disabled: s.sending || s.loading });
          r.addEventListener('change', function () { if (r.checked && s.visibility !== o[0] && !s.loading) { s.visibility = o[0]; requestPreview(); } });
          return el('label', { class: 'check', for: id }, [r, ' ' + o[1]]);
        })),
        el('p', { class: 'field-hint', text: '바꾸면 미리보기를 다시 만들어요.' })
      ]));
    }

    if (p === 'instagram' && s.ai !== 'yes' && s.ai !== 'no') {
      body.push(el('p', { class: 'pub-wait', role: 'status', text: '‘AI 정보’ 라벨을 붙일지 먼저 골라 주세요. 고르기 전에는 미리보기를 만들지 않고 게시할 수도 없어요.' }));
    } else if (s.loading) {
      body.push(el('p', { class: 'loading pub-loading', role: 'status', text: p === 'instagram' ? '카드 이미지를 그리는 중이에요. 10~30초 걸려요.' : '미리보기를 만드는 중이에요…' }));
    } else if (s.error) {
      body.push(el('div', { class: 'pub-issues', 'data-level': 'error', role: 'alert' }, [
        el('h3', { class: 'sub-title', text: '미리보기를 만들지 못했어요' }),
        el('p', { text: s.error }),
        s.errorData && s.errorData.code && ['not_connected', 'reconnect', 'not_configured', 'unavailable'].indexOf(s.errorData.code) >= 0
          ? el('p', null, el('a', { href: '#/brand/connections', onclick: function () { closeDialog(); }, text: '연결 설정 열기' })) : null,
        el('div', { class: 'form-foot' }, el('button', { type: 'button', class: 'btn btn--small', 'data-key': 'pub-retry', text: '다시 시도', onclick: requestPreview }))
      ]));
    } else if (pv) {
      var content = pv.content || {};
      var acc = pv.account || {};
      body.push(el('dl', { class: 'pub-facts' }, [
        el('div', null, [el('dt', { text: '계정' }), el('dd', { text: [acc.name || acc.username || '', acc.kind || KIND[p]].filter(Boolean).join(' · ') + (acc.id_hint && !(acc.name || acc.username) ? ' ' + acc.id_hint : '') })]),
        pv.item ? el('div', null, [el('dt', { text: '콘텐츠' }), el('dd', { text: 'v' + (pv.item.version || '?') + ' · ' + (pv.item.title || '(제목 없음)') + (typeof pv.item.approved_score === 'number' ? ' · 검수 ' + pv.item.approved_score + '점' : '') + (pv.item.approval_forced ? ' · 강제 승인' : '') })]) : null,
        p === 'instagram' && pv.quota && typeof pv.quota.total === 'number' ? el('div', null, [el('dt', { text: '오늘 한도' }), el('dd', { text: '남은 게시 ' + num(pv.quota.total - (pv.quota.used || 0)) + '/' + num(pv.quota.total) + '개' })]) : null
      ]));
      if (p === 'instagram') body.push(slidesGrid(pv));
      var tags = content.hashtags || [];
      body.push(el('div', { class: 'pub-content' }, [
        el('div', { class: 'pub-content-head' }, [
          el('h3', { class: 'sub-title', text: p === 'instagram' ? '캡션' : '게시될 글' }),
          el('span', { class: 'fold-meta', text: num(content.chars) + ' / ' + num(content.limit || (p === 'instagram' ? 2200 : 3000)) + '자' + (p === 'instagram' ? ' · 해시태그 ' + tags.length + ' / ' + IG_MAX_TAGS + '개' : '') })
        ]),
        el('div', { class: 'pub-text', tabindex: '0', role: 'region', 'aria-label': p === 'instagram' ? '보낼 캡션' : '보낼 글', text: content.text || '' }),
        tags.length ? el('div', { class: 'hashtags' }, tags.map(function (h) { return el('span', { text: h }); })) : null
      ]));
      body.push(issueList(pv.errors, 'error'));
      body.push(issueList(pv.warnings, 'warning'));
      body.push(noticeList(s));
      if (pv.request_preview && pv.request_preview.length) {
        body.push(el('details', { class: 'pub-request' }, [
          el('summary', { text: '보낼 요청 보기(개발자용)' }),
          el('pre', { tabindex: '0', text: JSON.stringify(pv.request_preview, null, 2) })
        ]));
      }
      if (s.expired) {
        body.push(ws.ui.notice('warn', [
          el('b', { text: '미리보기가 만료됐어요' }), ' · 확인한 뒤 30분이 지났거나 그 사이 내용이 바뀌었어요. ',
          el('button', { type: 'button', class: 'btn btn--small notice-action', 'data-key': 'pub-remake', text: '다시 만들기', onclick: requestPreview })
        ]));
      }
    }

    var confirmText = p === 'instagram' ? '슬라이드·캡션·계정을 확인했어요. 지울 수 없다는 것도 알아요. 지금 이 한 건을 올려요.'
      : '게시될 내용과 계정을 확인했어요. 지금 이 한 건을 올려요.';
    var previewReady = !!(pv && !s.loading && !s.error && pv.can_publish !== false && !(pv.errors || []).length && !s.expired);
    var box = el('input', { type: 'checkbox', id: 'pubConfirm', checked: s.checked, disabled: !previewReady || s.sending });
    box.addEventListener('change', function () {
      s.checked = box.checked;
      var b = document.getElementById('pubSend');
      if (b) b.disabled = !canSend(s);
    });
    var sendErr = s.sendError ? el('p', { class: 'form-error', id: 'pubSendError', tabindex: '-1', role: 'alert', text: s.sendError }) : null;

    d.textContent = '';
    U.appendChildren(d, [
      el('header', { class: 'sheet-head' }, [
        el('div', null, [
          el('p', { class: 'eyebrow', text: 'API 게시 · 한 건' }),
          el('h2', { id: 'pubTitle', tabindex: '-1', text: label(p) + '에 게시하기 전에 확인해 주세요' })
        ]),
        el('button', { type: 'button', class: 'icon-btn', 'aria-label': '닫기', onclick: closeDialog }, closeIcon())
      ]),
      el('div', { class: 'pub-body' }, body.filter(Boolean)),
      el('footer', { class: 'pub-foot' }, [
        el('label', { class: 'check pub-confirm', for: 'pubConfirm' }, [box, ' ', el('span', { text: confirmText })]),
        sendErr,
        el('div', { class: 'sheet-foot' }, [
          el('button', { type: 'button', class: 'btn', 'data-key': 'pub-cancel', text: '취소', onclick: closeDialog }),
          el('button', { type: 'button', class: 'btn btn--primary', id: 'pubSend', disabled: !canSend(s), text: s.sending ? '보내는 중…' : label(p) + '에 지금 게시', onclick: send })
        ])
      ])
    ]);
    if (keep) {
      var n = document.getElementById(keep) || d.querySelector('[data-key="' + keep + '"]');
      if (n && !n.disabled) n.focus();
      else { var t = document.getElementById('pubTitle'); if (t) t.focus(); }
    }
  }

  // ------------------------------------------------------------------ 브랜드·자료 → "API 게시 연결"
  function connBox() { var b = P.conn.box; return b && document.contains(b) ? b : document.getElementById('brandConn'); }

  function connectionsCard(box, opts) {
    opts = opts || {};
    if (!box) return;
    P.conn.box = box;
    if (!live()) { box.hidden = true; box.textContent = ''; return; }
    var param = String(opts.param || '');
    var m = /^connections\/linkedin\/([a-z_]+)$/.exec(param);
    if (m) {
      var r = CALLBACK[m[1]];
      if (r) {
        ws.ui.toast(r[1], r[0]);
        P.conn.msgs = [{ kind: r[0], text: r[1], redirect: m[1] === 'exchange_failed' }];
      }
      try { if (history.replaceState) history.replaceState(null, '', '#/brand/connections'); } catch (e) { /* sandboxed */ }
      if (ws.route && ws.route.view === 'brand') ws.route.param = 'connections';
    }
    var wantFocus = /^connections/.test(param) && opts.focus !== false;
    renderConn();
    var fresh = m || opts.reload || !P.status || Date.now() - P.statusAt > 5000;
    if (fresh) {
      loadStatus(true).then(function () { P.conn.error = null; renderConn(); if (wantFocus) focusConn(); },
        function (err) { P.conn.error = err; renderConn(); });
    } else if (wantFocus) focusConn();
  }
  function focusConn() {
    var t = document.getElementById('pubConnTitle');
    if (!t) return;
    if (t.scrollIntoView) t.scrollIntoView({ block: 'start', behavior: U.reduceMotion() ? 'auto' : 'smooth' });
    try { t.focus({ preventScroll: true }); } catch (e) { t.focus(); }
  }

  function badge(kind, text) { return el('span', { class: 'pub-badge', 'data-kind': kind, text: text }); }
  function stateBadge(p, ps) {
    var st = ps.state;
    var tok = ps.token || {};
    var acc = ps.account || {};
    var days = typeof tok.days_left === 'number' ? tok.days_left + '일 남음' : '';
    var who = p === 'instagram' ? (acc.username || '') : (acc.name || '');
    if (st === 'connected') return badge('ok', ['연결됨', who, days, p === 'instagram' && tok.auto_refresh ? '자동 갱신 켜짐' : ''].filter(Boolean).join(' · '));
    if (st === 'expiring') return badge('warn', ['곧 만료', who, days].filter(Boolean).join(' · '));
    if (st === 'needs_reconnect') return badge('error', '다시 연결 필요');
    if (st === 'unavailable') return badge('warn', (acc.id_hint || who ? '연결됨 · ' : '') + '지금은 쓸 수 없음');
    if (st === 'disabled') return badge('off', '꺼짐');
    if (st === 'not_configured') return badge('off', '앱 정보 없음');
    return badge('off', '연결 안 됨');
  }

  function connMsg(n) {
    return ws.ui.notice(n.kind, [n.text, n.redirect ? el('p', null, ['지금 INSIA가 쓰는 주소: ', el('code', { text: ((platformStatus('linkedin') || {}).app || {}).redirect_uri || '' })]) : null]);
  }

  function renderConn() {
    var box = connBox();
    if (!box) return;
    var st = P.status;
    var keep = document.activeElement && box.contains(document.activeElement) ? (document.activeElement.id || document.activeElement.getAttribute('data-key')) : null;
    if (P.conn.error && (P.conn.error.status === 404 || P.conn.error.auth)) { box.hidden = true; box.textContent = ''; return; }
    if (st && st.enabled === false) { box.hidden = true; box.textContent = ''; return; }   // INSIA_PUBLISH=0: no card at all
    box.textContent = '';
    box.hidden = false;
    var nodes = [
      el('div', { class: 'docs-head' }, [
        el('h3', { class: 'card-title', id: 'pubConnTitle', tabindex: '-1', text: 'API 게시 연결' }),
        st ? el('span', { class: 'fold-meta', text: st.configured ? '설정됨' : '선택 기능' }) : null
      ])
    ];
    if (st && st.fake) nodes.push(ws.ui.notice('warn', [el('b', { text: '가짜 게시 모드(테스트용)' }), ' 실제로 올라가지 않아요. 임시 워크스페이스에서만 켜져요.'], 'pub-fake'));
    nodes.push(el('p', { class: 'card-sub', text: 'LinkedIn·인스타그램에 버튼 한 번으로 올리려면 내 개발자 앱을 연결해요. 연결하지 않아도 지금처럼 파일을 받아 직접 올리면 돼요. INSIA는 사람이 확인하고 누를 때만 한 건씩 올려요.' }));
    P.conn.msgs.forEach(function (n) { nodes.push(connMsg(n)); });
    if (P.conn.error && !st) nodes.push(ws.ui.errorState(P.conn.error, function () { P.conn.error = null; connectionsCard(connBox(), { reload: true }); }));
    else if (!st) nodes.push(ws.ui.loading('연결 상태를 불러오는 중이에요…'));
    else {
      nodes.push(linkedinRow(platformStatus('linkedin') || { state: 'not_configured', app: {} }));
      nodes.push(instagramRow(platformStatus('instagram') || { state: 'disabled' }, st.media || {}));
      nodes.push(el('p', { class: 'pub-docs-note', text: '화면별 설정 순서와 문제 해결은 운영 안내(docs/operations.md)의 ‘API 게시’ 절에 있어요. 스크립트나 예약으로 게시하는 것은 LinkedIn API 약관 위반이라 INSIA는 그런 경로를 만들지 않아요.' }));
    }
    U.appendChildren(box, nodes);
    if (keep) { var n = document.getElementById(keep) || box.querySelector('[data-key="' + keep + '"]'); if (n && !n.disabled) n.focus(); }
  }

  function connError(msg) { P.conn.msgs = [{ kind: 'error', text: msg }]; renderConn(); }

  function refreshAfter(changeMsg, kind) {
    P.status = null;
    if (changeMsg) P.conn.msgs = [{ kind: kind || 'success', text: changeMsg }];
    return loadStatus(true).then(function () { P.conn.error = null; renderConn(); }, function (err) { P.conn.error = err; renderConn(); });
  }

  function disconnectControls(p) {
    var confirming = P.conn.confirm === p;
    if (confirming) {
      return [
        el('span', { class: 'confirm-text', text: label(p) + ' 연결을 해제할까요?' }),
        el('button', {
          type: 'button', class: 'btn btn--small btn--danger', 'data-key': 'conn-disc-yes-' + p, text: '해제', disabled: !!P.conn.busy, onclick: function () {
            P.conn.busy = 'disconnect';
            renderConn();
            ws.del('/api/publish/' + p).then(function (resp) {
              P.conn.busy = '';
              P.conn.confirm = '';
              var hint = resp && resp.revoke_hint ? resp.revoke_hint : (p === 'instagram'
                ? '인스타그램 설정 → 앱 및 웹사이트에서 앱 권한도 지울 수 있어요.'
                : 'LinkedIn 설정 → 데이터 개인정보 → 권한 있는 서비스에서 앱 권한도 지울 수 있어요.');
              ws.ui.toast('INSIA에서 ' + label(p) + ' 토큰을 지웠어요.', 'success');
              refreshAfter('INSIA에서 토큰을 지웠어요. ' + hint);
            }, function (ex) {
              P.conn.busy = '';
              P.conn.confirm = '';
              if (!ex.auth) connError('연결을 해제하지 못했어요: ' + ex.message);
            });
          }
        }),
        el('button', { type: 'button', class: 'btn btn--small', 'data-key': 'conn-disc-no-' + p, text: '취소', onclick: function () { P.conn.confirm = ''; renderConn(); } })
      ];
    }
    return [el('button', { type: 'button', class: 'btn btn--small btn--ghost', 'data-key': 'conn-disc-' + p, text: '연결 해제', disabled: !!P.conn.busy, onclick: function () { P.conn.confirm = p; renderConn(); var n = document.querySelector('[data-key="conn-disc-yes-' + p + '"]'); if (n) n.focus(); } })];
  }

  function suggestedRedirect() {
    var o = location.origin || (location.protocol + '//' + location.host);
    var host = location.hostname;
    // http is only offered for localhost: LinkedIn documents it by example, 127.0.0.1 / ::1 are not confirmed
    if (location.protocol === 'https:' || (location.protocol === 'http:' && host === 'localhost')) return o + '/oauth/linkedin/callback';
    return '';
  }

  function linkedinRow(ps) {
    var app = ps.app || {};
    var envApp = app.source === 'env';
    var connected = ps.state === 'connected' || ps.state === 'expiring' || ps.state === 'needs_reconnect' || (ps.state === 'unavailable' && ps.account);
    var hasApp = !!(app.client_id_set && app.client_secret_set);
    var f = P.conn.form;
    var redirect = app.redirect_uri || '';
    var appOpen = !!f.appOpen;   // folded until the person opens it: most people never set API publishing up

    var idInput = el('input', { id: 'connLiId', type: 'text', autocomplete: 'off', spellcheck: 'false', value: f.clientId || '', readonly: envApp, placeholder: app.client_id_set ? '저장됨 (바꿀 때만 입력)' : '예: 86abc123def456' });
    idInput.addEventListener('input', function () { f.clientId = idInput.value; });
    var secInput = el('input', { id: 'connLiSecret', type: 'password', autocomplete: 'off', spellcheck: 'false', value: f.clientSecret || '', readonly: envApp, placeholder: app.client_secret_set ? '저장됨 (다시 보여 주지 않아요)' : 'Primary Client Secret' });
    secInput.addEventListener('input', function () { f.clientSecret = secInput.value; });
    var redirInput = el('input', { id: 'connLiRedirect', type: 'url', autocomplete: 'off', spellcheck: 'false', value: f.redirect !== undefined ? f.redirect : redirect, readonly: envApp || !P.conn.editRedirect });
    redirInput.addEventListener('input', function () { f.redirect = redirInput.value; });
    var redirStatus = el('span', { class: 'copy-status', role: 'status' });
    var redirFallback = el('textarea', { class: 'copy-fallback', rows: '2', readonly: true, 'aria-label': '복사할 주소', hidden: true });
    var suggest = suggestedRedirect();
    var appErr = el('p', { class: 'form-error', role: 'alert', hidden: true });

    function saveApp() {
      var body = {};
      var id = (f.clientId || '').trim(), sec = (f.clientSecret || '').trim();
      if (id) body.client_id = id;
      if (sec) body.client_secret = sec;
      if (P.conn.editRedirect && f.redirect !== undefined && f.redirect.trim() !== redirect) body.redirect_uri = f.redirect.trim();
      if (!Object.keys(body).length) { appErr.textContent = '바꿀 값을 넣어 주세요.'; appErr.hidden = false; idInput.focus(); return; }
      if (!app.client_id_set && !id) { appErr.textContent = 'Client ID를 넣어 주세요.'; appErr.hidden = false; idInput.focus(); return; }
      if (!app.client_secret_set && !sec) { appErr.textContent = 'Client Secret을 넣어 주세요.'; appErr.hidden = false; secInput.focus(); return; }
      P.conn.busy = 'app';
      renderConn();
      ws.put('/api/publish/linkedin/app', body).then(function () {
        P.conn.busy = '';
        P.conn.form = {};
        P.conn.editRedirect = false;
        refreshAfter('LinkedIn 앱 정보를 저장했어요. 이제 ‘LinkedIn 연결하기’를 눌러 주세요.');
      }, function (ex) {
        P.conn.busy = '';
        if (!ex.auth) connError('앱 정보를 저장하지 못했어요: ' + ex.message);
      });
    }

    var appForm = el('details', { class: 'pub-app', open: appOpen }, [
      el('summary', { text: '앱 정보 (Client ID · Secret · Redirect URL)' }),
      el('form', { class: 'pub-app-form', novalidate: true, 'aria-label': 'LinkedIn 앱 정보', onsubmit: function (e) { e.preventDefault(); if (!envApp && !P.conn.busy) saveApp(); } }, [
      el('p', { class: 'field-hint', text: 'LinkedIn 페이지(회사 페이지)에 연결된 개발자 앱이 필요해요. 게시는 내 개인 프로필로 돼요. 앱의 Products 탭에 ‘Share on LinkedIn’과 ‘Sign In with LinkedIn using OpenID Connect’를 추가하고, Auth 탭의 값을 여기에 넣어 주세요.' }),
      envApp ? ws.ui.notice('info', '환경 변수에서 설정됨 · 여기서는 바꿀 수 없어요 (INSIA_LINKEDIN_CLIENT_ID 등).') : null,
      el('div', { class: 'pub-app-grid' }, [
        el('div', { class: 'field' }, [el('label', { for: 'connLiId', text: 'Client ID' }), idInput]),
        el('div', { class: 'field' }, [el('label', { for: 'connLiSecret' }, ['Client Secret ', el('small', { text: app.client_secret_set ? '저장됨' : '다시 보여 주지 않아요' })]), secInput]),
        el('div', { class: 'field field--wide' }, [
          el('label', { for: 'connLiRedirect', text: 'Redirect URL' }),
          el('div', { class: 'pub-redirect' }, [
            redirInput,
            el('button', { type: 'button', class: 'btn btn--small', 'data-key': 'conn-copy-redirect', text: '복사', onclick: function () { U.copyText(redirInput.value, redirStatus, redirFallback); } })
          ]),
          el('small', { class: 'field-hint', text: 'LinkedIn 개발자 앱 Auth 탭의 Authorized redirect URLs에 이 주소를 그대로 넣어 주세요.' }),
          redirStatus, redirFallback,
          !envApp ? el('div', { class: 'pub-redirect-tools' }, [
            !P.conn.editRedirect ? el('button', { type: 'button', class: 'linkish', 'data-key': 'conn-edit-redirect', text: '다른 주소 쓰기', onclick: function () { P.conn.editRedirect = true; renderConn(); var n = document.getElementById('connLiRedirect'); if (n) n.focus(); } }) : null,
            suggest && suggest !== (f.redirect !== undefined ? f.redirect : redirect) ? el('button', {
              type: 'button', class: 'linkish', 'data-key': 'conn-suggest-redirect', text: '지금 연 주소로 바꾸기 (' + suggest + ')',
              onclick: function () { P.conn.editRedirect = true; f.redirect = suggest; renderConn(); }
            }) : null
          ]) : null
        ])
      ]),
      appErr,
      !envApp ? el('div', { class: 'form-foot' }, [el('button', { type: 'submit', class: 'btn btn--primary btn--small', 'data-key': 'conn-save-app', text: P.conn.busy === 'app' ? '저장하는 중…' : '앱 정보 저장', disabled: !!P.conn.busy })]) : null
      ])
    ]);
    appForm.addEventListener('toggle', function () { f.appOpen = appForm.open; });

    var actions = [];
    if (hasApp) {
      actions.push(el('button', {
        type: 'button', class: 'btn btn--small' + (connected ? '' : ' btn--primary'), 'data-key': 'conn-li-connect', disabled: !!P.conn.busy,
        text: P.conn.busy === 'connect' ? '연결하는 중…' : connected ? '다시 연결' : 'LinkedIn 연결하기',
        onclick: function () { startConnect(false); }
      }));
      actions.push(el('button', { type: 'button', class: 'linkish', 'data-key': 'conn-li-paste', text: '연결이 안 되면 주소 붙여넣기', disabled: !!P.conn.busy, onclick: function () { startConnect(true); } }));
    }
    if (connected) actions = actions.concat(disconnectControls('linkedin'));

    var acc = ps.account || {};
    return el('section', { class: 'pub-platform', 'data-platform': 'linkedin', 'aria-labelledby': 'connLiTitle' }, [
      el('div', { class: 'pub-platform-head' }, [el('h4', { id: 'connLiTitle', text: 'LinkedIn' }), stateBadge('linkedin', ps)]),
      ps.state === 'not_configured' ? el('p', { class: 'pub-note', text: '아래 ‘앱 정보’에 내 LinkedIn 개발자 앱의 Client ID와 Secret을 넣으면 연결할 수 있어요.' })
        : ps.reason && ps.state !== 'connected' ? el('p', { class: 'pub-note', text: ps.reason }) : null,
      connected && (acc.name || acc.id_hint) ? el('p', { class: 'pub-note', text: '계정: ' + [acc.name, acc.kind || KIND.linkedin, acc.id_hint].filter(Boolean).join(' · ') }) : null,
      actions.length ? el('div', { class: 'pub-actions' }, actions) : null,
      P.conn.paste ? pasteBox() : null,
      appForm,
      el('p', { class: 'pub-consent', text: 'INSIA는 이 컴퓨터의 워크스페이스에 게시용 토큰, 만료일, 계정 id만 저장해요(LinkedIn 이름은 저장하지 않아요). 토큰은 게시할 때만 쓰고 다른 곳으로 보내지 않아요. 언제든 ‘연결 해제’로 지울 수 있고, LinkedIn 설정 → 데이터 개인정보 → 권한 있는 서비스에서 앱 권한도 거둘 수 있어요.' })
    ]);
  }

  function safeAuthorizeUrl(u) { return /^https:\/\/www\.linkedin\.com\//.test(String(u || '')) ? u : ''; }

  function startConnect(forcePaste) {
    P.conn.busy = 'connect';
    P.conn.msgs = [];
    renderConn();
    ws.post('/api/publish/linkedin/connect', {}).then(function (r) {
      P.conn.busy = '';
      var url = safeAuthorizeUrl(r && r.authorize_url);
      if (!url) { connError('LinkedIn 동의 화면 주소를 받지 못했어요. 앱 정보를 확인해 주세요.'); return; }
      if (r.mode === 'redirect' && !forcePaste) { location.href = url; return; }
      P.conn.paste = { url: url, openUrl: /^https?:\/\//.test(String(r.open_url || '')) ? r.open_url : '', redirect: r.redirect_uri || '', value: '', error: '', started: Date.now(), expires: Number(r.expires_in) || 600 };
      renderConn();
      var b = document.querySelector('[data-key="conn-li-open"]');
      if (b) b.focus();
    }, function (ex) {
      P.conn.busy = '';
      if (!ex.auth) connError('LinkedIn 연결을 시작하지 못했어요: ' + ex.message);
    });
  }

  function pasteBox() {
    var ps = P.conn.paste;
    var input = el('textarea', { id: 'connLiPaste', rows: '3', spellcheck: 'false', autocomplete: 'off', placeholder: (ps.redirect || 'http://localhost:8765/oauth/linkedin/callback') + '?code=…&state=…' });
    input.value = ps.value || '';
    input.addEventListener('input', function () { ps.value = input.value; });
    var mins = Math.max(1, Math.round(ps.expires / 60));
    return el('div', { class: 'pub-paste', 'data-key': 'conn-paste' }, [
      ps.openUrl ? ws.ui.notice('info', [
        'LinkedIn 앱에 등록한 주소(' + originOf(ps.openUrl) + ')로 대시보드를 열면 바로 연결돼요. ',
        el('a', { class: 'btn btn--small notice-action', href: ps.openUrl, 'data-key': 'conn-open-origin', text: '그 주소로 열기' }),
        el('p', { class: 'notice-foot', text: '아니면 아래 순서로 주소를 붙여 넣어 주세요.' })
      ]) : null,
      el('ol', { class: 'pub-steps' }, [
        el('li', null, [
          '‘LinkedIn 동의 화면 열기’를 눌러 로그인하고 동의해 주세요. ',
          el('button', { type: 'button', class: 'btn btn--small btn--primary', 'data-key': 'conn-li-open', text: 'LinkedIn 동의 화면 열기', onclick: function () { window.open(ps.url, '_blank', 'noopener'); } })
        ]),
        el('li', { text: '동의하면 새 탭이 다른 주소로 이동해요. ‘연결할 수 없음’이 떠도 괜찮아요. 그 탭의 주소창 주소 전체를 복사해 아래에 붙여 넣어 주세요.' }),
        el('li', null, [
          el('label', { class: 'field', for: 'connLiPaste' }, [el('span', null, ['주소창 주소 ', el('small', { text: mins + '분 안에 붙여 넣어 주세요' })]), input]),
          ps.error ? el('p', { class: 'form-error', role: 'alert', text: ps.error }) : null,
          el('div', { class: 'form-foot' }, [
            el('button', { type: 'button', class: 'btn btn--small', 'data-key': 'conn-paste-cancel', text: '취소', onclick: function () { P.conn.paste = null; renderConn(); } }),
            el('button', {
              type: 'button', class: 'btn btn--small btn--primary', 'data-key': 'conn-paste-done', text: P.conn.busy === 'complete' ? '연결하는 중…' : '연결 마치기', disabled: !!P.conn.busy,
              onclick: function () {
                var v = (ps.value || '').trim();
                if (!v) { ps.error = '주소창의 주소를 붙여 넣어 주세요.'; renderConn(); var n = document.getElementById('connLiPaste'); if (n) n.focus(); return; }
                P.conn.busy = 'complete';
                ps.error = '';
                renderConn();
                ws.post('/api/publish/linkedin/complete', { url: v }).then(function () {
                  P.conn.busy = '';
                  P.conn.paste = null;
                  ws.ui.toast('LinkedIn 계정을 연결했어요.', 'success');
                  refreshAfter('LinkedIn 계정을 연결했어요.');
                }, function (ex) {
                  P.conn.busy = '';
                  if (ex.auth) return;
                  ps.error = ex.message;
                  renderConn();
                  var n = document.getElementById('connLiPaste');
                  if (n) n.focus();
                });
              }
            })
          ])
        ])
      ])
    ]);
  }
  function originOf(u) { var m = /^(https?:\/\/[^/?#]+)/.exec(String(u || '')); return m ? m[1] : String(u || ''); }

  function reqRow(ok, title, detail) {
    return el('li', { 'data-ok': String(!!ok) }, [
      el('span', { class: ok ? 'ok' : 'no', 'aria-label': ok ? '갖춤' : '없음', text: ok ? '✓' : '✕' }),
      el('span', null, [el('b', { text: title }), detail ? ' · ' + detail : ''])
    ]);
  }

  function instagramRow(ps, media) {
    if (ps.state === 'disabled') {
      return el('section', { class: 'pub-platform', 'data-platform': 'instagram', 'aria-labelledby': 'connIgTitle' }, [
        el('div', { class: 'pub-platform-head' }, [el('h4', { id: 'connIgTitle', text: '인스타그램' }), badge('off', '시험 중 · 꺼짐')]),
        el('p', { class: 'pub-note', text: '인스타그램 API 게시는 아직 시험 중이라 꺼져 있어요.' }),
        el('details', { class: 'pub-how' }, [
          el('summary', { text: '켜는 방법' }),
          el('p', { text: '서버를 켤 때 환경 변수 INSIA_PUBLISH_INSTAGRAM=1을 주면 켜져요(예: INSIA_PUBLISH_INSTAGRAM=1 insia serve). 인스타그램은 카드 이미지를 공개 HTTPS 주소에서 가져가므로 이미지 전용 공개 주소도 필요해요. 순서는 운영 안내의 ‘API 게시’ 절에 있어요.' })
        ])
      ]);
    }
    var connected = ps.state === 'connected' || ps.state === 'expiring' || ps.state === 'needs_reconnect' || (ps.state === 'unavailable' && ps.account && (ps.account.id_hint || ps.account.username));
    var req = ps.requirements || {};
    var tokInput = el('input', { id: 'connIgToken', type: 'password', autocomplete: 'off', spellcheck: 'false', value: P.conn.igToken || '', placeholder: 'IGAA…' });
    tokInput.addEventListener('input', function () { P.conn.igToken = tokInput.value; });
    function saveToken() {
      var t = (P.conn.igToken || '').trim();
      if (!t) { connError('Meta 개발자 앱에서 만든 토큰을 붙여 넣어 주세요.'); var n = document.getElementById('connIgToken'); if (n) n.focus(); return; }
      P.conn.busy = 'ig';
      renderConn();
      ws.put('/api/publish/instagram/token', { access_token: t }).then(function () {
        P.conn.busy = '';
        P.conn.igToken = '';
        ws.ui.toast('인스타그램 계정을 연결했어요.', 'success');
        refreshAfter('인스타그램 계정을 연결했어요. 토큰은 INSIA가 켜져 있을 때 자동으로 연장돼요.');
      }, function (ex) {
        P.conn.busy = '';
        P.conn.igToken = '';   // never keep a refused token in the page
        if (!ex.auth) connError('인스타그램을 연결하지 못했어요: ' + ex.message);
      });
    }
    var mediaOk = !!(media.valid && req.public_https !== false);
    var mediaText = media.valid
      ? (media.url || '') + (media.mode === 'listener' ? ' (이미지 전용 포트' + (media.port ? ' ' + media.port : '') + ')' : media.mode === 'main' ? ' (대시보드 도메인)' : '')
      : (media.reason || '인스타그램이 이미지를 가져갈 공개 HTTPS 주소가 없어요.');
    var acc = ps.account || {};
    return el('section', { class: 'pub-platform', 'data-platform': 'instagram', 'aria-labelledby': 'connIgTitle' }, [
      el('div', { class: 'pub-platform-head' }, [el('h4', { id: 'connIgTitle', text: '인스타그램' }), stateBadge('instagram', ps), ps.beta ? badge('off', '시험 중') : null]),
      ps.reason && ps.state !== 'connected' ? el('p', { class: 'pub-note', text: ps.reason }) : null,
      connected && (acc.username || acc.id_hint) ? el('p', { class: 'pub-note', text: '계정: ' + [acc.username, acc.account_type, acc.id_hint].filter(Boolean).join(' · ') + ((ps.token || {}).estimated ? ' · 만료일은 추정값이에요' : '') }) : null,
      el('form', { class: 'pub-token', novalidate: true, 'aria-label': '인스타그램 토큰', onsubmit: function (e) { e.preventDefault(); if (!P.conn.busy) saveToken(); } }, [
        el('div', { class: 'field' }, [
          el('label', { for: 'connIgToken' }, [connected ? '새 토큰으로 바꾸기 ' : '액세스 토큰 ', el('small', { text: 'Meta 개발자 앱 → Instagram → API setup with Instagram login → Generate token' })]),
          tokInput
        ]),
        el('div', { class: 'pub-actions' }, [
          el('button', { type: 'submit', class: 'btn btn--small btn--primary', 'data-key': 'conn-ig-save', text: P.conn.busy === 'ig' ? '확인하는 중…' : '연결', disabled: !!P.conn.busy })
        ].concat(connected ? disconnectControls('instagram') : []))
      ]),
      el('ul', { class: 'pub-reqs', 'aria-label': '인스타그램 API 게시에 필요한 것' }, [
        reqRow(mediaOk, '이미지 공개 주소', mediaText),
        reqRow(req.render !== false, '카드 이미지 렌더링', req.render !== false ? 'Playwright·Chromium·한글 글꼴 있음' : '브라우저(Playwright·Chromium)나 한글 글꼴이 없어요')
      ]),
      !mediaOk ? el('details', { class: 'pub-how', 'data-key': 'conn-media-how' }, [
        el('summary', { text: '이미지만 공개 주소로 여는 방법' }),
        el('p', { text: '대시보드는 지금처럼 이 컴퓨터(127.0.0.1)에만 두고, 카드 이미지만 내주는 전용 포트 하나를 Cloudflare Tunnel이나 Caddy로 https 도메인에 연결해요. 이 포트는 /pub/m/ 이미지 말고는 아무것도 보여 주지 않아요.' }),
        ws.ui.cmdBlock('insia serve --media-port 8766 --media-base-url https://media.example.com'),
        el('p', { class: 'field-hint', text: '도메인은 내 것으로 바꿔 주세요. Caddy·cloudflared 설정 예시는 운영 안내의 ‘이미지만 공개하기’에 있어요. 이렇게 하지 않으면 지금처럼 카드 묶음을 받아 앱에서 올리면 돼요.' })
      ]) : null,
      req.render === false ? el('details', { class: 'pub-how', 'data-key': 'conn-render-how' }, [
        el('summary', { text: '설치 방법' }),
        ws.ui.cmdBlock('pip install -e ".[render]" && python -m playwright install chromium'),
        el('p', { class: 'field-hint', text: 'Linux에서 한글이 네모로 깨지면 sudo apt install fonts-noto-cjk. Docker는 .env에 INSIA_WITH_RENDER=1을 넣고 다시 빌드해요.' })
      ]) : null,
      el('p', { class: 'pub-consent', text: 'INSIA는 이 컴퓨터의 워크스페이스에 게시용 토큰, 만료일, 계정 id, @핸들만 저장해요. 토큰은 게시할 때만 쓰고 다른 곳으로 보내지 않아요. 언제든 ‘연결 해제’로 지울 수 있고, 인스타그램 설정 → 앱 및 웹사이트에서 앱 권한도 거둘 수 있어요.' })
    ]);
  }

  // ------------------------------------------------------------------ lifecycle
  function reset() {
    Object.keys(P.polls).forEach(function (k) { if (P.polls[k] && P.polls[k].timer) clearTimeout(P.polls[k].timer); });
    P.status = null; P.statusAt = 0; P.statusLoading = null;
    P.polls = {}; P.done = {}; P.results = {}; P.resolving = {}; P.attempts = {};
    P.conn.msgs = []; P.conn.paste = null; P.conn.form = {}; P.conn.igToken = ''; P.conn.confirm = ''; P.conn.busy = ''; P.conn.error = null;
    closeDialog();
  }
  I.views.publish = { reset: reset };   // shell.js resets every view on login/logout

  I.publish = {
    button: button, section: section, panel: panel, lock: lock, statusPill: statusPill, connectionsCard: connectionsCard,
    /** Forget the cached GET /api/publish (after a change made elsewhere). */
    invalidate: function () { P.status = null; P.statusAt = 0; }
  };
})();
