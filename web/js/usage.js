/* INSIA 에이전트 스튜디오 — 사용량 (cost & usage).
 *
 * Month selector → GET /api/usage?since=&until=. Hero figure for the month's
 * total, stat tiles, the per-run budget cap, a per-day column chart (inline SVG:
 * one series, clean y ticks, axis titles, hover + keyboard tooltip, table view),
 * cost by task, and the per-run table. Numbers in columns use tabular figures.
 */
(function () {
  'use strict';

  var I = window.INSIA;
  var U = I.util;
  var ws = I.ws;
  var ui = ws.ui;
  var el = U.el;
  var D = ws.date;
  var SVGNS = 'http://www.w3.org/2000/svg';
  var TASK_LABEL = {
    plan: '기획', research: '리서치', structure: '리서치 정리', followup: '추가 리서치', draft: '초안 작성', review: '검수',
    revise: '수정', plan_calendar: '캘린더 계획', calendar: '캘린더 계획'
  };

  var S = { container: null, month: null, data: null, error: null, loading: false, focusDay: -1, ro: null, lastWidth: 0 };

  function show(container, param, ctx) {
    S.container = container;
    if (ws.mode !== 'live') {
      ws.mount(container, ui.demoExplainer('usage', '사용량',
        '실행마다 API 응답의 토큰 수로 비용을 계산해 쌓아 둬요. 이번 달에 얼마를 썼는지, 어떤 작업이 비용을 많이 쓰는지, 하루하루 얼마나 썼는지 볼 수 있어요.', [
          '이번 달 합계, 실행 수, 실행당 평균 비용, 토큰과 웹 검색 횟수를 한눈에 봐요.',
          '일별 비용 막대그래프와 표, 작업별(기획·리서치·작성·검수) 비용, 실행별 비용 표를 제공해요.',
          '실행당 예산 상한(INSIA_MAX_COST_USD 또는 --max-cost-usd)을 넘으면 남은 작업을 멈추고 끝난 채널은 지켜요.',
          '모의 실행은 비용이 0원이에요. 가격표는 INSIA_PRICE_* 환경 변수나 workspace/prices.json으로 고칠 수 있어요.'
        ]));
      if (ctx && ctx.focus) ui.focusHeading(document.getElementById('usageTitle'));
      return;
    }
    if (!S.month) S.month = D.monthStart(D.today());
    render(ctx && ctx.focus);
    load();
    if (window.ResizeObserver && !S.ro) {
      S.ro = new ResizeObserver(function () {
        var box = container.querySelector('.chart-box');
        var w = box ? box.clientWidth : 0;
        if (w && Math.abs(w - S.lastWidth) > 4) drawChart();
      });
      S.ro.observe(container);
    }
  }
  function reset() { S.data = null; }
  I.views.usage = { show: show, reset: reset };
  function isShown() { return S.container && !S.container.hidden && ws.route.view === 'usage'; }

  function load() {
    var month = S.month;
    S.loading = true;
    S.error = null;
    if (isShown()) markLoading();
    return ws.get('/api/usage' + ws.q({ since: month, until: D.monthEnd(month) })).then(function (resp) {
      if (month !== S.month) return;
      S.data = resp || {};
      S.loading = false;
      S.focusDay = -1;
      if (isShown()) render(false);
    }, function (err) {
      if (month !== S.month) return;
      S.loading = false;
      S.error = err;
      if (isShown()) render(false);
    });
  }
  function markLoading() {
    var b = S.container.querySelector('.usage-body');
    if (b) { b.classList.add('is-loading'); b.setAttribute('aria-busy', 'true'); }
  }

  // ------------------------------------------------------------------ derived numbers
  function runDate(r) {
    var iso = r.started_at || r.created_at || r.finished_at || '';
    if (iso) return iso;
    var m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(r.run_id || '');
    return m ? m[1] + '-' + m[2] + '-' + m[3] + 'T' + m[4] + ':' + m[5] + ':' + m[6] + 'Z' : '';
  }
  function kstDate(iso) {
    if (!iso) return '';
    if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) return iso;
    var t = Date.parse(iso);
    return isNaN(t) ? String(iso).slice(0, 10) : D.ymd(new Date(t + 9 * 3600 * 1000));
  }
  function tokens(r) { return (Number(r.input_tokens) || 0) + (Number(r.output_tokens) || 0); }
  function usd(v) { return typeof v === 'number' ? v : (v && typeof v === 'object' ? Number(v.usd != null ? v.usd : v.cost_usd) || 0 : Number(v) || 0); }

  function days() {
    var data = S.data || {};
    var byDay = {};
    (data.by_day || []).forEach(function (d) { byDay[d.date] = (byDay[d.date] || 0) + usd(d.usd != null ? d.usd : d.cost_usd); });
    var runsByDay = {};
    (data.runs || []).forEach(function (r) { var k = kstDate(runDate(r)); if (k) runsByDay[k] = (runsByDay[k] || 0) + 1; });
    var out = [];
    var end = D.monthEnd(S.month);
    for (var d = S.month; d <= end; d = D.add(d, 1)) out.push({ date: d, usd: byDay[d] || 0, runs: runsByDay[d] || 0 });
    return out;
  }

  // ------------------------------------------------------------------ render
  function render(focus) {
    var data = S.data || {};
    var isCurrent = S.month === D.monthStart(D.today());
    var monthLabel = S.month.slice(0, 4) + '년 ' + (+S.month.slice(5, 7)) + '월';
    var nav = el('div', { class: 'month-nav', role: 'group', 'aria-label': '기간' }, [
      el('button', { type: 'button', class: 'icon-btn', 'aria-label': '이전 달', 'data-key': 'prev', onclick: function () { S.month = D.addMonths(S.month, -1); render(false); load(); } }, arrow(-1)),
      el('span', { class: 'month-label', 'aria-live': 'polite', text: monthLabel }),
      el('button', { type: 'button', class: 'icon-btn', 'aria-label': '다음 달', 'data-key': 'next', disabled: isCurrent, onclick: function () { S.month = D.addMonths(S.month, 1); render(false); load(); } }, arrow(1))
    ]);
    var head = ui.viewHead('usage', '사용량', 'API 응답의 토큰 수로 계산한 비용이에요. 가격표 기준 추정치라 실제 청구액과 조금 다를 수 있어요.', [nav]);

    var body;
    if (S.error && !S.data) body = ui.errorState(S.error, function () { load(); });
    else if (!S.data) body = ui.loading('사용량을 불러오는 중이에요…');
    else body = usageBody(data, isCurrent, monthLabel);
    var keep = document.activeElement && S.container.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
    ws.mount(S.container, [head, el('div', { class: 'usage-body' + (S.loading ? ' is-loading' : ''), 'aria-busy': String(!!S.loading) }, body)]);
    drawChart();
    if (focus) ui.focusHeading(document.getElementById('usageTitle'));
    else if (keep) { var n = S.container.querySelector('[data-key="' + keep + '"]'); if (n && !n.disabled) n.focus(); }
  }
  function arrow(dir) {
    var s = document.createElementNS(SVGNS, 'svg');
    s.setAttribute('viewBox', '0 0 24 24');
    s.setAttribute('aria-hidden', 'true');
    s.setAttribute('focusable', 'false');
    var p = document.createElementNS(SVGNS, 'path');
    p.setAttribute('d', dir < 0 ? 'M15 5l-7 7 7 7' : 'M9 5l7 7-7 7');
    p.setAttribute('fill', 'none');
    p.setAttribute('stroke', 'currentColor');
    p.setAttribute('stroke-width', '2');
    p.setAttribute('stroke-linecap', 'round');
    p.setAttribute('stroke-linejoin', 'round');
    s.appendChild(p);
    return s;
  }

  function usageBody(data, isCurrent, monthLabel) {
    var runs = (data.runs || []).slice().sort(function (a, b) { return runDate(b) < runDate(a) ? -1 : runDate(b) > runDate(a) ? 1 : 0; });
    var total = usd(data.total_usd != null ? data.total_usd : runs.reduce(function (a, r) { return a + usd(r.usd != null ? r.usd : r.cost_usd); }, 0));
    var inTok = runs.reduce(function (a, r) { return a + (Number(r.input_tokens) || 0); }, 0);
    var outTok = runs.reduce(function (a, r) { return a + (Number(r.output_tokens) || 0); }, 0);
    var searches = runs.reduce(function (a, r) { return a + (Number(r.web_search_requests) || 0); }, 0);
    var budget = data.budget_usd != null ? Number(data.budget_usd) : ws.health && ws.health.budget_usd != null ? Number(ws.health.budget_usd) : null;

    var kpis = el('div', { class: 'kpi-row' }, [
      el('section', { class: 'card kpi kpi--hero', 'aria-label': (isCurrent ? '이번 달' : monthLabel) + ' 비용' }, [
        el('p', { class: 'kpi-label', text: (isCurrent ? '이번 달' : monthLabel) + ' 비용' }),
        el('p', { class: 'hero-figure', text: ws.fmtUsd(total) }),
        el('p', { class: 'kpi-foot', text: isCurrent ? D.monthDay(S.month) + '부터 오늘까지 · USD' : monthLabel + ' 전체 · USD' })
      ]),
      kpi('실행', U.fmtNum(runs.length) + '개', runs.length ? '실행당 평균 ' + ws.fmtUsd(total / runs.length) : '이 달에는 실행이 없어요'),
      kpi('토큰', ws.fmtCompact(inTok + outTok), '입력 ' + ws.fmtCompact(inTok) + ' · 출력 ' + ws.fmtCompact(outTok)),
      kpi('웹 검색', U.fmtNum(searches) + '회', '리서치 에이전트의 검색 요청'),
      el('section', { class: 'card kpi kpi--budget' }, [
        el('p', { class: 'kpi-label', text: '실행당 예산 상한' }),
        el('p', { class: 'kpi-value', text: budget ? ws.fmtUsd(budget) : '상한 없음' }),
        el('p', { class: 'kpi-foot' }, budget
          ? ['한 실행의 누적 비용이 넘으면 남은 작업을 멈추고, 끝난 채널은 저장해요. ', el('code', { text: 'INSIA_MAX_COST_USD' }), '로 바꿔요.']
          : [el('code', { text: 'INSIA_MAX_COST_USD' }), ' 또는 ', el('code', { text: '--max-cost-usd' }), '로 정하면 넘는 순간 실행을 멈춰요.'])
      ])
    ]);

    var dayRows = days();
    var peak = dayRows.reduce(function (a, d) { return d.usd > a.usd ? d : a; }, { usd: 0 });
    var chartSummary = monthLabel + ' 일별 비용 막대그래프. 합계 ' + ws.fmtUsd(total) + (peak.usd ? ', 가장 많이 쓴 날 ' + D.monthDay(peak.date) + ' ' + ws.fmtUsd(peak.usd) : ', 비용이 든 날 없음') + '. 화살표 키로 날짜를 옮기면 금액을 읽어 줘요.';
    var chart = el('section', { class: 'card chart-card', 'aria-labelledby': 'chartTitle' }, [
      el('div', { class: 'chart-head' }, [
        el('h3', { class: 'card-title', id: 'chartTitle', text: '일별 비용' }),
        el('p', { class: 'card-sub', text: 'USD · 한국 시간 기준 날짜' })
      ]),
      el('div', { class: 'chart-box', id: 'usageChart', tabindex: '0', role: 'group', 'aria-roledescription': '막대그래프', 'aria-label': chartSummary, 'data-key': 'chart' }, [
        el('div', { class: 'chart-tip', id: 'usageTip', role: 'status', 'aria-live': 'polite', hidden: true })
      ]),
      el('details', { class: 'table-view' }, [
        el('summary', { text: '표로 보기' }),
        el('div', { class: 'table-wrap' }, el('table', { class: 'data-table' }, [
          el('thead', null, el('tr', null, [el('th', { scope: 'col', text: '날짜' }), el('th', { scope: 'col', class: 'num', text: '비용' }), el('th', { scope: 'col', class: 'num', text: '실행' })])),
          el('tbody', null, dayRows.filter(function (d) { return d.usd || d.runs; }).map(function (d) {
            return el('tr', null, [el('td', { text: D.monthDay(d.date) + ' (' + D.weekday(d.date) + ')' }), el('td', { class: 'num', text: ws.fmtUsd(d.usd, 2) }), el('td', { class: 'num', text: U.fmtNum(d.runs) })]);
          }))
        ]))
      ])
    ]);

    var tasks = data.by_task && typeof data.by_task === 'object' ? Object.keys(data.by_task).map(function (k) {
      var v = data.by_task[k];
      return { task: k, usd: usd(v), input: v && v.input_tokens, output: v && v.output_tokens };
    }).sort(function (a, b) { return b.usd - a.usd; }) : [];
    var taskTotal = tasks.reduce(function (a, t) { return a + t.usd; }, 0) || 1;
    var taskCard = el('section', { class: 'card', 'aria-labelledby': 'taskTitle' }, [
      el('h3', { class: 'card-title', id: 'taskTitle', text: '작업별 비용' }),
      tasks.length ? el('div', { class: 'table-wrap' }, el('table', { class: 'data-table' }, [
        el('thead', null, el('tr', null, [
          el('th', { scope: 'col', text: '작업' }), el('th', { scope: 'col', class: 'num', text: '비용' }), el('th', { scope: 'col', class: 'num', text: '비중' }),
          el('th', { scope: 'col', class: 'num', text: '입력 토큰' }), el('th', { scope: 'col', class: 'num', text: '출력 토큰' })
        ])),
        el('tbody', null, tasks.map(function (t) {
          var pct = Math.round(100 * t.usd / taskTotal);
          return el('tr', null, [
            el('td', null, [TASK_LABEL[t.task] || t.task, el('span', { class: 'share', 'aria-hidden': 'true' }, el('i', { style: 'width:' + pct + '%' }))]),
            el('td', { class: 'num', text: ws.fmtUsd(t.usd, 2) }),
            el('td', { class: 'num', text: pct + '%' }),
            el('td', { class: 'num', text: t.input != null ? U.fmtNum(Number(t.input)) : '—' }),
            el('td', { class: 'num', text: t.output != null ? U.fmtNum(Number(t.output)) : '—' })
          ]);
        }))
      ])) : el('p', { class: 'empty', text: '작업별 기록이 없어요.' })
    ]);

    var runCard = el('section', { class: 'card', 'aria-labelledby': 'runTitle' }, [
      el('h3', { class: 'card-title', id: 'runTitle', text: '실행별 비용' }),
      runs.length ? el('div', { class: 'table-wrap' }, el('table', { class: 'data-table' }, [
        el('thead', null, el('tr', null, [
          el('th', { scope: 'col', text: '실행' }), el('th', { scope: 'col', text: '시작' }), el('th', { scope: 'col', class: 'num', text: '비용' }),
          el('th', { scope: 'col', class: 'num', text: '입력 토큰' }), el('th', { scope: 'col', class: 'num', text: '출력 토큰' }),
          el('th', { scope: 'col', class: 'num', text: '캐시 읽기' }), el('th', { scope: 'col', class: 'num', text: '웹 검색' })
        ])),
        el('tbody', null, runs.map(function (r) {
          var over = budget && usd(r.usd != null ? r.usd : r.cost_usd) > budget;
          return el('tr', { 'data-over': over ? 'true' : null }, [
            el('td', null, [el('span', { class: 'mono', text: r.run_id || '' }), r.topic ? el('span', { class: 'run-topic', text: r.topic }) : null, r.kind && r.kind !== 'pipeline' ? el('span', { class: 'run-kind', text: r.kind }) : null]),
            el('td', { class: 'nowrap', text: ws.date.dateTime(runDate(r)) }),
            el('td', { class: 'num', text: ws.fmtUsd(usd(r.usd != null ? r.usd : r.cost_usd), 2) + (over ? ' · 상한 초과' : '') }),
            el('td', { class: 'num', text: U.fmtNum(Number(r.input_tokens) || 0) }),
            el('td', { class: 'num', text: U.fmtNum(Number(r.output_tokens) || 0) }),
            el('td', { class: 'num', text: U.fmtNum(Number(r.cache_read_tokens) || 0) }),
            el('td', { class: 'num', text: U.fmtNum(Number(r.web_search_requests) || 0) })
          ]);
        }))
      ])) : el('p', { class: 'empty', text: '이 달에 기록된 실행이 없어요.' })
    ]);

    return [
      kpis,
      chart,
      el('div', { class: 'usage-tables' }, [taskCard, runCard]),
      el('p', { class: 'usage-note' }, [
        '비용 = 토큰 수 × 모델 가격표(백만 토큰당 USD) + 웹 검색 1,000회당 $10. 가격이 바뀌면 ',
        el('code', { text: 'INSIA_PRICE_*' }), ' 환경 변수나 ', el('code', { text: 'workspace/prices.json' }), '으로 고쳐요. 모의 실행은 비용이 0이에요.'
      ])
    ];
  }

  function kpi(label, value, foot) {
    return el('section', { class: 'card kpi' }, [
      el('p', { class: 'kpi-label', text: label }),
      el('p', { class: 'kpi-value', text: value }),
      foot ? el('p', { class: 'kpi-foot', text: foot }) : null
    ]);
  }

  // ------------------------------------------------------------------ chart
  function niceStep(max) {
    if (max <= 0) return 0.25;
    var raw = max / 4;
    var pow = Math.pow(10, Math.floor(Math.log10(raw)));
    var n = raw / pow;
    var step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
    return step * pow;
  }
  function svg(tag, attrs, text) {
    var n = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function tickLabel(v, step) { return '$' + v.toFixed(step < 0.1 ? 2 : step < 1 ? (Math.round(step * 100) % 10 ? 2 : 1) : 0); }

  function drawChart() {
    var box = document.getElementById('usageChart');
    if (!box || !S.data) return;
    var old = box.querySelector('svg');
    if (old) old.remove();
    var W = box.clientWidth || 600;
    S.lastWidth = W;
    var H = W < 480 ? 216 : 256;
    var rows = days();
    var max = rows.reduce(function (a, d) { return Math.max(a, d.usd); }, 0);
    var step = niceStep(max);
    var top = Math.max(step, Math.ceil(max / step - 1e-9) * step);
    var ticks = [];
    for (var v = 0; v <= top + 1e-9; v += step) ticks.push(v);
    var ml = 48, mr = 8, mt = 28, mb = 40;
    var pw = Math.max(40, W - ml - mr), ph = H - mt - mb;
    var band = pw / rows.length;
    var bw = Math.max(2, Math.min(24, band - 2));
    var y = function (val) { return mt + ph - (top ? val / top * ph : 0); };

    var s = svg('svg', { class: 'chart-svg', width: W, height: H, viewBox: '0 0 ' + W + ' ' + H, 'aria-hidden': 'true', focusable: 'false' });
    // gridlines + y ticks
    ticks.forEach(function (t) {
      var yy = Math.round(y(t)) + 0.5;
      s.appendChild(svg('line', { class: t === 0 ? 'axis-line' : 'grid-line', x1: ml, x2: W - mr, y1: yy, y2: yy }));
      s.appendChild(svg('text', { class: 'tick', x: ml - 8, y: yy + 4, 'text-anchor': 'end' }, tickLabel(t, step)));
    });
    // bars (square at the baseline, 4px rounded data end)
    var bars = [];
    rows.forEach(function (d, i) {
      var cx = ml + band * i + band / 2;
      if (d.usd > 0) {
        var h = Math.max(1.5, mt + ph - y(d.usd));
        var x0 = cx - bw / 2, y0 = mt + ph - h, r = Math.min(4, h, bw / 2);
        var path = 'M' + x0 + ' ' + (mt + ph) + 'V' + (y0 + r) + 'Q' + x0 + ' ' + y0 + ' ' + (x0 + r) + ' ' + y0 + 'H' + (x0 + bw - r) + 'Q' + (x0 + bw) + ' ' + y0 + ' ' + (x0 + bw) + ' ' + (y0 + r) + 'V' + (mt + ph) + 'Z';
        bars[i] = s.appendChild(svg('path', { class: 'bar', d: path }));
      }
      // x labels: every day when there is room, else odd days, else 1 · 5 · 10 …; the last day always,
      // minus the label right before it so the two never collide
      var dayNum = +d.date.slice(8);
      var last = i === rows.length - 1;
      var every = band < 14 ? 5 : band < 26 ? 2 : 1;
      var label = every === 1 || dayNum === 1 || (every === 2 ? dayNum % 2 === 1 : dayNum % every === 0);
      if (!last && every > 1 && rows.length - dayNum < 2) label = false;
      if (label || last) {
        s.appendChild(svg('text', { class: 'tick', x: cx, y: mt + ph + 16, 'text-anchor': 'middle' }, String(dayNum)));
      }
    });
    // axis titles
    s.appendChild(svg('text', { class: 'axis-title', x: ml + pw / 2, y: H - 4, 'text-anchor': 'middle' }, (+S.month.slice(5, 7)) + '월 날짜 (일)'));
    s.appendChild(svg('text', { class: 'axis-title', x: 0, y: 12, 'text-anchor': 'start' }, '비용 (USD)'));
    // hover cursor band
    var cursor = svg('rect', { class: 'cursor', x: 0, y: mt, width: band, height: ph, visibility: 'hidden' });
    s.insertBefore(cursor, s.firstChild);
    box.insertBefore(s, box.firstChild);

    var tip = document.getElementById('usageTip');
    function focusDay(i, announce) {
      if (i < 0 || i >= rows.length) { hide(); return; }
      S.focusDay = i;
      var d = rows[i];
      cursor.setAttribute('x', String(ml + band * i));
      cursor.setAttribute('visibility', 'visible');
      bars.forEach(function (b, k) { if (b) b.classList.toggle('is-hot', k === i); });
      tip.textContent = '';
      U.appendChildren(tip, [el('b', { text: ws.fmtUsd(d.usd, 2) }), el('span', { text: D.monthDay(d.date) + ' (' + D.weekday(d.date) + ')' + (d.runs ? ' · 실행 ' + d.runs + '개' : '') })]);
      tip.hidden = false;
      var tx = ml + band * i + band / 2;
      var tw = tip.offsetWidth || 120;
      tip.style.left = Math.max(0, Math.min(W - tw, tx - tw / 2)) + 'px';
      tip.style.top = Math.max(0, (d.usd > 0 ? y(d.usd) : mt + ph) - 52) + 'px';
      if (!announce) tip.setAttribute('aria-live', 'off'); else tip.setAttribute('aria-live', 'polite');
    }
    function hide() {
      cursor.setAttribute('visibility', 'hidden');
      bars.forEach(function (b) { if (b) b.classList.remove('is-hot'); });
      tip.hidden = true;
    }
    box.onpointermove = function (e) {
      var rect = box.getBoundingClientRect();
      var i = Math.floor((e.clientX - rect.left - ml) / band);
      if (i >= 0 && i < rows.length) focusDay(i, false); else hide();
    };
    box.onpointerleave = function () { if (document.activeElement !== box) hide(); else focusDay(S.focusDay, false); };
    box.onfocus = function () { var i = S.focusDay >= 0 ? S.focusDay : lastWithCost(rows); focusDay(i, true); };
    box.onblur = hide;
    box.onkeydown = function (e) {
      var i = S.focusDay >= 0 ? S.focusDay : lastWithCost(rows);
      if (e.key === 'ArrowRight') i = Math.min(rows.length - 1, i + 1);
      else if (e.key === 'ArrowLeft') i = Math.max(0, i - 1);
      else if (e.key === 'Home') i = 0;
      else if (e.key === 'End') i = rows.length - 1;
      else return;
      e.preventDefault();
      focusDay(i, true);
    };
    if (document.activeElement === box && S.focusDay >= 0) focusDay(S.focusDay, false);
  }
  function lastWithCost(rows) {
    for (var i = rows.length - 1; i >= 0; i--) if (rows[i].usd > 0) return i;
    return 0;
  }
})();
