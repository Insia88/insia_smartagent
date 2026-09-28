/* INSIA 에이전트 스튜디오 — 캘린더 (content calendar).
 *
 * Week / month grid of planned slots (channel colour + status), the
 * "이번 주 계획 세우기" form (POST /api/calendar/plan) and per-slot actions in a
 * dialog: 초안 만들기 (generate job → studio), 날짜 변경, 건너뛰기 / 되살리기, and
 * the linked 보관함 item. Demo mode shows an explainer instead.
 */
(function () {
  'use strict';

  var I = window.INSIA;
  var U = I.util;
  var ws = I.ws;
  var ui = ws.ui;
  var el = U.el;
  var D = ws.date;
  var PLAN_CHANNELS = ['naver_blog', 'linkedin', 'instagram'];
  var DEFAULT_COUNTS = { naver_blog: 2, linkedin: 2, instagram: 2 };

  var C = {
    container: null,
    mode: 'week',
    anchor: null,        // week: Monday; month: first day
    slots: null,
    range: '',
    error: null,
    planOpen: false,
    planBusy: false,
    planError: '',
    planSummary: '',
    dialog: null,
    dialogSlot: null,
    dialogTrigger: null,
    dialogBusy: false,
    dialogError: '',
    dateEdit: false
  };

  function show(container, param, ctx) {
    C.container = container;
    if (ws.mode !== 'live') {
      ws.mount(container, ui.demoExplainer('calendar', '캘린더',
        '한 주의 테마와 채널별 게시 수를 정하면 총괄 에이전트가 날짜마다 주제·관점·키워드를 짜 줘요. 매주 월요일에 계획을 세우고, 그날 올릴 글은 버튼 하나로 초안을 만들어요.', [
          '주간·월간 달력에서 채널 색과 상태(계획 · 작성 중 · 초안 있음 · 건너뜀)를 한눈에 봐요.',
          '“이번 주 계획 세우기”에 테마와 채널별 게시 수를 넣으면, 이미 게시한 주제는 피해서 계획을 짜요.',
          '칸을 눌러 초안 만들기, 날짜 바꾸기, 건너뛰기를 할 수 있고, 만든 초안은 보관함에서 검토·승인해요.',
          '터미널에서 insia run-due를 예약해 두면 그날 올릴 초안을 미리 만들어 둘 수도 있어요.'
        ]));
      if (ctx && ctx.focus) ui.focusHeading(document.getElementById('calendarTitle'));
      return;
    }
    if (!C.anchor) C.anchor = D.weekStart(D.today());
    render(ctx && ctx.focus);
    load();
  }
  function reset() { C.slots = null; C.range = ''; }
  I.views.calendar = { show: show, reset: reset };

  ws.onJobEnd(function (job) {
    if (job.slotId || C.slots) { C.slots = null; if (isShown()) load(); }
  });
  function isShown() { return C.container && !C.container.hidden && ws.route.view === 'calendar'; }

  // ------------------------------------------------------------------ data
  function gridRange() {
    if (C.mode === 'week') return { from: C.anchor, to: D.add(C.anchor, 6) };
    var first = D.monthStart(C.anchor);
    var start = D.weekStart(first);
    var end = D.monthEnd(first);
    var endWeek = D.add(D.weekStart(end), 6);
    return { from: start, to: endWeek };
  }
  function load() {
    var r = gridRange();
    var key = r.from + '|' + r.to;
    C.error = null;
    return ws.get('/api/calendar' + ws.q({ from: r.from, to: r.to })).then(function (resp) {
      if (key !== gridRange().from + '|' + gridRange().to) return;
      C.slots = ws.listOf(resp, 'slots');
      C.range = key;
      if (isShown()) render(false);
      if (C.dialogSlot) {
        var fresh = C.slots.filter(function (s) { return s.id === C.dialogSlot.id; })[0];
        if (fresh) { C.dialogSlot = fresh; renderDialog(); }
      }
    }, function (err) {
      C.error = err;
      if (isShown()) render(false);
    });
  }

  // ------------------------------------------------------------------ render
  function render(focus) {
    var keep = document.activeElement && C.container.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
    var planBtn = el('button', {
      type: 'button', class: 'btn btn--primary', 'data-key': 'plan-toggle', 'aria-expanded': String(C.planOpen), 'aria-controls': 'planForm',
      text: '이번 주 계획 세우기', onclick: function () { C.planOpen = !C.planOpen; C.planError = ''; render(false); var t = document.getElementById('planTheme'); if (C.planOpen && t) t.focus(); }
    });
    var r = gridRange();
    var label = C.mode === 'week'
      ? D.monthDay(r.from) + ' – ' + D.monthDay(r.to)
      : C.anchor.slice(0, 4) + '년 ' + (+C.anchor.slice(5, 7)) + '월';
    var nav = el('div', { class: 'cal-toolbar' }, [
      el('div', { class: 'cal-nav' }, [
        el('button', { type: 'button', class: 'icon-btn', 'data-key': 'prev', 'aria-label': C.mode === 'week' ? '이전 주' : '이전 달', onclick: function () { step(-1); } }, chevron(-1)),
        el('button', { type: 'button', class: 'btn', 'data-key': 'today', text: '오늘', onclick: function () { C.anchor = C.mode === 'week' ? D.weekStart(D.today()) : D.monthStart(D.today()); C.slots = null; render(false); load(); } }),
        el('button', { type: 'button', class: 'icon-btn', 'data-key': 'next', 'aria-label': C.mode === 'week' ? '다음 주' : '다음 달', onclick: function () { step(1); } }, chevron(1)),
        el('h3', { class: 'cal-range', 'aria-live': 'polite', text: label })
      ]),
      el('div', { class: 'seg', role: 'group', 'aria-label': '달력 보기' }, [['week', '주'], ['month', '월']].map(function (m) {
        return el('button', {
          type: 'button', 'data-key': 'mode-' + m[0], 'aria-pressed': String(C.mode === m[0]), text: m[1], onclick: function () {
            if (C.mode === m[0]) return;
            C.mode = m[0];
            var today = D.today();
            // month → week lands on this week when this month is shown, else on the month's first week
            if (m[0] === 'week') C.anchor = D.weekStart(today.slice(0, 7) === C.anchor.slice(0, 7) ? today : C.anchor);
            else C.anchor = D.monthStart(D.weekStart(today) === C.anchor ? today : D.add(C.anchor, 3));
            C.slots = null;
            render(false);
            load();
          }
        });
      }))
    ]);
    var grid;
    if (C.error && !C.slots) grid = ui.errorState(C.error, function () { load(); });
    else grid = C.mode === 'week' ? weekGrid(r) : monthGrid(r);

    ws.mount(C.container, [
      ui.viewHead('calendar', '캘린더', '한 주 테마로 게시 계획을 세우고, 날짜마다 정한 주제로 초안을 만들어요. 게시는 사람이 직접 해요.', [planBtn]),
      C.planOpen ? planForm() : null,
      C.planSummary ? ui.notice('success', [el('b', { text: '계획을 세웠어요 · ' }), C.planSummary,
        el('button', { type: 'button', class: 'btn btn--small btn--ghost notice-action', text: '닫기', onclick: function () { C.planSummary = ''; render(false); } })]) : null,
      nav,
      legend(),
      el('div', { class: 'cal-wrap' + (C.slots ? '' : ' is-loading'), 'aria-busy': C.slots ? 'false' : 'true' }, grid)
    ]);
    if (focus) ui.focusHeading(document.getElementById('calendarTitle'));
    else if (keep) { var n = C.container.querySelector('[data-key="' + keep + '"]'); if (n) n.focus(); }
  }

  function chevron(dir) {
    var s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    s.setAttribute('viewBox', '0 0 24 24');
    s.setAttribute('aria-hidden', 'true');
    s.setAttribute('focusable', 'false');
    var p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', dir < 0 ? 'M15 5l-7 7 7 7' : 'M9 5l7 7-7 7');
    p.setAttribute('fill', 'none');
    p.setAttribute('stroke', 'currentColor');
    p.setAttribute('stroke-width', '2');
    p.setAttribute('stroke-linecap', 'round');
    p.setAttribute('stroke-linejoin', 'round');
    s.appendChild(p);
    return s;
  }

  function step(dir) {
    C.anchor = C.mode === 'week' ? D.add(C.anchor, 7 * dir) : D.addMonths(C.anchor, dir);
    C.slots = null;
    render(false);
    load();
  }

  function legend() {
    return el('div', { class: 'cal-legend', 'aria-label': '범례' }, [
      el('span', { class: 'lg-group' }, U.CHANNEL_IDS.map(function (c) {
        return el('span', { class: 'lg-item' }, [el('i', { class: 'lg-swatch', style: '--ch:' + ws.channelColor(c), 'aria-hidden': 'true' }), U.chName(c)]);
      })),
      el('span', { class: 'lg-group' }, Object.keys(ws.SLOT_STATUS).map(function (k) {
        return el('span', { class: 'lg-item' }, [el('i', { class: 'lg-status', 'data-status': k, 'aria-hidden': 'true' }), ws.SLOT_STATUS[k]]);
      }))
    ]);
  }

  function slotsOn(ymd) {
    return (C.slots || []).filter(function (s) { return s.date === ymd; }).sort(function (a, b) {
      return U.CHANNEL_IDS.indexOf(a.channel) - U.CHANNEL_IDS.indexOf(b.channel);
    });
  }

  function slotButton(s, compact) {
    return el('button', {
      type: 'button', class: 'slot' + (compact ? ' slot--compact' : ''), 'data-status': s.status, 'data-key': 'slot-' + s.id,
      style: '--ch:' + ws.channelColor(s.channel), 'aria-haspopup': 'dialog',
      'aria-label': U.chName(s.channel) + ', ' + (ws.SLOT_STATUS[s.status] || s.status) + ': ' + (s.topic || ''),
      onclick: function (e) { openDialog(s, e.currentTarget); }
    }, [
      el('span', { class: 'slot-top' }, [
        el('span', { class: 'slot-ch', text: ws.CHANNEL_SHORT[s.channel] || U.chName(s.channel) }),
        el('span', { class: 'slot-status', text: ws.SLOT_STATUS[s.status] || s.status })
      ]),
      el('span', { class: 'slot-topic', text: s.topic || '(주제 없음)' })
    ]);
  }

  function weekGrid(r) {
    var today = D.today();
    var days = [];
    for (var i = 0; i < 7; i++) days.push(D.add(r.from, i));
    return el('ol', { class: 'cal-week' }, days.map(function (ymd) {
      var list = slotsOn(ymd);
      return el('li', { class: 'cal-day', 'data-today': ymd === today ? 'true' : null, 'data-empty': list.length ? null : 'true' }, [
        el('h4', { class: 'cal-day-head' }, [
          el('span', { class: 'cd-wd', text: D.weekday(ymd) }),
          el('span', { class: 'cd-date', text: D.monthDay(ymd) }),
          ymd === today ? el('span', { class: 'cd-today', text: '오늘' }) : null
        ]),
        list.length ? el('ul', { class: 'slot-list' }, list.map(function (s) { return el('li', null, slotButton(s, false)); }))
          : el('p', { class: 'cal-empty', text: C.slots ? '계획 없음' : '' })
      ]);
    }));
  }

  function monthGrid(r) {
    var today = D.today();
    var month = C.anchor.slice(0, 7);
    var cells = [];
    for (var d = r.from; d <= r.to; d = D.add(d, 1)) cells.push(d);
    var head = el('div', { class: 'cal-wdays', 'aria-hidden': 'true' }, ['월', '화', '수', '목', '금', '토', '일'].map(function (w) { return el('span', { text: w }); }));
    return el('div', { class: 'cal-month-wrap' }, [head, el('ol', { class: 'cal-month' }, cells.map(function (ymd) {
      var list = slotsOn(ymd);
      var more = list.length - 2;
      return el('li', { class: 'cal-cell', 'data-out': ymd.slice(0, 7) !== month ? 'true' : null, 'data-today': ymd === today ? 'true' : null }, [
        el('span', { class: 'cc-num' }, [el('span', { 'aria-hidden': 'true', text: String(+ymd.slice(8)) }), el('span', { class: 'sr-only', text: D.monthDay(ymd) + ' ' + D.weekday(ymd) + '요일' + (list.length ? ', 계획 ' + list.length + '개' : '') })]),
        list.length ? el('ul', { class: 'slot-list' }, list.slice(0, more > 0 ? 1 : 2).map(function (s) { return el('li', null, slotButton(s, true)); })) : null,
        more > 0 ? el('button', {
          type: 'button', class: 'cc-more', 'data-key': 'more-' + ymd, text: '+' + (list.length - 1) + '개',
          'aria-label': D.monthDay(ymd) + ' 계획 ' + list.length + '개를 주간 보기로 보기',
          onclick: function () { C.mode = 'week'; C.anchor = D.weekStart(ymd); C.slots = null; render(false); load(); }
        }) : null
      ]);
    }))]);
  }

  // ------------------------------------------------------------------ plan form
  function planForm() {
    var start = D.today();
    var theme = el('input', { id: 'planTheme', type: 'text', required: true, autocomplete: 'off', placeholder: '예: 예비창업패키지 마감 전 준비 체크리스트' });
    var startInput = el('input', { id: 'planStart', type: 'date', value: start, required: true });
    var span = el('p', { class: 'plan-span', 'aria-live': 'polite' });
    function updateSpan() {
      var s = startInput.value;
      span.textContent = /^\d{4}-\d{2}-\d{2}$/.test(s) ? D.monthDay(s) + ' (' + D.weekday(s) + ') ~ ' + D.monthDay(D.add(s, 6)) + ' (' + D.weekday(D.add(s, 6)) + ') · 7일' : '';
    }
    startInput.addEventListener('input', updateSpan);
    updateSpan();
    var counts = {};
    var countFields = PLAN_CHANNELS.map(function (c) {
      var inp = el('input', { id: 'planCount-' + c, type: 'number', min: '0', max: '7', step: '1', value: String(DEFAULT_COUNTS[c]), inputmode: 'numeric' });
      counts[c] = inp;
      return el('label', { class: 'count-field', for: 'planCount-' + c, style: '--ch:' + ws.channelColor(c) }, [
        el('i', { class: 'lg-swatch', 'aria-hidden': 'true' }), el('span', { text: U.chName(c) }), inp, el('span', { class: 'unit', text: '개' })
      ]);
    });
    var err = el('p', { class: 'form-error', role: 'alert', hidden: !C.planError, text: C.planError });
    var submit = el('button', { type: 'submit', class: 'btn btn--primary', disabled: C.planBusy, text: C.planBusy ? '계획을 세우는 중…' : '계획 세우기' });
    var form = el('form', { class: 'card plan-form', id: 'planForm', novalidate: true, 'aria-labelledby': 'planFormTitle' }, [
      el('h3', { class: 'card-title', id: 'planFormTitle', text: '이번 주 계획 세우기' }),
      el('p', { class: 'card-sub', text: '총괄 에이전트가 회사 프로필과 지난 게시물을 보고 날짜별 주제·관점·키워드를 짜요. 이미 게시한 주제와 관점은 피해요. 사업계획서는 캘린더 대신 스튜디오에서 만들어요.' }),
      el('div', { class: 'plan-grid' }, [
        el('label', { class: 'field field--wide', for: 'planTheme' }, [el('span', { text: '이번 주 테마' }), theme]),
        el('div', { class: 'field' }, [el('label', { for: 'planStart', text: '시작일' }), startInput, span]),
        el('fieldset', { class: 'field counts' }, [el('legend', { text: '채널별 게시 수' })].concat(countFields))
      ]),
      err,
      el('div', { class: 'form-foot' }, [
        el('button', { type: 'button', class: 'btn', text: '닫기', onclick: function () { C.planOpen = false; render(false); var b = C.container.querySelector('[data-key="plan-toggle"]'); if (b) b.focus(); } }),
        submit
      ])
    ]);
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var t = theme.value.trim();
      var s = startInput.value;
      var body = { theme: t, start: s, end: /^\d{4}-\d{2}-\d{2}$/.test(s) ? D.add(s, 6) : '', counts: {} };
      var total = 0;
      PLAN_CHANNELS.forEach(function (c) {
        var n = Math.max(0, Math.min(7, parseInt(counts[c].value, 10) || 0));
        body.counts[c] = n;
        total += n;
      });
      function fail(msg, node) { C.planError = msg; err.textContent = msg; err.hidden = false; if (node) node.focus(); }
      if (!t) return fail('이번 주 테마를 적어 주세요.', theme);
      if (!body.end) return fail('시작일을 골라 주세요.', startInput);
      if (!total) return fail('게시 수를 하나 이상 정해 주세요.', counts.naver_blog);
      C.planBusy = true;
      C.planError = '';
      submit.disabled = true;
      submit.textContent = '계획을 세우는 중…';
      ws.post('/api/calendar/plan', body).then(function (resp) {
        if (resp && resp.run_id && !ws.listOf(resp, 'slots').length) return waitRun(resp.run_id).then(function () { return resp; });
        return resp;
      }).then(function (resp) {
        var plan = (resp && resp.plan) || resp || {};
        var slots = ws.listOf(plan, 'slots');
        C.planBusy = false;
        C.planOpen = false;
        C.planSummary = (plan.summary || resp.summary || '') + (slots.length ? ' (게시 ' + slots.length + '개)' : '');
        C.mode = 'week';
        C.anchor = D.weekStart(s);
        C.slots = null;
        render(false);
        load().then(function () {
          if (s !== C.anchor) { /* the plan starts mid-week: the next week holds the rest */ }
        });
        ui.toast('이번 주 계획을 세웠어요.', 'success');
      }, function (ex) {
        C.planBusy = false;
        if (ex.auth) return;
        submit.disabled = false;
        submit.textContent = '계획 세우기';
        fail('계획을 세우지 못했어요: ' + ex.message);
      });
    });
    return form;
  }

  /** For servers that plan asynchronously ({run_id}): poll the run until it stops. */
  function waitRun(runId) {
    var started = Date.now();
    return new Promise(function (resolve, reject) {
      (function tick() {
        ws.get('/api/runs/' + encodeURIComponent(runId)).then(function (r) {
          var st = r && (r.status || (r.run && r.run.status));
          if (st && st !== 'running' && st !== 'queued') {
            if (st === 'failed') reject(new Error((r && r.error) || '계획 작업이 실패했어요'));
            else resolve(r);
          } else if (Date.now() - started > 10 * 60 * 1000) reject(new Error('계획이 10분 넘게 끝나지 않았어요'));
          else setTimeout(tick, 2000);
        }, reject);
      })();
    });
  }

  // ------------------------------------------------------------------ slot dialog
  function ensureDialog() {
    if (C.dialog && document.contains(C.dialog)) return C.dialog;
    var dlg = el('dialog', { class: 'sheet slot-dialog', id: 'slotDialog', 'aria-labelledby': 'slotTitle' });
    dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });
    dlg.addEventListener('close', function () {
      C.dialogSlot = null;
      C.dateEdit = false;
      C.dialogError = '';
      var t = C.dialogTrigger;
      C.dialogTrigger = null;
      if (t && document.contains(t)) t.focus();
      else if (t && t.getAttribute) { var n = C.container.querySelector('[data-key="' + t.getAttribute('data-key') + '"]'); if (n) n.focus(); }
    });
    document.body.appendChild(dlg);
    C.dialog = dlg;
    return dlg;
  }

  function openDialog(slot, trigger) {
    C.dialogSlot = slot;
    C.dialogTrigger = trigger || null;
    C.dateEdit = false;
    C.dialogError = '';
    var dlg = ensureDialog();
    renderDialog();
    if (!dlg.open) dlg.showModal();
    var f = dlg.querySelector('.slot-actions .btn--primary') || dlg.querySelector('.icon-btn');
    if (f) f.focus();
  }

  function renderDialog() {
    var s = C.dialogSlot;
    var dlg = C.dialog;
    if (!s || !dlg) return;
    var err = el('p', { class: 'form-error', role: 'alert', hidden: !C.dialogError, text: C.dialogError });
    var actions = [];
    function act(label, primary, fn, danger) {
      return el('button', { type: 'button', class: 'btn' + (primary ? ' btn--primary' : '') + (danger ? ' btn--danger' : ''), disabled: C.dialogBusy, text: label, onclick: fn });
    }
    if (s.status === 'planned' || s.status === 'skipped') actions.push(act('초안 만들기', true, generate));
    if (s.status === 'drafted' && s.item_id) actions.push(el('a', { class: 'btn btn--primary', href: '#/library/' + encodeURIComponent(s.item_id), onclick: function () { dlg.close(); }, text: '보관함에서 열기' }));
    if (s.status === 'generating') actions.push(el('a', { class: 'btn', href: '#/studio', onclick: function () { dlg.close(); }, text: '스튜디오에서 보기' }));
    if (s.status !== 'generating') actions.push(act('날짜 바꾸기', false, function () { C.dateEdit = !C.dateEdit; renderDialog(); var d = document.getElementById('slotDate'); if (d) d.focus(); }));
    if (s.status === 'planned') actions.push(act('건너뛰기', false, function () { update({ status: 'skipped' }, '이 계획을 건너뛰었어요.'); }));
    if (s.status === 'skipped') actions.push(act('다시 계획에 넣기', false, function () { update({ status: 'planned' }, '다시 계획에 넣었어요.'); }));

    var dateForm = null;
    if (C.dateEdit) {
      var inp = el('input', { id: 'slotDate', type: 'date', value: s.date });
      dateForm = el('div', { class: 'slot-date-form' }, [
        el('label', { class: 'field', for: 'slotDate' }, [el('span', { text: '새 날짜' }), inp]),
        el('button', {
          type: 'button', class: 'btn btn--primary', disabled: C.dialogBusy, text: '날짜 저장', onclick: function () {
            if (!/^\d{4}-\d{2}-\d{2}$/.test(inp.value)) { C.dialogError = '날짜를 골라 주세요.'; renderDialog(); return; }
            update({ date: inp.value }, D.monthDay(inp.value) + '로 옮겼어요.');
          }
        })
      ]);
    }
    var info = [
      ['관점', s.angle], ['키워드', (s.keywords || []).join(', ')], ['목표', s.goal], ['상태', ws.SLOT_STATUS[s.status] || s.status]
    ].filter(function (r) { return r[1]; });
    dlg.textContent = '';
    U.appendChildren(dlg, [
      el('header', { class: 'sheet-head' }, [
        el('div', null, [
          el('p', { class: 'eyebrow slot-eyebrow', style: '--ch:' + ws.channelColor(s.channel) }, [el('i', { class: 'lg-swatch', 'aria-hidden': 'true' }), U.chName(s.channel) + ' · ' + D.monthDay(s.date) + ' (' + D.weekday(s.date) + ')']),
          el('h2', { id: 'slotTitle', text: s.topic || '(주제 없음)' })
        ]),
        el('button', { type: 'button', class: 'icon-btn', 'aria-label': '닫기', onclick: function () { dlg.close(); } }, closeIcon())
      ]),
      el('div', { class: 'slot-body' }, [
        el('dl', { class: 'slot-info' }, info.map(function (r) { return el('div', null, [el('dt', { text: r[0] }), el('dd', { text: r[1] })]); })),
        s.status === 'generating' ? el('p', { class: 'panel-hint', text: '초안을 만드는 중이에요. 끝나면 보관함에 저장되고 이 칸이 ‘초안 있음’으로 바뀌어요.' }) : null,
        s.status === 'planned' || s.status === 'skipped' ? el('p', { class: 'panel-hint', text: '초안 만들기를 누르면 이 주제로 실행이 시작되고, 스튜디오에서 에이전트가 일하는 모습을 보여 드려요.' }) : null,
        dateForm,
        err
      ]),
      el('footer', { class: 'sheet-foot slot-actions' }, actions)
    ]);
  }
  function closeIcon() {
    var s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    s.setAttribute('viewBox', '0 0 24 24');
    s.setAttribute('aria-hidden', 'true');
    s.setAttribute('focusable', 'false');
    var p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', 'M6 6l12 12M18 6L6 18');
    p.setAttribute('stroke', 'currentColor');
    p.setAttribute('stroke-width', '2');
    p.setAttribute('stroke-linecap', 'round');
    s.appendChild(p);
    return s;
  }

  function update(body, okMsg) {
    var s = C.dialogSlot;
    if (!s) return;
    C.dialogBusy = true;
    C.dialogError = '';
    renderDialog();
    ws.post('/api/calendar/' + encodeURIComponent(s.id), body).then(function (resp) {
      C.dialogBusy = false;
      var fresh = ws.unwrap(resp, 'slot');
      if (fresh && fresh.id) {
        C.slots = (C.slots || []).map(function (x) { return x.id === fresh.id ? fresh : x; });
        C.dialogSlot = fresh;
      }
      C.dateEdit = false;
      ui.toast(okMsg, 'success');
      if (C.dialog.open) C.dialog.close();
      render(false);
      load();
    }, function (ex) {
      C.dialogBusy = false;
      if (ex.auth) { if (C.dialog.open) C.dialog.close(); return; }
      C.dialogError = ex.message;
      renderDialog();
    });
  }

  function generate() {
    var s = C.dialogSlot;
    if (!s) return;
    C.dialogBusy = true;
    C.dialogError = '';
    renderDialog();
    ws.post('/api/calendar/' + encodeURIComponent(s.id) + '/generate', {}).then(function (resp) {
      C.dialogBusy = false;
      var runId = resp && resp.run_id;
      if (!runId) throw new Error('서버가 작업 번호(run_id)를 보내지 않았어요.');
      C.slots = (C.slots || []).map(function (x) { return x.id === s.id ? Object.assign({}, x, { status: 'generating' }) : x; });
      C.dialogTrigger = null;
      if (C.dialog.open) C.dialog.close();
      ws.watchJob(runId, { label: '캘린더 초안 만들기', slotId: s.id, itemId: resp.item_id || '', goStudio: true });
    }).catch(function (ex) {
      C.dialogBusy = false;
      if (ex.auth) { if (C.dialog.open) C.dialog.close(); return; }
      C.dialogError = '초안을 만들지 못했어요: ' + ex.message;
      renderDialog();
    });
  }
})();
