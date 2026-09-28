/* INSIA 에이전트 스튜디오 — run controls for the studio (live mode only).
 *
 * - Run bar under the studio toolbar for the run the stage follows: 멈추기 while it runs,
 *   이어서 실행 when it stopped (interrupted by a restart, failed, cancelled, over budget),
 *   and a link to the saved items when it finished.
 * - 실행 기록 dialog: recent runs from GET /api/runs with 보기 (live or replay of the stored
 *   events), 이어서 실행 (POST /api/runs/<id>/resume) and 멈추기 (POST /api/runs/<id>/cancel).
 * - On boot (and after login) the stage attaches to a run that is still going, or opens the
 *   latest run's final state instead of the demo recording.
 *
 * Item jobs started from 보관함 / 캘린더 keep their own job banner (core.js), so the bar hides for them.
 */
(function () {
  'use strict';

  var I = window.INSIA;
  var U = I.util;
  var ws = I.ws;
  var ui = ws.ui;
  var el = U.el;

  var KIND = { pipeline: '실행', slot: '캘린더 초안', review: '재검수', revise: '수정 요청', edit: '사람 수정', import: '가져오기' };
  var RUN_STATUS = { running: '진행 중', completed: '완료', failed: '실패', cancelled: '멈춤', interrupted: '중단됨' };
  var R = {
    watching: null,   // {runId, job, history, ended, event}
    info: null,       // GET /api/runs/<id> of the watched run
    infoTries: 0,
    busy: false,
    error: '',
    dialog: null,
    runs: null,
    runsError: null,
    trigger: null,
    attached: false
  };

  function kindLabel(k) { return KIND[k] || k || '실행'; }
  function statusLabel(s) { return RUN_STATUS[s] || s || ''; }
  function barNode() { return document.getElementById('runBar'); }

  // ------------------------------------------------------------------ following the stage
  I.studio.onLive(function (e) {
    if (e.type === 'start') {
      R.watching = { runId: e.runId, job: !!(e.opts && e.opts.job), history: !!(e.opts && e.opts.history), ended: false, event: null };
      R.info = null;
      R.infoTries = 0;
      R.busy = false;
      R.error = '';
      renderBar();
      if (!R.watching.job) refreshInfo(e.runId);
      return;
    }
    if (!R.watching || R.watching.runId !== e.runId) return;
    if (e.event && e.event.type === 'replaced') { R.watching = null; renderBar(); return; }
    R.watching.ended = true;
    R.watching.event = e.event || null;
    R.infoTries = 0;
    renderBar();
    refreshInfo(e.runId);
    if (R.dialog && R.dialog.open) loadRuns();
  });

  /** Run detail for the bar. Right after the stream ends the server may still list the run as active: retry briefly. */
  function refreshInfo(runId) {
    ws.get('/api/runs/' + encodeURIComponent(runId)).then(function (info) {
      if (!R.watching || R.watching.runId !== runId) return;
      R.info = info;
      if (R.watching.ended && info && info.active && R.infoTries < 8) {
        R.infoTries++;
        setTimeout(function () { refreshInfo(runId); }, 400);
      }
      renderBar();
    }, function () { /* the bar keeps its generic text */ });
  }

  function renderBar() {
    var bar = barNode();
    if (!bar) return;
    var w = R.watching;
    if (ws.mode !== 'live' || !w || w.job) { bar.hidden = true; bar.textContent = ''; return; }
    var info = R.info || {};
    var topic = info.topic || '';
    var kind = kindLabel(info.kind);
    var active = !w.ended && !(w.history && info.status && info.status !== 'running');
    var nodes = [];
    var state;
    if (active) {
      state = 'running';
      nodes.push(el('span', { class: 'jb-dot', 'aria-hidden': 'true' }));
      nodes.push(el('b', { text: kind + ' 진행 중' }));
      if (topic) nodes.push(el('span', { class: 'rb-topic', text: topic }));
      nodes.push(el('button', {
        type: 'button', class: 'btn btn--small', 'data-key': 'run-stop', disabled: R.busy || info.cancel_requested,
        text: info.cancel_requested ? '멈추는 중…' : '멈추기', onclick: function () { cancelRun(w.runId); }
      }));
    } else {
      var st = info.active ? 'running' : (info.status || (w.event && w.event.type === 'run.completed' ? 'completed' : 'failed'));
      state = st === 'completed' ? 'done' : st === 'running' ? 'running' : 'failed';
      var items = info.items || {};
      var itemIds = Object.keys(items).map(function (c) { return items[c]; });
      var why = info.error || (w.event && w.event.data && w.event.data.error) || '';
      nodes.push(el('b', { text: (w.history ? '지난 ' : '') + kind + ' · ' + statusLabel(st) }));
      if (topic) nodes.push(el('span', { class: 'rb-topic', text: topic }));
      if (st === 'completed' && itemIds.length) nodes.push(el('span', { text: '결과 ' + itemIds.length + '개가 보관함에 있어요.' }));
      else if (st !== 'completed' && st !== 'running' && why) nodes.push(el('span', { class: 'rb-why', text: why }));
      if (info.resumable && !info.active) {
        nodes.push(el('button', {
          type: 'button', class: 'btn btn--small btn--primary', 'data-key': 'run-resume', disabled: R.busy,
          text: R.busy ? '이어서 실행하는 중…' : '이어서 실행', onclick: function () { resumeRun(w.runId, false); }
        }));
      }
      if (itemIds.length) {
        nodes.push(el('a', { class: 'btn btn--small', href: itemIds.length === 1 ? '#/library/' + encodeURIComponent(itemIds[0]) : '#/library', text: '보관함에서 보기' }));
      }
    }
    if (R.error) nodes.push(el('span', { class: 'rb-error', role: 'alert', text: R.error }));
    var keep = document.activeElement && bar.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
    bar.dataset.kind = state;
    bar.textContent = '';
    U.appendChildren(bar, nodes);
    bar.hidden = false;
    if (keep) { var n = bar.querySelector('[data-key="' + keep + '"]'); if (n && !n.disabled) n.focus(); }
  }

  // ------------------------------------------------------------------ actions
  function cancelRun(runId) {
    R.busy = true;
    R.error = '';
    renderBar();
    renderRuns();
    return ws.post('/api/runs/' + encodeURIComponent(runId) + '/cancel', {}).then(function () {
      R.busy = false;
      if (R.info && R.info.run_id === runId) R.info.cancel_requested = true;
      ui.toast('멈추는 중이에요. 진행 중인 호출이 끝나면 멈춰요. 끝낸 채널은 저장돼요.', 'info');
      renderBar();
      if (R.dialog && R.dialog.open) loadRuns();
    }, function (ex) {
      R.busy = false;
      if (ex.auth) return;
      R.error = '멈추지 못했어요: ' + ex.message;
      renderBar();
      if (R.dialog && R.dialog.open) { R.runsError = ex; renderRuns(); }
    });
  }

  function resumeRun(runId, force) {
    R.busy = true;
    R.error = '';
    renderBar();
    var body = { options: ws.jobOptions() };
    if (force) body.force = true;
    return ws.post('/api/runs/' + encodeURIComponent(runId) + '/resume', body).then(function (resp) {
      R.busy = false;
      if (R.dialog && R.dialog.open) R.dialog.close();
      ui.toast('이어서 실행해요. 끝난 채널은 건너뛰고 남은 작업부터 해요.', 'success');
      I.studio.watchRun((resp && resp.run_id) || runId, { title: '이어서 실행 ' + runId });
      if (ws.route.view !== 'studio') ws.go('studio');
    }, function (ex) {
      R.busy = false;
      if (ex.auth) return;
      R.error = '이어서 실행하지 못했어요: ' + ex.message;
      renderBar();
      if (R.dialog && R.dialog.open) { R.dialogError = R.error; renderRuns(); }
    });
  }

  function watch(run) {
    if (R.dialog && R.dialog.open) R.dialog.close();
    var live = run.active || run.status === 'running';
    I.studio.watchRun(run.run_id, { history: !live, title: (live ? '실시간 ' : '지난 ') + kindLabel(run.kind) + ' ' + run.run_id });
    if (ws.route.view !== 'studio') ws.go('studio');
  }

  // ------------------------------------------------------------------ 실행 기록 dialog
  function ensureDialog() {
    if (R.dialog && document.contains(R.dialog)) return R.dialog;
    var dlg = el('dialog', { class: 'sheet runs-dialog', id: 'runsDialog', 'aria-labelledby': 'runsTitle' });
    dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });
    dlg.addEventListener('close', function () {
      var t = R.trigger;
      R.trigger = null;
      R.dialogError = '';
      if (t && document.contains(t)) t.focus();
    });
    document.body.appendChild(dlg);
    R.dialog = dlg;
    return dlg;
  }

  function openDialog(trigger) {
    if (ws.mode !== 'live') return;
    R.trigger = trigger || null;
    R.dialogError = '';
    var dlg = ensureDialog();
    renderRuns();
    if (!dlg.open) dlg.showModal();
    var f = dlg.querySelector('.icon-btn');
    if (f) f.focus();
    loadRuns();
  }

  function loadRuns() {
    return ws.get('/api/runs?limit=30').then(function (r) {
      R.runs = ws.listOf(r, 'runs');
      R.runsError = null;
      renderRuns();
    }, function (err) {
      R.runsError = err;
      renderRuns();
    });
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

  function runRow(run) {
    var st = run.active ? 'running' : run.status;
    var chans = run.channels || [];
    var scores = run.scores || {};
    var chanText = chans.map(function (c) {
      return (ws.CHANNEL_SHORT[c] || U.chName(c)) + (typeof scores[c] === 'number' ? ' ' + scores[c] + '점' : '');
    }).join(' · ');
    var watching = R.watching && R.watching.runId === run.run_id;
    var actions = [];
    if (run.events || run.active) {
      actions.push(el('button', {
        type: 'button', class: 'btn btn--small', 'data-key': 'watch-' + run.run_id,
        text: watching ? '무대에서 보는 중' : (run.active ? '실시간으로 보기' : '다시 보기'), disabled: watching && !R.watching.ended,
        onclick: function () { watch(run); }
      }));
    }
    if (run.resumable) {
      actions.push(el('button', { type: 'button', class: 'btn btn--small btn--primary', 'data-key': 'resume-' + run.run_id, text: '이어서 실행', disabled: R.busy, onclick: function () { resumeRun(run.run_id, false); } }));
    }
    if (run.active) {
      actions.push(el('button', { type: 'button', class: 'btn btn--small btn--danger', 'data-key': 'stop-' + run.run_id, text: '멈추기', disabled: R.busy, onclick: function () { cancelRun(run.run_id); } }));
    }
    var ids = run.items ? Object.keys(run.items).map(function (c) { return run.items[c]; }) : [];
    var target = ids.length === 1 ? ids[0] : (run.parent_item_id || '');
    if (target) actions.push(el('a', { class: 'btn btn--small btn--ghost', href: '#/library/' + encodeURIComponent(target), onclick: function () { R.dialog.close(); }, text: '보관함' }));
    return el('li', { class: 'run-row', 'data-run-status': st }, [
      el('div', { class: 'run-main' }, [
        el('span', { class: 'run-state', 'data-run-status': st }, [el('span', { class: 'sp-dot', 'aria-hidden': 'true' }), statusLabel(st)]),
        el('span', { class: 'run-kind-label', text: kindLabel(run.kind) + (run.mode === 'mock' ? ' · 모의' : run.mode === 'live' ? ' · 실제 API' : '') }),
        el('span', { class: 'run-time', text: ws.date.dateTime(run.created_at) })
      ]),
      el('p', { class: 'run-topic', text: run.topic || '(주제 없음)' }),
      chanText ? el('p', { class: 'run-chans', text: chanText }) : null,
      st !== 'completed' && st !== 'running' && run.error ? el('p', { class: 'run-error', text: run.error }) : null,
      el('div', { class: 'run-actions' }, actions)
    ]);
  }

  function renderRuns() {
    var dlg = R.dialog;
    if (!dlg) return;
    var keep = document.activeElement && dlg.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
    var body;
    if (R.runsError && !R.runs) body = ui.errorState(R.runsError, loadRuns);
    else if (!R.runs) body = ui.loading('실행 기록을 불러오는 중이에요…');
    else if (!R.runs.length) body = el('p', { class: 'empty', text: '아직 실행 기록이 없어요. 새 실행으로 첫 브리프를 넣어 보세요.' });
    else body = el('ol', { class: 'run-list' }, R.runs.map(runRow));
    dlg.textContent = '';
    U.appendChildren(dlg, [
      el('header', { class: 'sheet-head' }, [
        el('div', null, [el('p', { class: 'eyebrow', text: '워크스페이스' }), el('h2', { id: 'runsTitle', text: '실행 기록' })]),
        el('button', { type: 'button', class: 'icon-btn', 'aria-label': '닫기', onclick: function () { dlg.close(); } }, closeIcon())
      ]),
      el('div', { class: 'runs-body' }, [
        el('p', { class: 'panel-hint', text: '서버를 다시 켜도 기록과 이벤트가 남아 있어요. 끝난 실행은 무대에서 다시 볼 수 있어요. 서버가 꺼져서 중단됐거나 직접 멈춘 실행은 ‘이어서 실행’을 누르면 끝난 채널은 건너뛰고 남은 작업만 해요.' }),
        R.dialogError ? el('p', { class: 'form-error', role: 'alert', text: R.dialogError }) : null,
        body
      ])
    ]);
    if (keep) { var n = dlg.querySelector('[data-key="' + keep + '"]'); if (n && !n.disabled) n.focus(); }
  }

  // ------------------------------------------------------------------ boot: follow what is going on
  /** Attach the stage to a running run, else open the newest pipeline / slot run (only once per page load). */
  function attach() {
    if (ws.mode !== 'live' || R.attached) return Promise.resolve(null);
    R.attached = true;
    return ws.get('/api/runs?limit=20').then(function (r) {
      var runs = ws.listOf(r, 'runs');
      var busy = runs.filter(function (x) { return x.active; })[0];
      if (busy) {
        I.studio.watchRun(busy.run_id, { title: '실시간 ' + kindLabel(busy.kind) + ' ' + busy.run_id });
        return busy;
      }
      if (I.studio.liveRunId() || R.watching) return null;  // the user already started something
      var last = runs.filter(function (x) { return (x.kind === 'pipeline' || x.kind === 'slot') && x.events; })[0];
      if (last) I.studio.watchRun(last.run_id, { history: true, title: '지난 ' + kindLabel(last.kind) + ' ' + last.run_id });
      return last || null;
    }, function () { R.attached = false; return null; });
  }

  function syncButton() {
    var b = document.getElementById('btnRuns');
    if (b) b.hidden = ws.mode !== 'live';
  }

  ws.runs = {
    attach: attach,
    open: openDialog,
    /** Called by the shell when the server mode changes (boot, login, logout). */
    sync: function () {
      syncButton();
      if (ws.mode !== 'live') { R.attached = false; if (R.dialog && R.dialog.open) R.dialog.close(); }
      renderBar();
    }
  };

  var btn = document.getElementById('btnRuns');
  if (btn) btn.addEventListener('click', function (e) { openDialog(e.currentTarget); });
})();
