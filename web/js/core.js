/* INSIA 에이전트 스튜디오 — workspace core (INSIA.ws).
 *
 * Shared by the 보관함 / 캘린더 / 브랜드·자료 / 사용량 views: the API client (with
 * the 401 → login hand-off), KST date helpers, formatters, small UI builders,
 * character counting that mirrors src/insia_agents/channels.py, a line diff,
 * and job tracking that streams item jobs into the studio stage.
 *
 * Classic script (no modules) so build_artifact.py can inline it. Loaded after
 * app.js; every model or user string reaches the DOM through textContent.
 */
(function () {
  'use strict';

  var I = window.INSIA;
  var U = I.util;
  var el = U.el;
  var ws = I.ws = {
    mode: 'demo',        // live | demo | auth
    health: null,
    authMessage: '',
    go: function () {},  // set by shell.js
    route: { view: 'studio', param: '' }
  };

  // ------------------------------------------------------------------ labels
  var STATUS = {
    draft: { label: '초안', hint: '사람 검토 전' },
    needs_changes: { label: '수정 필요', hint: '검수를 통과하지 못했어요' },
    approved: { label: '승인됨', hint: '게시해도 좋다고 승인했어요' },
    scheduled: { label: '게시 예정', hint: '게시 날짜를 정했어요' },
    published: { label: '게시 완료', hint: '채널에 올렸어요' },
    archived: { label: '보관됨', hint: '목록에서 뺐어요' }
  };
  var STATUS_ORDER = ['draft', 'needs_changes', 'approved', 'scheduled', 'published', 'archived'];
  var SLOT_STATUS = { planned: '계획', generating: '작성 중', drafted: '초안 있음', skipped: '건너뜀' };
  var CHANNEL_SHORT = { bizplan: '사업계획서', naver_blog: '블로그', linkedin: '링크드인', instagram: '인스타' };
  var CHANNEL_COLOR = { bizplan: '#3B5BDB', naver_blog: '#03C75A', linkedin: '#0A66C2', instagram: '#E1306C' };
  ws.STATUS = STATUS;
  ws.STATUS_ORDER = STATUS_ORDER;
  ws.SLOT_STATUS = SLOT_STATUS;
  ws.CHANNEL_SHORT = CHANNEL_SHORT;
  ws.LOCAL_CMD = 'pip install -e . && insia serve';

  function channelColor(ch) {
    var m = U.manifest().channels || {};
    return (m[ch] && m[ch].color) || CHANNEL_COLOR[ch] || '#8C9BBD';
  }
  ws.channelColor = channelColor;

  // ------------------------------------------------------------------ API client
  /**
   * JSON request to the local API. Resolves with the parsed body (null for 204).
   * Rejects with an Error whose .message is the server's Korean `error` text and
   * .status the HTTP status. A 401 switches the app to the login view first.
   */
  function api(method, path, body) {
    var opts = {
      method: method, mode: 'same-origin', credentials: 'same-origin', cache: 'no-store',
      headers: { Accept: 'application/json' }
    };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (r) {
      if (r.status === 204) return null;
      var ct = r.headers.get('content-type') || '';
      var parse = ct.indexOf('json') >= 0 ? r.json().catch(function () { return null; }) : Promise.resolve(null);
      return parse.then(function (j) {
        if (r.ok) return j;
        var msg = (j && (j.error || j.detail)) || httpMessage(r.status);
        var err = new Error(msg);
        err.status = r.status;
        err.data = j || null;  // the JSON body: e.g. a 409's can_force / recovered / run_status
        if (r.status === 401) {
          err.auth = true;
          ws.requireLogin(msg);
        }
        throw err;
      });
    }, function () {
      var err = new Error('서버에 연결하지 못했어요. insia serve가 켜져 있는지 확인해 주세요.');
      err.status = 0;
      throw err;
    });
  }
  function httpMessage(status) {
    if (status === 404) return '서버가 이 기능을 아직 지원하지 않아요 (404). insia를 최신 버전으로 업데이트해 주세요.';
    if (status === 429) return '동시에 실행할 수 있는 작업 수를 넘었어요. 잠시 후 다시 시도해 주세요.';
    if (status >= 500) return '서버에서 요청을 처리하지 못했어요 (' + status + '). 서버 로그를 확인해 주세요.';
    return '요청을 처리하지 못했어요 (' + status + ').';
  }
  ws.api = api;
  ws.get = function (path) { return api('GET', path); };
  ws.post = function (path, body) { return api('POST', path, body === undefined ? {} : body); };
  ws.put = function (path, body) { return api('PUT', path, body); };
  ws.del = function (path) { return api('DELETE', path); };

  /** `{key: value}` wrappers and bare values are both accepted (the API shape is `{items: [...]}` etc.). */
  ws.unwrap = function (obj, key) {
    if (obj && typeof obj === 'object' && !Array.isArray(obj) && obj[key] !== undefined) return obj[key];
    return obj;
  };
  ws.listOf = function (obj, key) {
    var v = ws.unwrap(obj, key);
    return Array.isArray(v) ? v : [];
  };
  ws.q = function (params) {
    var parts = [];
    Object.keys(params).forEach(function (k) {
      if (params[k] !== undefined && params[k] !== null && params[k] !== '') parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
    });
    return parts.length ? '?' + parts.join('&') : '';
  };

  // ------------------------------------------------------------------ dates (KST)
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function ymdOf(d) { return d.getUTCFullYear() + '-' + pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate()); }
  function parseYmd(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ''));
    return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])) : null;
  }
  /** Today's date in Korea (the founder's calendar), YYYY-MM-DD. */
  function todayKst() {
    try {
      var parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
      if (/^\d{4}-\d{2}-\d{2}$/.test(parts)) return parts;
    } catch (e) { /* old browser */ }
    var d = new Date(Date.now() + 9 * 3600 * 1000);
    return ymdOf(d);
  }
  function addDays(ymd, n) { var d = parseYmd(ymd); d.setUTCDate(d.getUTCDate() + n); return ymdOf(d); }
  function weekStart(ymd) { var d = parseYmd(ymd); var wd = (d.getUTCDay() + 6) % 7; d.setUTCDate(d.getUTCDate() - wd); return ymdOf(d); }
  function monthStart(ymd) { return String(ymd).slice(0, 8) + '01'; }
  function monthEnd(ymd) { var d = parseYmd(monthStart(ymd)); d.setUTCMonth(d.getUTCMonth() + 1); d.setUTCDate(0); return ymdOf(d); }
  function addMonths(ymd, n) { var d = parseYmd(monthStart(ymd)); d.setUTCMonth(d.getUTCMonth() + n); return ymdOf(d); }
  var WEEKDAYS = ['일', '월', '화', '수', '목', '금', '토'];
  function weekday(ymd) { var d = parseYmd(ymd); return d ? WEEKDAYS[d.getUTCDay()] : ''; }
  function fmtMonthDay(ymd) { var d = parseYmd(ymd); return d ? (d.getUTCMonth() + 1) + '월 ' + d.getUTCDate() + '일' : ''; }
  /** ISO timestamp (UTC or offset) → "9월 28일 17:10" in KST; a bare date → "9월 28일". */
  function fmtDateTime(iso) {
    if (!iso) return '';
    if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) return fmtMonthDay(iso);
    var t = Date.parse(iso);
    if (isNaN(t)) return String(iso);
    var k = new Date(t + 9 * 3600 * 1000);
    var sameYear = k.getUTCFullYear() === +todayKst().slice(0, 4);
    return (sameYear ? '' : k.getUTCFullYear() + '년 ') + (k.getUTCMonth() + 1) + '월 ' + k.getUTCDate() + '일 ' + pad2(k.getUTCHours()) + ':' + pad2(k.getUTCMinutes());
  }
  ws.date = {
    today: todayKst, add: addDays, weekStart: weekStart, monthStart: monthStart, monthEnd: monthEnd, addMonths: addMonths,
    parse: parseYmd, ymd: ymdOf, weekday: weekday, monthDay: fmtMonthDay, dateTime: fmtDateTime
  };

  // ------------------------------------------------------------------ numbers
  ws.fmtUsd = function (n, digits) {
    n = Number(n) || 0;
    if (n > 0 && n < 0.01 && digits === undefined) return '<$0.01';
    return '$' + n.toFixed(digits === undefined ? 2 : digits).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  };
  ws.fmtCompact = function (n) {
    n = Number(n) || 0;
    try { return new Intl.NumberFormat('ko-KR', { notation: 'compact', maximumFractionDigits: 1 }).format(n); } catch (e) { return U.fmtNum(n); }
  };

  // ------------------------------------------------------------------ UI builders
  function statusPill(status) {
    var s = STATUS[status] || { label: status || '알 수 없음', hint: '' };
    return el('span', { class: 'status-pill', 'data-status': status || 'unknown', title: s.hint || null }, [el('span', { class: 'sp-dot', 'aria-hidden': 'true' }), s.label]);
  }
  function scoreBadge(score, passed) {
    if (typeof score !== 'number') return el('span', { class: 'score-badge', 'data-state': 'none', text: '검수 전' });
    return el('span', { class: 'score-badge', 'data-state': passed ? 'pass' : 'fail', title: passed ? '검수 통과' : '검수 미통과' }, [
      el('b', { text: String(score) }), '점', el('span', { class: 'sb-verdict', text: passed ? ' 통과' : ' 미통과' })
    ]);
  }
  /** An approval that skipped the review gate ("그래도 승인") stays on record while the item is approved, scheduled or published. */
  function isForcedApproval(item) {
    return !!(item && item.approval_forced && (item.status === 'approved' || item.status === 'scheduled' || item.status === 'published'));
  }
  function forcedWhy(item) {
    var v = Number(item.approved_version) || 0;
    var score = typeof item.approved_score === 'number' ? item.approved_score + '점, 통과 기준 미달' : '검수 전';
    return '검수를 통과하지 않은 버전(' + (v ? 'v' + v + ', ' : '') + score + ')을 사람이 그래도 승인했어요';
  }
  function forcedBadge(item) {
    if (!isForcedApproval(item)) return null;
    var why = forcedWhy(item);
    return el('span', { class: 'forced-pill', 'data-key': 'forced-approval', title: why }, [
      el('span', { 'aria-hidden': 'true', text: '!' }), '강제 승인', el('span', { class: 'sr-only', text: ' · ' + why })
    ]);
  }
  ws.isForcedApproval = isForcedApproval;
  ws.forcedWhy = forcedWhy;

  /**
   * Korean explanation when a job's or run's result did not become the item's current version because a
   * person (or another job) saved a version meanwhile (run.completed / JobResult: superseded,
   * superseded_by_human_edit, version, current_version; pipeline runs: superseded_channels). '' otherwise.
   */
  /** run.completed carries the job's version number; a JobResult (GET /api/runs/<id> → job) carries the DraftVersion. */
  ws.versionNumber = function (v) { return Number(v && typeof v === 'object' ? v.version : v) || 0; };
  ws.supersededText = function (data) {
    if (!data || !(data.superseded || data.superseded_by_human_edit)) return '';
    var who = data.superseded_by_human_edit ? '사람이 고친 버전' : '새 버전';
    var cur = Number(data.current_version) || 0;
    var ver = ws.versionNumber(data.version);
    function num(n) { return n ? '(v' + n + ')' : ''; }  // "버전(v5)이": the particle follows the word, not the number
    if (data.kind === 'review') {
      return '검수하는 동안 ' + who + num(cur) + '이 저장됐어요. 이 점수는 검수한 버전' + num(ver) + '에만 붙었고, 현재 버전' + num(cur) +
        '은 아직 검수 전이에요.';
    }
    if (data.kind === 'revise') {
      return '수정하는 동안 ' + who + '이 저장돼서, 에이전트의 수정 결과는 버전 기록' + num(ver) + '에만 남겼어요. 현재 버전' + num(cur) + '은 ' +
        (data.superseded_by_human_edit ? '사람이 고친 내용' : '그 새 버전') + ' 그대로예요. 수정 결과를 쓰려면 버전 기록에서 비교한 뒤 직접 옮겨 주세요.';
    }
    var chans = data.superseded_channels && typeof data.superseded_channels === 'object' ? Object.keys(data.superseded_channels) : [];
    var names = chans.map(function (c) { return U.chName(c); }).join(', ');
    return (names ? names + ': ' : '') + '실행하는 동안 ' + who + '이 있어서, 에이전트 결과는 기록에만 남기고 현재 버전은 그대로 뒀어요. 보관함 버전 기록에서 비교할 수 있어요.';
  };
  /** Same draft text from the same source (the change log aside: a copy put back on top starts with a note). */
  ws.sameVersionText = function (a, b) {
    if (!a || !b || !a.draft || !b.draft || a.source !== b.source) return false;
    var x = Object.assign({}, a.draft), y = Object.assign({}, b.draft);
    delete x.change_log; delete y.change_log;
    return JSON.stringify(x) === JSON.stringify(y);
  };
  /**
   * Resolves with ws.supersededText(data) once the item confirms it, else ''. A review/revise job can report
   * "superseded" while its own text is in fact current: a run (e.g. `insia run-due` in a terminal) kept the job's
   * version current by putting a copy of it on top (same text, change-log note), and the job's review follows the
   * copy. Pipeline runs are judged by the server already.
   */
  ws.confirmKept = function (data, itemId) {
    var text = ws.supersededText(data);
    var ver = ws.versionNumber(data && data.version);
    if (!text || !itemId || !ver || ws.mode !== 'live' || !data || (data.kind !== 'review' && data.kind !== 'revise')) return Promise.resolve(text);
    return ws.get('/api/items/' + encodeURIComponent(itemId)).then(function (detail) {
      var vs = (detail && detail.versions) || [];
      var latest = vs[vs.length - 1];
      var mine = vs.filter(function (x) { return x.version === ver; })[0];
      if (latest && mine && (latest.version === mine.version || ws.sameVersionText(latest, mine))) return '';
      return text;
    }, function () { return text; });
  };

  function channelIcon(ch, cls) {
    var m = (U.manifest().channels || {})[ch] || {};
    var fallback = function () {
      return el('span', { class: (cls || 'item-icon') + ' ch-icon-fallback', style: '--ch:' + channelColor(ch), 'aria-hidden': 'true', text: U.chName(ch).charAt(0) });
    };
    var src = m.icon && U.assetUrl(m.icon);
    if (!src) return fallback();
    var img = el('img', { class: cls || 'item-icon', src: src, alt: '', decoding: 'async' });
    img.addEventListener('error', function () { if (img.parentNode) img.replaceWith(fallback()); });
    return img;
  }
  function viewHead(idBase, title, sub, tools) {
    return el('header', { class: 'view-head' }, [
      el('div', { class: 'view-head-text' }, [
        el('h2', { class: 'view-title', id: idBase + 'Title', tabindex: '-1', text: title }),
        sub ? el('p', { class: 'view-sub', text: sub }) : null
      ]),
      tools ? el('div', { class: 'view-tools' }, tools) : null
    ]);
  }
  /** kind: info | success | warn | error */
  function notice(kind, children, extraClass) {
    return el('div', { class: 'notice' + (extraClass ? ' ' + extraClass : ''), 'data-kind': kind || 'info' }, children);
  }
  function cmdBlock(text) {
    var status = el('span', { class: 'copy-status', role: 'status' });
    var fallback = el('textarea', { class: 'copy-fallback', rows: '2', readonly: true, 'aria-label': '복사할 명령어', hidden: true });
    return el('div', { class: 'cmd-wrap' }, [
      el('div', { class: 'cmd' }, [
        el('code', { text: text }),
        el('button', { type: 'button', class: 'btn btn--small', text: '복사', onclick: function () { U.copyText(text, status, fallback); } })
      ]),
      status, fallback
    ]);
  }
  /** Demo/artifact explainer for views that need the local app. */
  function demoExplainer(idBase, title, lead, bullets) {
    return el('div', { class: 'explainer' }, [
      el('p', { class: 'eyebrow', text: '로컬 앱에서 쓰는 기능' }),
      el('h2', { class: 'view-title', id: idBase + 'Title', tabindex: '-1', text: title }),
      el('p', { class: 'explainer-lead', text: lead }),
      el('ul', { class: 'explainer-list' }, bullets.map(function (b) { return el('li', { text: b }); })),
      el('h3', { class: 'sub-title', text: '내 컴퓨터에서 실행하기' }),
      cmdBlock(ws.LOCAL_CMD),
      el('p', { class: 'explainer-note' }, [
        '저장소 폴더에서 위 명령을 실행한 뒤 ', el('code', { text: 'http://127.0.0.1:8765' }),
        '을 열면 돼요. API 키가 없으면 모의 실행으로 돌아가고, 기록은 workspace 폴더에 저장돼요. 이 데모 페이지는 아무것도 저장하지 않아요.'
      ])
    ]);
  }
  function errorState(err, retry) {
    return notice('error', [
      el('b', { text: '불러오지 못했어요' }), ' · ' + ((err && err.message) || '알 수 없는 오류'),
      retry ? el('button', { type: 'button', class: 'btn btn--small notice-action', text: '다시 시도', onclick: retry }) : null
    ]);
  }
  function loading(text) { return el('p', { class: 'loading', role: 'status', text: text || '불러오는 중이에요…' }); }

  var toastTimer = null;
  function toast(msg, kind) {
    var t = document.getElementById('toast');
    if (!t) return;
    t.textContent = msg;
    t.dataset.kind = kind || 'info';
    t.classList.add('is-on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove('is-on'); }, 5200);
  }

  /** Focus a view heading after navigation without scrolling the page twice. */
  function focusHeading(node) {
    if (node && typeof node.focus === 'function') {
      try { node.focus({ preventScroll: false }); } catch (e) { node.focus(); }
    }
  }

  ws.ui = {
    statusPill: statusPill, scoreBadge: scoreBadge, forcedBadge: forcedBadge, channelIcon: channelIcon, viewHead: viewHead, notice: notice,
    cmdBlock: cmdBlock, demoExplainer: demoExplainer, errorState: errorState, loading: loading, toast: toast, focusHeading: focusHeading
  };

  /** Replace a container's children in one go. */
  ws.mount = function (container, nodes) {
    container.textContent = '';
    U.appendChildren(container, nodes);
  };

  // ------------------------------------------------------------------ text measurement (mirrors channels.py)
  var LIMITS = {
    bizplan: { minNs: 3000, maxNs: 15000 },
    naver_blog: { minNs: 1500, maxNs: 3000, minHeadings: 3, minImages: 3, minTags: 5, maxTags: 10, maxTitle: 40 },
    linkedin: { min: 1300, max: 2000, hardMax: 3000, maxHook: 210, minTags: 3, maxTags: 5 },
    instagram: { minSlides: 7, maxSlides: 10, maxCaption: 2200, maxHook: 125, minTags: 3, maxTags: 5 }
  };
  ws.LIMITS = LIMITS;
  var LINE_BREAK = /\r\n|[\n\r\u000b\u000c\u001c\u001d\u001e\u0085\u2028\u2029]/;
  function cpLen(s) { return Array.from(s).length; }  // Python len() counts code points
  function charsWithSpace(t) { return cpLen(String(t || '').trim()); }
  function charsNoSpace(t) { return cpLen(String(t || '').replace(/\s/g, '')); }
  function firstLines(t, n) {
    return String(t || '').trim().split(LINE_BREAK).map(function (l) { return l.trim(); }).filter(Boolean).slice(0, n).join('\n');
  }
  function countMatches(re, t) { var m = String(t || '').match(re); return m ? m.length : 0; }
  function section(t, heading) {
    var text = String(t || '');
    var re = new RegExp('^##\\s*' + heading + '\\s*$', 'm');
    var m = re.exec(text);
    if (!m) return '';
    var rest = text.slice(m.index + m[0].length);
    var nxt = /^##\s+\S/m.exec(rest);
    return (nxt ? rest.slice(0, nxt.index) : rest).trim();
  }
  function n0(v) { return U.fmtNum(v); }
  function chk(id, label, passed, value, expected) { return { id: id, label: label, passed: !!passed, value: String(value), expected: expected }; }
  function inRange(v, lo, hi) { return v >= lo && v <= hi; }

  function normSpace(t) { return String(t || '').replace(/\s/g, '').toLowerCase(); }
  /** channels.profile_checks: banned words (all), required phrases (SNS only; title + body, not the tag list), blind names (bizplan). */
  function profileChecks(channel, draft, profile, opts) {
    var out = [];
    if (!profile) return out;
    var flat = normSpace(String(draft.title || '') + '\n' + String(draft.content || ''));
    var banned = (profile.banned_words || []).map(function (w) { return String(w).trim(); }).filter(Boolean);
    if (banned.length) {
      var hits = banned.filter(function (w) { var n = normSpace(w); return n && flat.indexOf(n) >= 0; });
      out.push(chk('banned_words', '금지 표현 없음', !hits.length, hits.length ? hits.join(', ') : '없음', '프로필의 금지 표현을 쓰지 않음'));
    }
    if (channel !== 'bizplan') {
      var required = (profile.required_phrases || []).map(function (w) { return String(w).trim(); }).filter(Boolean);
      if (required.length) {
        var missing = required.filter(function (w) { return flat.indexOf(normSpace(w)) < 0; });
        out.push(chk('required_phrases', '필수 문구 포함', !missing.length, missing.length ? '누락: ' + missing.join(', ') : '모두 포함', '프로필의 필수 문구를 넣음'));
      }
    } else {
      var team = profile.team || [];
      var names = team.map(function (m) { return String((m && m.name) || '').trim(); }).filter(function (n) { return n.length >= 2; });
      var exposed = names.filter(function (n) { return flat.indexOf(normSpace(n)) >= 0; });
      // The browser only checks team names. School and employer names from the team backgrounds ("카카오 출신",
      // "고려대 졸업") are matched by the server's rules (prompt_loader.blind_leaks, a large heuristic that is not
      // ported): the check is marked partial (never "통과") and says where the full verdict comes from — the save
      // result for an edit, a 재검수 for a saved version (opts.saved).
      var backgrounds = team.some(function (m) { return String((m && m.background) || '').trim(); });
      var value = exposed.length ? '실명 ' + exposed.length + '개 노출' : backgrounds ? '실명 노출 없음' : '노출 없음';
      var c = chk('blind_names', '블라인드(실명 미노출)', !exposed.length, value, '팀원 실명은 ○○로 가림');
      if (backgrounds && !exposed.length) {
        c.partial = true;
        c.note = '팀 배경의 학교·직장명 노출은 아직 확인 전이에요 (브라우저는 팀원 실명만 확인해요). ' +
          (opts && opts.saved ? '재검수하면 서버가 확인해요.' : '저장하면 서버가 확인해서 결과를 보여 줘요.');
      }
      out.push(c);
    }
    return out;
  }

  /**
   * Same checks and units as channels.check_format: 공백 제외 for bizplan/naver_blog content,
   * 공백 포함 for linkedin and the instagram caption, plus the company-profile brand checks
   * when `profile` is given. Returns FormatCheck-shaped objects; a check the browser can only do in part
   * (the bizplan blind rule's school/employer names) has `partial: true` and a Korean `note`.
   * opts.saved: the draft is an already saved version (the note then points to 재검수, not to saving).
   */
  function measure(channel, draft, brief, profile, opts) {
    var content = String(draft.content || '');
    var tags = (draft.hashtags || []).filter(function (x) { return String(x).trim(); });
    var L = LIMITS[channel];
    var out = [];
    if (!L) return out;
    if (channel === 'bizplan') {
      var n = charsNoSpace(content);
      out.push(chk('length', '분량(공백 제외)', inRange(n, L.minNs, L.maxNs), n0(n) + '자', n0(L.minNs) + '~' + n0(L.maxNs) + '자'));
      var headingText = [];
      content.replace(/^(#{1,6})\s+(.*)$/gm, function (m, h, txt) { headingText.push(txt.replace(/ /g, '')); return m; });
      var joined = headingText.join(' ');
      [['문제인식', '문제 인식'], ['실현가능성', '실현 가능성'], ['성장전략', '성장 전략'], ['팀구성', '팀 구성']].forEach(function (v) {
        var found = v.some(function (x) { return joined.indexOf(x.replace(/ /g, '')) >= 0; });
        out.push(chk('psst_' + v[0], "'" + v[v.length - 1] + "' 섹션", found, found ? '있음' : '없음', '제목(#)으로 포함'));
      });
    } else if (channel === 'naver_blog') {
      var nb = charsNoSpace(content);
      out.push(chk('length', '분량(공백 제외)', inRange(nb, L.minNs, L.maxNs), n0(nb) + '자', n0(L.minNs) + '~' + n0(L.maxNs) + '자'));
      var h = countMatches(/^##\s+\S/gm, content);
      out.push(chk('headings', '소제목(##) 수', h >= L.minHeadings, h + '개', L.minHeadings + '개 이상'));
      var imgs = countMatches(/\[이미지/g, content);
      out.push(chk('images', '이미지 자리 [이미지: …]', imgs >= L.minImages, imgs + '개', L.minImages + '개 이상'));
      out.push(chk('tags', '태그 수', inRange(tags.length, L.minTags, L.maxTags), tags.length + '개', L.minTags + '~' + L.maxTags + '개'));
      var tl = charsWithSpace(draft.title);
      out.push(chk('title_length', '제목 길이', tl > 0 && tl <= L.maxTitle, tl + '자', L.maxTitle + '자 이하'));
      if (brief && brief.keywords && brief.keywords.length) {
        var kw = String(brief.keywords[0]).trim();
        var inTitle = String(draft.title || '').replace(/ /g, '').indexOf(kw.replace(/ /g, '')) >= 0;
        out.push(chk('title_keyword', '제목에 메인 키워드', inTitle, inTitle ? '포함' : '없음', "'" + kw + "' 포함"));
      }
    } else if (channel === 'linkedin') {
      var nl = charsWithSpace(content);
      out.push(chk('length', '분량(공백 포함)', inRange(nl, L.min, L.max), n0(nl) + '자', n0(L.min) + '~' + n0(L.max) + '자 (최대 ' + n0(L.hardMax) + '자)'));
      var hook = charsWithSpace(firstLines(content, 2));
      out.push(chk('hook_length', '첫 2줄 길이', hook > 0 && hook <= L.maxHook, hook + '자', L.maxHook + '자 이하'));
      out.push(chk('hashtags', '해시태그 수', inRange(tags.length, L.minTags, L.maxTags), tags.length + '개', L.minTags + '~' + L.maxTags + '개'));
      var hasUrl = /https?:\/\//i.test(content);
      out.push(chk('no_link', '본문 외부 링크 없음', !hasUrl, hasUrl ? '링크 있음' : '없음', '링크는 첫 댓글로'));
    } else if (channel === 'instagram') {
      var slides = countMatches(/^###\s*슬라이드\s*\d+/gm, content);
      out.push(chk('slides', '캐러셀 슬라이드 수', inRange(slides, L.minSlides, L.maxSlides), slides + '장', L.minSlides + '~' + L.maxSlides + '장'));
      var caption = section(content, '캡션');
      var c = charsWithSpace(caption);
      out.push(chk('caption_length', '캡션 길이', c > 0 && c <= L.maxCaption, n0(c) + '자', '1~' + n0(L.maxCaption) + '자'));
      var ch = charsWithSpace(firstLines(caption, 1));
      out.push(chk('hook_length', '캡션 첫 줄 길이', ch > 0 && ch <= L.maxHook, ch + '자', L.maxHook + '자 이하'));
      out.push(chk('hashtags', '해시태그 수', inRange(tags.length, L.minTags, L.maxTags), tags.length + '개', L.minTags + '~' + L.maxTags + '개'));
    }
    return out.concat(profileChecks(channel, draft, profile, opts));
  }
  ws.measure = measure;

  // ------------------------------------------------------------------ company profile (cached for the editor's brand checks)
  var profileCache = null;
  /** Resolves with the saved Profile, or null when it cannot be loaded (checks are then skipped). */
  ws.getProfile = function () {
    if (ws.mode !== 'live') return Promise.resolve(null);
    if (!profileCache) {
      profileCache = api('GET', '/api/profile').then(function (r) { return ws.unwrap(r, 'profile') || null; }, function () { profileCache = null; return null; });
    }
    return profileCache;
  };
  ws.setProfile = function (p) { profileCache = Promise.resolve(p || null); };

  /** Options for item jobs: in mock mode reuse the playback speed chosen in the studio brief form. */
  ws.jobOptions = function () {
    var opts = {};
    if (ws.health && ws.health.mode === 'mock') {
      var sp = parseFloat(U.storageGet('insia.mockSpeed'));
      if (!isNaN(sp) && (sp === 0 || (sp >= 0.1 && sp <= 100))) opts.speed = sp;
    }
    return opts;
  };
  ws.text = { charsWithSpace: charsWithSpace, charsNoSpace: charsNoSpace, firstLines: firstLines, section: section };

  /** "#a #b, c" → ["#a", "#b", "#c"] (duplicates dropped, order kept). */
  ws.parseTags = function (text) {
    var seen = {};
    return String(text || '').split(/[\s,，]+/).map(function (t) { return t.trim(); }).filter(Boolean).map(function (t) {
      return t.charAt(0) === '#' ? t : '#' + t;
    }).filter(function (t) { if (t === '#' || seen[t]) return false; seen[t] = 1; return true; });
  };

  // ------------------------------------------------------------------ line diff
  /**
   * Line-level diff (LCS after trimming the common prefix/suffix).
   * Returns [{op: 'eq'|'del'|'add', text}]. Very large middles fall back to
   * "all removed, all added" so the page never stalls.
   */
  function lineDiff(a, b) {
    var A = String(a || '').replace(/\r\n?/g, '\n').split('\n');
    var B = String(b || '').replace(/\r\n?/g, '\n').split('\n');
    var pre = 0;
    while (pre < A.length && pre < B.length && A[pre] === B[pre]) pre++;
    var suf = 0;
    while (suf < A.length - pre && suf < B.length - pre && A[A.length - 1 - suf] === B[B.length - 1 - suf]) suf++;
    var a1 = A.slice(pre, A.length - suf), b1 = B.slice(pre, B.length - suf);
    var ops = [];
    var i;
    for (i = 0; i < pre; i++) ops.push({ op: 'eq', text: A[i] });
    var n = a1.length, m = b1.length;
    if (n * m > 4000000) {
      a1.forEach(function (t) { ops.push({ op: 'del', text: t }); });
      b1.forEach(function (t) { ops.push({ op: 'add', text: t }); });
    } else {
      var w = m + 1;
      var dp = new Uint32Array((n + 1) * w);
      for (var x = n - 1; x >= 0; x--) {
        for (var y = m - 1; y >= 0; y--) {
          dp[x * w + y] = a1[x] === b1[y] ? dp[(x + 1) * w + y + 1] + 1 : Math.max(dp[(x + 1) * w + y], dp[x * w + y + 1]);
        }
      }
      var p = 0, q = 0;
      while (p < n && q < m) {
        if (a1[p] === b1[q]) { ops.push({ op: 'eq', text: a1[p] }); p++; q++; }
        else if (dp[(p + 1) * w + q] >= dp[p * w + q + 1]) { ops.push({ op: 'del', text: a1[p++] }); }
        else { ops.push({ op: 'add', text: b1[q++] }); }
      }
      while (p < n) ops.push({ op: 'del', text: a1[p++] });
      while (q < m) ops.push({ op: 'add', text: b1[q++] });
    }
    for (i = A.length - suf; i < A.length; i++) ops.push({ op: 'eq', text: A[i] });
    return ops;
  }
  ws.lineDiff = lineDiff;

  // ------------------------------------------------------------------ jobs (item review / revise / slot generate)
  var jobListeners = [];
  ws.onJobEnd = function (fn) { jobListeners.push(fn); };

  function findItemId(runId) {
    var evs = I.player.events || [];
    for (var i = evs.length - 1; i >= 0; i--) {
      var d = evs[i] && evs[i].data;
      if (d && d.item_id) return d.item_id;
    }
    return '';
  }

  /**
   * Stream a job's events into the studio (the capybaras animate) and report back.
   * opts: {label, itemId, slotId, goStudio}. Listeners registered with onJobEnd get
   * {runId, itemId, slotId, ok, state, event}.
   */
  ws.watchJob = function (runId, opts) {
    opts = opts || {};
    var banner = document.getElementById('jobBanner');
    var stop = el('button', {
      type: 'button', class: 'btn btn--small btn--ghost', 'data-key': 'job-stop', text: '멈추기', onclick: function () {
        stop.disabled = true;
        stop.textContent = '멈추는 중…';
        ws.post('/api/runs/' + encodeURIComponent(runId) + '/cancel', {}).catch(function (ex) {
          stop.disabled = false;
          stop.textContent = '멈추기';
          if (!ex.auth) toast('멈추지 못했어요: ' + ex.message, 'error');
        });
      }
    });
    showBanner(banner, 'running', [
      el('span', { class: 'jb-dot', 'aria-hidden': 'true' }),
      el('b', { text: opts.label || '작업' }), ' 진행 중이에요. 에이전트가 일하는 모습을 무대에서 볼 수 있어요.',
      opts.itemId ? el('a', { class: 'btn btn--small', href: '#/library/' + encodeURIComponent(opts.itemId), text: '보관함으로 돌아가기' }) : null,
      stop
    ]);
    I.studio.watchRun(runId, {
      job: true,
      title: opts.label || ('작업 ' + runId),
      onEnd: function (state, ev) {
        if (ev && ev.type === 'replaced') {
          // the stage switched to another run; the job keeps running on the server
          if (banner) banner.hidden = true;
          toast((opts.label || '작업') + '은 서버에서 계속 진행돼요. 끝나면 보관함에서 결과를 볼 수 있어요.', 'info');
          jobListeners.forEach(function (fn) {
            try { fn({ runId: runId, itemId: opts.itemId || '', slotId: opts.slotId || '', ok: false, replaced: true, state: null, event: ev }); } catch (e) { if (window.console) console.warn(e); }
          });
          return;
        }
        var ok = ev && ev.type === 'run.completed';
        var itemId = opts.itemId || findItemId(runId);
        var chans = state ? state.channelOrder : [];
        var ch = chans && chans.length === 1 ? state.channels[chans[0]] : null;
        var result = ch && typeof ch.score === 'number' ? ' · ' + ch.score + '점 ' + (ch.passed ? '통과' : '미통과') : '';
        var why = ev && ev.data && ev.data.error ? ev.data.error : '';
        function finish(kept) {
          showBanner(banner, ok ? 'done' : 'failed', [
            el('b', { text: (opts.label || '작업') + (ok ? ' 완료' : ' 실패') }),
            ok ? result + ' · 결과는 보관함에 저장됐어요.' : ' · ' + (why || '원인을 알 수 없어요'),
            kept ? el('span', { class: 'jb-kept', 'data-key': 'job-kept', text: ' ' + kept }) : null,
            itemId ? el('a', { class: 'btn btn--small', href: '#/library/' + encodeURIComponent(itemId), text: '보관함에서 보기' }) : null,
            el('button', { type: 'button', class: 'btn btn--small btn--ghost', text: '닫기', onclick: function () { banner.hidden = true; } })
          ]);
          toast((opts.label || '작업') + (ok ? ' 완료' + result : ' 실패') + (kept ? ' · 에이전트 결과는 기록에만 남았어요' : ''),
            ok ? (kept ? 'info' : 'success') : 'error');
          jobListeners.forEach(function (fn) {
            try { fn({ runId: runId, itemId: itemId, slotId: opts.slotId || '', ok: ok, state: state, event: ev, kept: kept }); } catch (e) { if (window.console) console.warn(e); }
          });
        }
        if (ok) ws.confirmKept(ev.data, itemId).then(finish);
        else finish('');
      }
    });
    if (opts.goStudio) ws.go('studio');
  };
  function showBanner(banner, kind, children) {
    if (!banner) return;
    banner.dataset.kind = kind;
    banner.textContent = '';
    U.appendChildren(banner, children);
    banner.hidden = false;
  }

  /** Called by the API client on 401 (token mode). shell.js renders the login view. */
  ws.requireLogin = function (message) {
    ws.authMessage = message || '';
    if (ws.mode !== 'auth') {
      ws.mode = 'auth';
      I.studio.setServer({ authRequired: true });
      if (ws.syncAuthUi) ws.syncAuthUi();
    }
    if (ws.route.view !== 'login') ws.go('login', '', { back: ws.route });
  };
})();
