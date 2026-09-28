/* INSIA 에이전트 스튜디오 — 보관함 (content library).
 *
 * List: filter by status (chips with counts) and channel; item cards.
 * Detail: rendered content, edit mode with live counters (channels.py units),
 * review panel, version history with a line diff, and the approval workflow
 * (재검수 · 수정 요청 · 승인/그래도 승인 · 게시 예정 · 게시 완료 · 보관 · 내보내기).
 *
 * Live mode talks to /api/items*. Demo/artifact mode builds read-only items from
 * the embedded trace (channel.completed + draft/review events).
 */
(function () {
  'use strict';

  var I = window.INSIA;
  var U = I.util;
  var ws = I.ws;
  var ui = ws.ui;
  var el = U.el;

  var EXPORTS = {
    bizplan: [['docx', 'Word 문서', '.docx · 표·개조식 서식 포함'], ['md', '마크다운', '.md'], ['txt', '텍스트', '.txt'], ['zip', '묶음 파일', '.zip · 형식별 파일 모음']],
    naver_blog: [['html', '스마트에디터용 HTML', '.html · 붙여넣기용'], ['md', '마크다운', '.md'], ['txt', '텍스트', '.txt'], ['docx', 'Word 문서', '.docx'], ['zip', '묶음 파일', '.zip · 형식별 파일 모음']],
    linkedin: [['txt', '붙여넣기용 텍스트', '.txt · 해시태그 포함'], ['md', '마크다운', '.md'], ['docx', 'Word 문서', '.docx'], ['zip', '묶음 파일', '.zip · 형식별 파일 모음']],
    instagram: [['zip', '카드 이미지 + 캡션', '.zip · 1080×1350'], ['txt', '캡션 텍스트', '.txt'], ['md', '마크다운', '.md'], ['docx', 'Word 문서', '.docx']]
  };
  var DRAFT_KEY = 'insia.unsaved.';

  var S = {
    container: null,
    items: null,          // list (live: from API; demo: from trace)
    listError: null,
    listLoading: false,
    status: 'all',        // all (= not archived) | <status>
    channel: '',
    demo: null,           // {items, details, sources}
    detailId: '',
    detail: null,         // ContentItemDetail
    detailError: null,
    viewVersion: null,    // version number being viewed (null = latest)
    editing: false,
    panel: '',            // approve | revise | schedule | publish | archive
    panelError: '',
    busy: false,
    saveResult: null,     // {version, checks}
    diff: null,           // {from, to}
    jobs: {},             // itemId -> {runId, label}
    profile: null         // company profile for the editor's brand checks (live only)
  };

  // ------------------------------------------------------------------ data
  function demoData() {
    if (S.demo) return S.demo;
    var trace = I.studio.bootTrace();
    var out = { items: [], details: {}, sources: {} };
    if (!trace || !Array.isArray(trace.events)) return (S.demo = out);
    var evs = trace.events.filter(function (e) { return e && e.type; }).slice();
    evs.sort(function (a, b) { return (Number(a.t) || 0) - (Number(b.t) || 0) || (a.seq || 0) - (b.seq || 0); });
    var s = I.initialState();
    evs.forEach(function (e) { try { s = I.applyEvent(s, e); } catch (err) { /* skip bad event */ } });
    s.research.sources.forEach(function (x) { if (x.id) out.sources[x.id] = x; });
    var when = (trace.meta && trace.meta.recorded_at) || '';
    var brief = (s.run && s.run.brief) || (trace.meta && trace.meta.brief) || null;
    s.channelOrder.forEach(function (c) {
      var ch = s.channels[c];
      if (!ch || !ch.final) return;
      var id = 'demo-' + c;
      var versions = ch.drafts.map(function (d, i) {
        var isFinal = (d.round || 0) === ch.final.round;
        var review = ch.reviews.filter(function (r) { return (r.round || 0) === (d.round || 0); })[0] || null;
        return {
          id: id + '-v' + (i + 1), item_id: id, version: i + 1, source: 'agent', instructions: '', created_at: when,
          draft: {
            channel: c, round: d.round || 0, title: isFinal ? ch.final.title : d.title, content: isFinal ? ch.final.content : '',
            excerpt: d.excerpt || '', hashtags: isFinal ? ch.final.hashtags : (d.hashtags || []), change_log: d.change_log || []
          },
          review: review,
          demoPartial: !isFinal
        };
      });
      var finalVersion = versions.filter(function (v) { return !v.demoPartial; })[0];
      var item = {
        id: id, run_id: (s.run && s.run.id) || '', channel: c, title: ch.final.title, status: 'draft',
        version: finalVersion ? finalVersion.version : versions.length, score: ch.final.score, passed: ch.final.passed,
        scheduled_at: '', published_at: '', published_url: '', note: '', created_at: when, updated_at: when
      };
      out.items.push(item);
      out.details[id] = { item: item, versions: versions, brief: brief, demoFinalVersion: item.version };
    });
    return (S.demo = out);
  }

  function isDemo() { return ws.mode !== 'live'; }

  function loadList(force) {
    if (isDemo()) { S.items = demoData().items; return Promise.resolve(S.items); }
    if (S.items && !force) return Promise.resolve(S.items);
    S.listLoading = true;
    S.listError = null;
    return ws.get('/api/items' + ws.q({ channel: S.channel })).then(function (r) {
      S.items = ws.listOf(r, 'items');
      S.listLoading = false;
      updateNavCount();
      return S.items;
    }, function (err) {
      S.listLoading = false;
      S.listError = err;
      throw err;
    });
  }

  function loadDetail(id) {
    if (isDemo()) {
      var d = demoData().details[id];
      if (!d) return Promise.reject(Object.assign(new Error('데모 기록에 없는 콘텐츠예요.'), { status: 404 }));
      return Promise.resolve(d);
    }
    return ws.get('/api/items/' + encodeURIComponent(id));
  }

  function updateNavCount() {
    var badge = document.getElementById('navCountLibrary');
    if (!badge) return;
    if (isDemo() || !S.items || S.channel) { if (isDemo() || !S.items) badge.hidden = true; return; }
    var n = S.items.filter(function (i) { return i.status === 'draft' || i.status === 'needs_changes'; }).length;
    badge.textContent = '';
    if (n) U.appendChildren(badge, [el('span', { 'aria-hidden': 'true', text: String(n) }), el('span', { class: 'sr-only', text: ', 검토 대기 ' + n + '건' })]);
    badge.hidden = !n;
  }

  // ------------------------------------------------------------------ view API
  function show(container, param, ctx) {
    S.container = container;
    if (param) {
      if (param !== S.detailId) resetDetail(param);
      renderDetail(ctx && ctx.focus);
      refreshDetail(ctx && ctx.focus);
    } else {
      S.detailId = '';
      renderList(ctx && ctx.focus);
      loadList(!isDemo()).then(function () { if (!S.detailId && isShown()) renderList(false); }, function () { if (!S.detailId && isShown()) renderList(false); });
    }
  }
  function isShown() { return S.container && !S.container.hidden && ws.route.view === 'library'; }
  function resetDetail(id) {
    S.detailId = id;
    S.detail = null;
    S.detailError = null;
    S.viewVersion = null;
    S.editing = false;
    S.panel = '';
    S.panelError = '';
    S.saveResult = null;
    S.diff = null;
    S.busy = false;
  }
  function reset() { S.items = null; S.demo = null; S.detail = null; S.detailId = ''; }
  I.views.library = { show: show, reset: reset, prefetch: function () { loadList(true).catch(function () { /* shown later */ }); } };

  ws.onJobEnd(function (job) {
    Object.keys(S.jobs).forEach(function (k) { if (S.jobs[k].runId === job.runId) delete S.jobs[k]; });
    S.items = null;
    if (!isDemo()) loadList(true).catch(function () { /* ignore */ });
    if (S.detailId && job.itemId === S.detailId && isShown()) refreshDetail(false);
  });

  // ------------------------------------------------------------------ list
  function renderList(focus) {
    var items = S.items || [];
    var counts = { all: 0 };
    ws.STATUS_ORDER.forEach(function (k) { counts[k] = 0; });
    items.forEach(function (it) {
      if (!S.channel || it.channel === S.channel) {
        counts[it.status] = (counts[it.status] || 0) + 1;
        if (it.status !== 'archived') counts.all++;
      }
    });
    var shown = items.filter(function (it) {
      if (S.channel && it.channel !== S.channel) return false;
      return S.status === 'all' ? it.status !== 'archived' : it.status === S.status;
    });

    var chSelect = el('select', { id: 'libChannel', class: 'select' }, [el('option', { value: '', text: '모든 채널' })].concat(U.CHANNEL_IDS.map(function (c) {
      return el('option', { value: c, text: U.chName(c), selected: S.channel === c });
    })));
    chSelect.addEventListener('change', function () {
      S.channel = chSelect.value;
      if (isDemo()) renderList(false);
      else { S.items = null; renderList(false); loadList(true).then(function () { renderList(false); }, function () { renderList(false); }); }
      var sel = document.getElementById('libChannel');
      if (sel) sel.focus();
    });
    var tools = [
      el('label', { class: 'inline-field', for: 'libChannel' }, [el('span', { text: '채널' }), chSelect]),
      !isDemo() ? el('button', { type: 'button', class: 'btn', 'data-key': 'refresh', text: '새로고침', onclick: function () { loadList(true).then(function () { renderList(false); }, function () { renderList(false); }); } }) : null
    ];

    var chips = el('div', { class: 'chip-filter', role: 'group', 'aria-label': '상태로 거르기' }, ['all'].concat(ws.STATUS_ORDER).map(function (k) {
      var label = k === 'all' ? '전체' : ws.STATUS[k].label;
      return el('button', {
        type: 'button', class: 'fchip', 'data-status': k, 'data-key': 'fchip-' + k, 'aria-pressed': String(S.status === k),
        title: k === 'all' ? '보관한 항목은 빼고 보여줘요' : ws.STATUS[k].hint,
        onclick: function () { S.status = k; renderList(false); var b = S.container.querySelector('.fchip[data-status="' + k + '"]'); if (b) b.focus(); }
      }, [label, el('span', { class: 'fchip-n', text: String(counts[k] || 0) })]);
    }));

    var body;
    if (S.listError && !S.items) {
      body = ui.errorState(S.listError, function () { loadList(true).then(function () { renderList(false); }, function () { renderList(false); }); });
    } else if (!S.items) {
      body = ui.loading('보관함을 불러오는 중이에요…');
    } else if (!items.length) {
      body = el('div', { class: 'empty-state' }, [
        el('p', { class: 'empty-title', text: '아직 보관함이 비어 있어요' }),
        el('p', { class: 'empty', text: '스튜디오에서 브리프로 실행하거나 캘린더에서 초안을 만들면, 채널별 결과물이 여기에 쌓여요.' }),
        el('a', { class: 'btn btn--primary', href: '#/studio', text: '스튜디오로 가기' })
      ]);
    } else if (!shown.length) {
      body = el('p', { class: 'empty empty-state', text: '이 조건에 맞는 콘텐츠가 없어요. 다른 상태나 채널을 골라 보세요.' });
    } else {
      body = el('ul', { class: 'item-grid', 'aria-label': '콘텐츠 ' + shown.length + '개' }, shown.map(itemCard));
    }

    var keep = captureFocus();
    ws.mount(S.container, [
      ui.viewHead('library', '보관함', '에이전트가 만든 초안을 검토하고 승인·게시 상태를 기록해요. 사람이 승인하기 전에는 어디에도 게시되지 않아요.', tools),
      isDemo() ? demoNote() : null,
      chips,
      body
    ]);
    if (focus) ui.focusHeading(document.getElementById('libraryTitle'));
    else restoreFocus(keep);
  }

  function demoNote() {
    return ui.notice('info', [
      el('b', { text: '데모 기록 · 읽기 전용' }),
      ' 녹화된 샘플 실행의 결과물이에요. 편집·재검수·승인·내보내기는 내 컴퓨터에서 ',
      el('code', { text: ws.LOCAL_CMD }), '로 앱을 켜면 쓸 수 있어요.'
    ]);
  }

  function itemCard(it) {
    var when = it.status === 'scheduled' && it.scheduled_at ? ws.date.dateTime(it.scheduled_at) + ' 게시 예정'
      : it.status === 'published' && it.published_at ? ws.date.dateTime(it.published_at) + ' 게시'
        : it.updated_at ? ws.date.dateTime(it.updated_at) + ' 수정' : '';
    return el('li', null, el('a', {
      class: 'item-card', href: '#/library/' + encodeURIComponent(it.id), style: '--ch:' + ws.channelColor(it.channel), 'data-status': it.status
    }, [
      el('span', { class: 'ic-top' }, [ui.channelIcon(it.channel, 'ic-icon'), el('span', { class: 'ic-channel', text: U.chName(it.channel) }), ui.statusPill(it.status)]),
      el('span', { class: 'ic-title', text: it.title || '(제목 없음)' }),
      el('span', { class: 'ic-meta' }, [
        ui.scoreBadge(it.score, it.passed),
        el('span', { class: 'ic-ver', text: 'v' + (it.version || 1) }),
        when ? el('span', { class: 'ic-when', text: when }) : null
      ])
    ]));
  }

  // ------------------------------------------------------------------ detail
  function refreshDetail(focus) {
    var id = S.detailId;
    // the profile feeds the brand checks of the editor and of the unreviewed-version preview
    return Promise.all([loadDetail(id), isDemo() ? null : ws.getProfile()]).then(function (res) {
      var d = res[0];
      if (res[1]) S.profile = res[1];
      if (S.detailId !== id) return;
      S.detail = d;
      S.detailError = null;
      if (isShown()) renderDetail(focus && !S.detail);
    }, function (err) {
      if (S.detailId !== id) return;
      S.detailError = err;
      if (isShown()) renderDetail(false);
    });
  }

  function versions() { return (S.detail && S.detail.versions) || []; }
  function latestVersion() { return U.last(versions()); }
  function viewedVersion() {
    var vs = versions();
    if (S.viewVersion !== null) {
      var v = vs.filter(function (x) { return x.version === S.viewVersion; })[0];
      if (v) return v;
    }
    if (S.detail && S.detail.demoFinalVersion) return vs.filter(function (x) { return x.version === S.detail.demoFinalVersion; })[0] || U.last(vs);
    return U.last(vs);
  }

  function renderDetail(focus) {
    var back = el('a', { class: 'back-link', href: '#/library' }, [el('span', { 'aria-hidden': 'true', text: '← ' }), '보관함']);
    if (S.detailError && !S.detail) {
      ws.mount(S.container, [back, el('h2', { class: 'view-title', id: 'libraryTitle', tabindex: '-1', text: '콘텐츠를 열지 못했어요' }), ui.errorState(S.detailError, function () { refreshDetail(false); })]);
      return;
    }
    if (!S.detail) {
      ws.mount(S.container, [back, el('h2', { class: 'view-title', id: 'libraryTitle', tabindex: '-1', text: '콘텐츠 불러오는 중' }), ui.loading()]);
      if (focus) ui.focusHeading(document.getElementById('libraryTitle'));
      return;
    }
    var keep = captureFocus();
    var item = S.detail.item;
    var v = viewedVersion();
    var latest = latestVersion();
    // the "saved v4" notice is stale once a job or another edit made a newer version
    if (S.saveResult && latest && latest.version !== S.saveResult.version) S.saveResult = null;
    var head = el('header', { class: 'detail-head', style: '--ch:' + ws.channelColor(item.channel) }, [
      ui.channelIcon(item.channel, 'detail-icon'),
      el('div', { class: 'detail-heading' }, [
        el('p', { class: 'eyebrow', text: [U.chName(item.channel), 'v' + (item.version || (latest && latest.version) || 1), item.updated_at ? ws.date.dateTime(item.updated_at) + ' 수정' : ''].filter(Boolean).join(' · ') }),
        el('h2', { class: 'view-title detail-title', id: 'libraryTitle', tabindex: '-1', text: item.title || (v && v.draft.title) || '(제목 없음)' })
      ]),
      el('div', { class: 'detail-state' }, [ui.statusPill(item.status), ui.scoreBadge(item.score, item.passed)])
    ]);

    var main = el('div', { class: 'detail-main' }, [
      jobNotice(item),
      saveResultNotice(),
      v && latest && v.version !== latest.version ? ui.notice('info', [
        el('b', { text: '이전 버전(v' + v.version + ')을 보고 있어요' }), ' · 최신 버전은 v' + latest.version + '예요.',
        el('button', { type: 'button', class: 'btn btn--small notice-action', text: '최신 버전 보기', onclick: function () { S.viewVersion = null; renderDetail(false); } })
      ]) : null,
      // actions sit above the content so they come first on a phone and in reading order
      isDemo() ? demoNote() : actionsCard(item, latest),
      S.editing ? editorCard(item, latest) : contentCard(item, v)
    ]);
    var side = el('div', { class: 'detail-side' }, [
      isDemo() ? null : exportCard(item),
      reviewCard(item, v),
      versionsCard(item, v)
    ]);
    ws.mount(S.container, [back, head, el('div', { class: 'detail-layout' }, [main, side])]);
    if (focus) ui.focusHeading(document.getElementById('libraryTitle'));
    else restoreFocus(keep);
  }

  // keep keyboard focus stable across re-renders (actions re-render the whole detail)
  function captureFocus() {
    var a = document.activeElement;
    if (!a || !S.container.contains(a)) return null;
    return a.id ? { id: a.id } : (a.dataset && a.dataset.key ? { key: a.dataset.key } : null);
  }
  function restoreFocus(k) {
    if (!k) return;
    var n = k.id ? document.getElementById(k.id) : S.container.querySelector('[data-key="' + k.key + '"]');
    if (n) { try { n.focus({ preventScroll: true }); } catch (e) { n.focus(); } }
  }

  function jobNotice(item) {
    var job = S.jobs[item.id];
    if (!job) return null;
    return ui.notice('info', [
      el('span', { class: 'jb-dot', 'aria-hidden': 'true' }),
      el('b', { text: job.label + ' 중이에요' }), ' · 끝나면 이 화면이 새 결과로 바뀌어요.',
      el('a', { class: 'btn btn--small notice-action', href: '#/studio', text: '스튜디오에서 보기' })
    ], 'notice--live');
  }

  function saveResultNotice() {
    var r = S.saveResult;
    if (!r) return null;
    var checks = r.checks || [];
    var ok = checks.filter(function (c) { return c.passed; }).length;
    return ui.notice(checks.length && ok < checks.length ? 'warn' : 'success', [
      el('div', { class: 'notice-row' }, [
        el('b', { text: '새 버전(v' + r.version + ')을 저장했어요' }),
        checks.length ? ' · 서버 형식 검사 ' + ok + '/' + checks.length + ' 통과' : ' · 서버가 형식 검사 결과를 보내지 않았어요',
        el('button', { type: 'button', class: 'btn btn--small btn--ghost notice-action', text: '닫기', onclick: function () { S.saveResult = null; renderDetail(false); } })
      ]),
      checks.length ? U.formatChecksBlock(checks, '형식 검사 (저장 직후 서버 계산)') : null,
      el('p', { class: 'notice-foot', text: '사람이 고친 버전은 아직 검수 점수가 없어요. 승인 전에 재검수하면 루브릭 점수와 사실 확인을 다시 받아요.' })
    ], 'save-result');
  }

  function composeCopy(d) {
    if (!d) return '';
    var text = (d.title ? d.title + '\n\n' : '') + String(d.content || '').trim();
    var tags = (d.hashtags || []);
    if (tags.length && String(d.content || '').indexOf(tags[0]) < 0) text += '\n\n' + tags.join(' ');
    return text;
  }

  function contentCard(item, v) {
    if (!v) return el('section', { class: 'card' }, el('p', { class: 'empty', text: '저장된 버전이 없어요.' }));
    var d = v.draft || {};
    var status = el('span', { class: 'copy-status', role: 'status' });
    var fallback = el('textarea', { class: 'copy-fallback', rows: '8', readonly: true, 'aria-label': '복사할 텍스트', hidden: true });
    var canEdit = !isDemo() && item.status !== 'archived' && item.status !== 'published' && latestVersion() && v.version === latestVersion().version;
    var toolbar = el('div', { class: 'card-toolbar' }, [
      canEdit ? el('button', { type: 'button', class: 'btn', 'data-key': 'edit', text: '편집', onclick: function () { S.editing = true; S.panel = ''; renderDetail(false); var t = document.getElementById('edTitle'); if (t) t.focus(); } }) : null,
      d.content ? el('button', { type: 'button', class: 'btn', 'data-key': 'copy', text: '전체 복사', onclick: function () { copy(); } }) : null,
      status
    ]);
    function copy() { U.copyText(composeCopy(d), status, fallback); }
    var body;
    if (v.demoPartial || !d.content) {
      body = [
        el('p', { class: 'pane-note', text: v.demoPartial ? '데모 기록에는 이 라운드의 발췌만 들어 있어요. 전문은 최종 버전에서 볼 수 있어요.' : '본문이 비어 있어요.' }),
        d.excerpt ? el('div', { class: 'md' }, el('blockquote', { text: d.excerpt })) : null
      ];
    } else {
      var md = el('div', { class: 'md' });
      md.innerHTML = U.renderMarkdown(d.content, { sources: isDemo() ? demoData().sources : {} });
      body = [
        d.title && !/^\s*#\s+/.test(d.content) ? el('h3', { class: 'out-title', text: d.title }) : null,
        md,
        d.hashtags && d.hashtags.length ? el('div', null, [
          el('h4', { class: 'sub-title', text: item.channel === 'naver_blog' ? '태그' : '해시태그' }),
          el('div', { class: 'hashtags' }, d.hashtags.map(function (h) { return el('span', { text: h }); }))
        ]) : null
      ];
    }
    var pub = item.status === 'published' && item.published_url && /^https?:\/\//i.test(item.published_url)
      ? el('p', { class: 'pub-link' }, ['게시한 주소: ', el('a', { href: item.published_url, target: '_blank', rel: 'noopener noreferrer', text: item.published_url })]) : null;
    return el('section', { class: 'card content-card', 'aria-label': '본문' }, [toolbar, fallback, pub, body]);
  }

  // ------------------------------------------------------------------ editor
  function unsavedKey(item, latest) { return DRAFT_KEY + item.id + '.v' + (latest ? latest.version : 0); }
  function editorCard(item, latest) {
    var d = (latest && latest.draft) || { title: '', content: '', hashtags: [] };
    var saved = null;
    try { saved = JSON.parse(U.storageGet(unsavedKey(item, latest)) || 'null'); } catch (e) { saved = null; }
    var start = saved || { title: d.title || '', content: d.content || '', tags: (d.hashtags || []).join(' ') };
    var ch = item.channel;
    var withTags = ch !== 'bizplan';
    var tagWord = ch === 'naver_blog' ? '태그' : '해시태그';

    var title = el('input', { id: 'edTitle', type: 'text', value: start.title, autocomplete: 'off', 'aria-describedby': 'edTitleCount' });
    var titleCount = el('span', { class: 'field-count', id: 'edTitleCount' });
    var contentCount = el('span', { class: 'field-count', id: 'edContentCount' });
    var content = el('textarea', { id: 'edContent', rows: '22', spellcheck: 'false', 'aria-describedby': 'edContentCount edMeters' });
    content.value = start.content;
    var tags = withTags ? el('input', { id: 'edTags', type: 'text', value: start.tags, autocomplete: 'off', 'aria-describedby': 'edTagsHint' }) : null;
    var meters = el('ul', { class: 'meters', id: 'edMeters', 'aria-label': '실시간 형식 확인' });
    var err = el('p', { class: 'form-error', role: 'alert', hidden: true });
    var restored = saved ? ui.notice('warn', [
      el('b', { text: '저장하지 않은 편집을 되살렸어요' }), ' · 이 브라우저에 임시로 남아 있던 내용이에요.',
      el('button', { type: 'button', class: 'btn btn--small notice-action', text: '원래 내용으로', onclick: function () { U.storageSet(unsavedKey(item, latest), ''); renderDetail(false); } })
    ]) : null;

    var queued = false;
    function current() {
      return { title: title.value, content: content.value, hashtags: withTags ? ws.parseTags(tags.value) : [] };
    }
    function update() {
      queued = false;
      var cur = current();
      var checks = ws.measure(ch, cur, S.detail.brief, S.profile);
      meters.textContent = '';
      checks.forEach(function (c) {
        meters.appendChild(el('li', { 'data-ok': String(c.passed) }, [
          el('span', { class: 'm-mark', 'aria-hidden': 'true', text: c.passed ? '✓' : '!' }),
          el('span', { class: 'm-label', text: c.label }),
          el('span', { class: 'm-val', text: c.value }),
          el('span', { class: 'm-exp', text: '기준 ' + c.expected }),
          el('span', { class: 'sr-only', text: c.passed ? '기준 충족' : '기준 벗어남' })
        ]));
      });
      var tl = ws.text.charsWithSpace(cur.title);
      titleCount.textContent = ch === 'naver_blog' ? tl + ' / ' + ws.LIMITS.naver_blog.maxTitle + '자' : tl + '자';
      titleCount.dataset.ok = ch === 'naver_blog' ? String(tl > 0 && tl <= ws.LIMITS.naver_blog.maxTitle) : '';
      var dirty = cur.title !== (d.title || '') || cur.content !== (d.content || '') || (withTags && tags.value !== (d.hashtags || []).join(' '));
      if (dirty) U.storageSet(unsavedKey(item, latest), JSON.stringify({ title: cur.title, content: cur.content, tags: withTags ? tags.value : '' }));
      else U.storageSet(unsavedKey(item, latest), '');
      // the main length check stays next to the textarea label (the full list sits below it)
      var main = checks.filter(function (c) { return c.id === (ch === 'instagram' ? 'caption_length' : 'length'); })[0];
      contentCount.textContent = main ? (ch === 'instagram' ? '캡션 ' : '') + main.value + ' · 기준 ' + main.expected.replace(/ \(.*\)$/, '') + (ch === 'bizplan' || ch === 'naver_blog' ? ' (공백 제외)' : ' (공백 포함)') : '';
      contentCount.dataset.ok = main ? String(main.passed) : '';
      saveBtn.disabled = S.busy || !dirty;
    }
    function queue() { if (!queued) { queued = true; requestAnimationFrame(update); } }
    [title, content, tags].forEach(function (n) { if (n) n.addEventListener('input', queue); });

    var cancelZone = el('span', { class: 'confirm-zone' });
    var saveBtn = el('button', { type: 'submit', class: 'btn btn--primary', text: '저장 (새 버전)' });
    var cancelBtn = el('button', {
      type: 'button', class: 'btn', text: '편집 취소', onclick: function () {
        var cur = current();
        var dirty = cur.title !== (d.title || '') || cur.content !== (d.content || '') || (withTags && tags.value !== (d.hashtags || []).join(' '));
        if (!dirty) { stopEditing(); return; }
        cancelZone.textContent = '';
        U.appendChildren(cancelZone, [
          el('span', { text: '고친 내용을 버릴까요?' }),
          el('button', { type: 'button', class: 'btn btn--small btn--danger', text: '버리기', onclick: function () { U.storageSet(unsavedKey(item, latest), ''); stopEditing(); } }),
          el('button', { type: 'button', class: 'btn btn--small', text: '계속 편집', onclick: function () { cancelZone.textContent = ''; content.focus(); } })
        ]);
        cancelZone.querySelector('.btn--danger').focus();
      }
    });
    function stopEditing() { S.editing = false; renderDetail(false); var b = S.container.querySelector('[data-key="edit"]'); if (b) b.focus(); }

    var form = el('form', { class: 'card editor', novalidate: true, 'aria-label': '본문 편집' }, [
      restored,
      el('div', { class: 'field' }, [
        el('span', { class: 'field-label' }, [el('label', { for: 'edTitle', text: '제목' }), titleCount]),
        title
      ]),
      el('div', { class: 'field' }, [
        el('span', { class: 'field-label' }, [
          el('label', { for: 'edContent', text: '본문' }),
          contentCount
        ]),
        content,
        el('small', { class: 'field-hint', text: ch === 'instagram' ? '## 캐러셀 / ### 슬라이드 N — 제목 / ## 캡션 형식을 지켜 주세요' : ch === 'linkedin' ? '일반 텍스트 · 링크는 본문 대신 첫 댓글에' : '마크다운 (## 소제목, [이미지: 설명])' })
      ]),
      meters,
      withTags ? el('div', { class: 'field' }, [
        el('span', { class: 'field-label' }, [el('label', { for: 'edTags', text: tagWord }), el('small', { id: 'edTagsHint', text: '공백이나 쉼표로 구분 · #은 자동으로 붙어요' + (ch === 'linkedin' ? ' · 본문 마지막 줄에도 같은 해시태그를 적어 주세요' : '') })]),
        tags
      ]) : null,
      el('p', { class: 'edit-note', text: '글자수는 검수 코드(channels.py)와 같은 기준으로 셉니다: ' + (ch === 'bizplan' || ch === 'naver_blog' ? '본문은 공백 제외.' : ch === 'linkedin' ? '본문은 공백 포함.' : '캡션은 공백 포함.') + ' 저장하면 사람 수정 버전이 새로 생기고 상태가 초안으로 돌아가요' + (item.status === 'approved' || item.status === 'scheduled' ? ' (승인도 풀려요).' : '.') }),
      err,
      el('div', { class: 'form-foot' }, [cancelBtn, cancelZone, saveBtn])
    ]);
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var cur = current();
      if (!cur.title.trim()) { err.textContent = '제목을 입력해 주세요.'; err.hidden = false; title.focus(); return; }
      if (!cur.content.trim()) { err.textContent = '본문이 비어 있어요.'; err.hidden = false; content.focus(); return; }
      S.busy = true;
      saveBtn.disabled = true;
      saveBtn.textContent = '저장하는 중…';
      ws.put('/api/items/' + encodeURIComponent(item.id) + '/draft', cur).then(function (resp) {
        var ver = resp && (resp.version && typeof resp.version === 'object' ? resp.version : null);
        var checks = (resp && (resp.format_checks || resp.checks)) || (ver && ver.review && ver.review.format_checks) || [];
        S.saveResult = { version: (ver && ver.version) || ((latest ? latest.version : 0) + 1), checks: checks };
        U.storageSet(unsavedKey(item, latest), '');
        S.editing = false;
        S.viewVersion = null;
        S.busy = false;
        S.items = null;
        ui.toast('새 버전을 저장했어요.', 'success');
        return refreshDetail(false).then(function () {
          var n = S.container.querySelector('.save-result');
          if (n) { n.setAttribute('tabindex', '-1'); n.focus(); }
        });
      }, function (ex) {
        S.busy = false;
        saveBtn.disabled = false;
        saveBtn.textContent = '저장 (새 버전)';
        if (ex.auth) return;
        err.textContent = '저장하지 못했어요: ' + ex.message;
        err.hidden = false;
      });
    });
    // first paint of counters (after the nodes are in the DOM is not required)
    update();
    // brand checks (금지 표현 · 필수 문구 · 블라인드) need the saved profile; repaint once it arrives
    ws.getProfile().then(function (prof) { if (prof !== S.profile) { S.profile = prof; if (document.contains(meters)) queue(); } });
    return form;
  }

  // ------------------------------------------------------------------ actions
  function actionsCard(item, latest) {
    var st = item.status;
    var job = S.jobs[item.id];
    var passed = !!(latest && latest.review && latest.review.passed);
    var buttons = [];
    function btn(key, label, primary, onclick, disabled) {
      return el('button', {
        type: 'button', class: 'btn' + (primary ? ' btn--primary' : ''), 'data-key': 'act-' + key,
        'aria-expanded': ['approve', 'revise', 'schedule', 'publish', 'archive'].indexOf(key) >= 0 && (key !== 'approve' || !passed) ? String(S.panel === key) : null,
        disabled: S.busy || S.editing || disabled, text: label, onclick: onclick
      });
    }
    function toggle(key) { return function () { S.panel = S.panel === key ? '' : key; S.panelError = ''; renderDetail(false); focusPanel(); }; }

    if (st === 'draft' || st === 'needs_changes') {
      buttons.push(btn('approve', '승인', true, function () {
        if (passed) setStatus({ status: 'approved' }, '승인했어요. 이제 게시 날짜를 정하거나 게시 완료로 표시할 수 있어요.');
        else toggle('approve')();
      }));
    }
    if (st === 'approved') {
      buttons.push(btn('publish', '게시 완료 표시', true, toggle('publish')));
      buttons.push(btn('schedule', '게시 예정일 정하기', false, toggle('schedule')));
    }
    if (st === 'scheduled') {
      buttons.push(btn('publish', '게시 완료 표시', true, toggle('publish')));
      buttons.push(btn('schedule', '예정일 바꾸기', false, toggle('schedule')));
      buttons.push(btn('unschedule', '예정 취소', false, function () { setStatus({ status: 'approved' }, '게시 예정을 취소했어요. 승인 상태로 돌아갔어요.'); }));
    }
    if (st !== 'published' && st !== 'archived') {
      buttons.push(btn('review', '재검수', false, startReview, !!job));
      buttons.push(btn('revise', '수정 요청', false, toggle('revise'), !!job));
    }
    if (st !== 'archived') buttons.push(btn('archive', '보관', false, toggle('archive')));
    else buttons.push(btn('unarchive', '보관 해제', true, function () { setStatus({ status: 'draft' }, '보관을 해제했어요. 초안으로 돌아갔어요.'); }));

    var stateLine = {
      draft: '사람 검토를 기다리는 초안이에요.',
      needs_changes: '검수를 통과하지 못한 초안이에요. 수정 요청이나 편집 후 재검수해 보세요.',
      approved: '승인한 콘텐츠예요. 채널에 올린 뒤 게시 완료로 표시해 주세요.',
      scheduled: (item.scheduled_at ? ws.date.dateTime(item.scheduled_at) + '에 ' : '') + '게시할 예정이에요. 올린 뒤 게시 완료로 표시해 주세요.',
      published: '게시를 마쳤어요' + (item.published_at ? ' (' + ws.date.dateTime(item.published_at) + ')' : '') + '.',
      archived: '보관한 콘텐츠예요. 보관을 해제하면 초안으로 돌아가요. 내보내기는 계속 할 수 있어요.'
    }[st] || '';

    return el('section', { class: 'card actions-card', 'aria-labelledby': 'actTitle' }, [
      el('h3', { class: 'card-title', id: 'actTitle', text: '검토와 게시' }),
      el('p', { class: 'card-sub', text: S.editing ? '편집 중에는 승인·게시·재검수를 할 수 없어요. 먼저 저장하거나 편집을 취소해 주세요.' : stateLine }),
      item.note ? el('p', { class: 'item-note' }, [el('b', { text: '메모 ' }), item.note]) : null,
      buttons.length ? el('div', { class: 'action-row' }, buttons) : null,
      S.editing ? null : actionPanel(item, latest, passed)
    ]);
  }

  function focusPanel() {
    var p = S.container.querySelector('.action-panel');
    if (!p) return;
    var f = p.querySelector('textarea, input, .btn--primary, .btn--danger');
    if (f) f.focus();
  }

  function actionPanel(item, latest, passed) {
    var key = S.panel;
    if (!key) return null;
    var err = el('p', { class: 'form-error', role: 'alert', hidden: !S.panelError, text: S.panelError });
    var cancel = el('button', { type: 'button', class: 'btn', text: '취소', onclick: function () { var k = S.panel; S.panel = ''; S.panelError = ''; renderDetail(false); var b = S.container.querySelector('[data-key="act-' + k + '"]'); if (b) b.focus(); } });
    var nodes;
    if (key === 'approve') {
      var r = latest && latest.review;
      var crit = r && r.issues ? r.issues.filter(function (x) { return x.severity === 'critical'; }).length : 0;
      var why = !latest ? '저장된 버전이 없어요.'
        : !r ? '최신 버전(v' + latest.version + ')은 ' + (latest.source === 'human' ? '사람이 고친 버전이라 ' : '') + '아직 검수하지 않았어요.'
          : '최신 버전(v' + latest.version + ')이 검수를 통과하지 못했어요: ' + r.score + '점' + (crit ? ', 치명 이슈 ' + crit + '건' : '') + ' (통과 기준: 80점 이상, 치명 이슈 없음).';
      nodes = [
        el('p', { class: 'panel-lead' }, [el('b', { text: '바로 승인할 수 없어요. ' }), why]),
        el('p', { class: 'panel-hint', text: '재검수로 다시 채점받거나, 내용을 직접 확인했다면 그래도 승인할 수 있어요. 그래도 승인한 기록은 남아요.' }),
        err,
        el('div', { class: 'form-foot' }, [
          cancel,
          el('button', { type: 'button', class: 'btn', text: '재검수 먼저', disabled: S.busy || !!S.jobs[item.id], onclick: startReview }),
          el('button', {
            type: 'button', class: 'btn btn--danger', text: '그래도 승인', disabled: S.busy, onclick: function () {
              // the note is the record that approval skipped the review gate
              var note = '그래도 승인 · ' + ws.date.today() + ' · v' + (latest ? latest.version : '?') + ' ' + (r ? r.score + '점 미통과' : '검수 전');
              setStatus({ status: 'approved', force: true, note: note }, '검수를 통과하지 않은 채로 승인했어요. 이 기록은 메모에 남겨 뒀어요.');
            }
          })
        ])
      ];
    } else if (key === 'revise') {
      var ta = el('textarea', { id: 'reviseText', rows: '4', placeholder: '예: 첫 두 줄을 더 짧고 구체적으로, 가격 문단은 빼 주세요.' });
      nodes = [
        el('label', { class: 'field', for: 'reviseText' }, [el('span', { text: '총괄 에이전트에게 줄 수정 지시' }), ta]),
        el('p', { class: 'panel-hint', text: '최신 검수 의견과 함께 전달돼요. 새 버전이 만들어지면 검수 에이전트가 자동으로 다시 채점해요. 스튜디오로 이동해서 진행 과정을 보여 드려요.' }),
        err,
        el('div', { class: 'form-foot' }, [cancel, el('button', {
          type: 'button', class: 'btn btn--primary', text: '수정 요청 보내기', disabled: S.busy, onclick: function () {
            var text = ta.value.trim();
            if (!text) { S.panelError = '무엇을 고칠지 적어 주세요.'; err.textContent = S.panelError; err.hidden = false; ta.focus(); return; }
            startRevise(text);
          }
        })])
      ];
    } else if (key === 'schedule') {
      var def = (item.scheduled_at || '').slice(0, 10) || ws.date.add(ws.date.today(), 1);
      var inp = el('input', { id: 'schedDate', type: 'date', value: def, min: ws.date.today() });
      nodes = [
        el('label', { class: 'field', for: 'schedDate' }, [el('span', { text: '게시 예정일' }), inp]),
        el('p', { class: 'panel-hint', text: 'INSIA는 자동으로 게시하지 않아요. 날짜는 캘린더와 목록에서 알림용으로만 써요.' }),
        err,
        el('div', { class: 'form-foot' }, [cancel, el('button', {
          type: 'button', class: 'btn btn--primary', text: '예정일 저장', disabled: S.busy, onclick: function () {
            if (!/^\d{4}-\d{2}-\d{2}$/.test(inp.value)) { S.panelError = '날짜를 골라 주세요.'; err.textContent = S.panelError; err.hidden = false; inp.focus(); return; }
            setStatus({ status: 'scheduled', scheduled_at: inp.value }, ws.date.monthDay(inp.value) + ' 게시 예정으로 정했어요.');
          }
        })])
      ];
    } else if (key === 'publish') {
      var url = el('input', { id: 'pubUrl', type: 'url', inputmode: 'url', placeholder: 'https://', value: item.published_url || '', autocomplete: 'off' });
      nodes = [
        el('label', { class: 'field', for: 'pubUrl' }, [el('span', null, ['게시한 주소 ', el('small', { text: '선택 · 나중에 성과를 확인할 때 써요' })]), url]),
        err,
        el('div', { class: 'form-foot' }, [cancel, el('button', {
          type: 'button', class: 'btn btn--primary', text: '게시 완료로 표시', disabled: S.busy, onclick: function () {
            var u = url.value.trim();
            if (u && !/^https?:\/\/\S+$/i.test(u)) { S.panelError = 'http:// 또는 https://로 시작하는 주소를 넣어 주세요.'; err.textContent = S.panelError; err.hidden = false; url.focus(); return; }
            var body = { status: 'published' };
            if (u) body.published_url = u;
            setStatus(body, '게시 완료로 표시했어요.');
          }
        })])
      ];
    } else if (key === 'archive') {
      nodes = [
        el('p', { class: 'panel-lead', text: '보관하면 기본 목록에서 빠지고 ‘보관됨’에서만 보여요. 버전 기록과 내보내기는 그대로 남아요.' }),
        err,
        el('div', { class: 'form-foot' }, [cancel, el('button', { type: 'button', class: 'btn btn--danger', text: '보관하기', disabled: S.busy, onclick: function () { setStatus({ status: 'archived' }, '보관했어요.'); } })])
      ];
    }
    return el('div', { class: 'action-panel', 'data-panel': key }, nodes);
  }

  function setStatus(body, okMessage) {
    var id = S.detailId;
    S.busy = true;
    S.panelError = '';
    renderDetail(false);
    ws.post('/api/items/' + encodeURIComponent(id) + '/status', body).then(function (resp) {
      S.busy = false;
      S.panel = '';
      S.items = null;
      var it = ws.unwrap(resp, 'item');
      if (it && it.id && S.detail) S.detail.item = it;
      ui.toast(okMessage, 'success');
      return refreshDetail(false).then(function () {
        var b = S.container.querySelector('.actions-card .btn--primary') || S.container.querySelector('.actions-card .btn');
        if (b) b.focus();
      });
    }, function (ex) {
      S.busy = false;
      if (ex.auth) return;
      // the server refused approval (e.g. its own review check): offer the force path
      if (body.status === 'approved' && !body.force && (ex.status === 409 || ex.status === 400)) S.panel = 'approve';
      S.panelError = ex.message;
      renderDetail(false);
      focusPanel();
    });
  }

  function startReview() {
    var id = S.detailId;
    S.busy = true;
    renderDetail(false);
    ws.post('/api/items/' + encodeURIComponent(id) + '/review', { options: ws.jobOptions() }).then(function (resp) {
      S.busy = false;
      S.panel = '';
      var runId = resp && resp.run_id;
      if (!runId) throw new Error('서버가 작업 번호(run_id)를 보내지 않았어요.');
      S.jobs[id] = { runId: runId, label: '재검수' };
      ws.watchJob(runId, { label: '재검수', itemId: id, goStudio: false });
      renderDetail(false);
    }).catch(function (ex) {
      S.busy = false;
      if (ex.auth) return;
      S.panelError = '';
      renderDetail(false);
      ui.toast('재검수를 시작하지 못했어요: ' + ex.message, 'error');
    });
  }

  function startRevise(text) {
    var id = S.detailId;
    S.busy = true;
    ws.post('/api/items/' + encodeURIComponent(id) + '/revise', { instructions: text, options: ws.jobOptions() }).then(function (resp) {
      S.busy = false;
      S.panel = '';
      var runId = resp && resp.run_id;
      if (!runId) throw new Error('서버가 작업 번호(run_id)를 보내지 않았어요.');
      S.jobs[id] = { runId: runId, label: '수정 요청' };
      ws.watchJob(runId, { label: '수정 요청', itemId: id, goStudio: true });
    }).catch(function (ex) {
      S.busy = false;
      if (ex.auth) return;
      S.panelError = '수정 요청을 보내지 못했어요: ' + ex.message;
      renderDetail(false);
      focusPanel();
    });
  }

  /**
   * Export links. The server's detail lists the channel's formats (recommended first) with
   * availability and hints (python-docx missing, PNG → slides.html); the local table only adds
   * friendlier descriptions and is the fallback for an older server.
   */
  function exportCard(item) {
    var local = {};
    (EXPORTS[item.channel] || []).forEach(function (f) { local[f[0]] = f; });
    var base = '/api/items/' + encodeURIComponent(item.id) + '/export?format=';
    var list = (S.detail && Array.isArray(S.detail.exports) && S.detail.exports.length) ? S.detail.exports.map(function (e) {
      var f = local[e.format];
      return { format: e.format, label: f ? f[1] : (e.label || e.format), sub: f ? f[2] : '.' + e.format, available: e.available !== false, hint: e.hint || '', url: e.url || base + e.format };
    }) : (EXPORTS[item.channel] || [['md', '마크다운', '.md']]).map(function (f) {
      return { format: f[0], label: f[1], sub: f[2], available: true, hint: '', url: base + f[0] };
    });
    // same-origin API path only (the download attribute and the token cookie need it)
    list.forEach(function (x) { if (!/^\/api\/items\//.test(x.url)) x.url = base + x.format; });
    return el('section', { class: 'card export-card', 'aria-labelledby': 'expTitle' }, [
      el('h3', { class: 'card-title', id: 'expTitle', text: '내보내기' }),
      el('p', { class: 'card-sub', text: '최신 버전을 파일로 받아요. 게시 전에 사람이 한 번 더 읽어 주세요.' }),
      el('div', { class: 'export-list' }, list.map(function (x, i) {
        var inner = [el('b', { text: x.label + (i === 0 ? ' · 추천' : '') }), el('span', { text: x.sub })];
        if (!x.available) {
          return el('div', { class: 'export-link', 'data-disabled': 'true', 'aria-disabled': 'true' }, inner.concat([el('small', { class: 'export-hint', text: x.hint || '이 서버에서는 만들 수 없어요.' })]));
        }
        return el('a', { class: 'export-link', href: x.url, download: '', 'data-format': x.format }, x.hint ? inner.concat([el('small', { class: 'export-hint', text: x.hint })]) : inner);
      }))
    ]);
  }

  // ------------------------------------------------------------------ review + versions
  function reviewCard(item, v) {
    var r = v && v.review;
    var body;
    if (r) {
      body = U.reviewDetails(r);
    } else {
      var preview = v && v.draft && v.draft.content ? ws.measure(item.channel, v.draft, S.detail.brief, isDemo() ? null : S.profile) : [];
      body = [
        el('p', { class: 'empty', text: v ? '이 버전(v' + v.version + ')은 아직 검수하지 않았어요.' + (isDemo() ? '' : ' 재검수하면 루브릭 점수와 사실 확인을 받아요.') : '검수 기록이 없어요.' }),
        preview.length ? U.formatChecksBlock(preview, '형식 미리 확인 (브라우저 계산 · 참고용)') : null
      ];
    }
    return el('details', { class: 'card fold', open: true }, [
      el('summary', { class: 'fold-head' }, [
        el('h3', { class: 'card-title', text: '검수 결과' }),
        el('span', { class: 'fold-meta', text: r ? 'v' + v.version + ' · R' + (r.round || 0) + ' · ' + r.score + '점 ' + (r.passed ? '통과' : '미통과') : '검수 전' })
      ]),
      el('div', { class: 'fold-body review-body' }, body)
    ]);
  }

  var SOURCE_LABEL = { agent: '에이전트', human: '사람 수정' };
  function versionsCard(item, viewed) {
    var vs = versions().slice().reverse();
    var list = el('ol', { class: 'versions' }, vs.map(function (v) {
      var r = v.review;
      var prev = versions().filter(function (x) { return x.version < v.version; }).pop();
      var isViewed = viewed && viewed.version === v.version;
      return el('li', { class: 'version', 'aria-current': isViewed ? 'true' : null }, [
        el('div', { class: 'v-head' }, [
          el('b', { class: 'v-num', text: 'v' + v.version }),
          el('span', { class: 'v-src', 'data-src': v.source, text: (SOURCE_LABEL[v.source] || v.source) + (v.source === 'agent' ? ' R' + ((v.draft && v.draft.round) || 0) : '') }),
          r ? ui.scoreBadge(r.score, r.passed) : el('span', { class: 'score-badge', 'data-state': 'none', text: '검수 전' }),
          v.created_at ? el('span', { class: 'v-time', text: ws.date.dateTime(v.created_at) }) : null
        ]),
        v.instructions ? el('p', { class: 'v-ins' }, [el('b', { text: '수정 지시 ' }), v.instructions]) : null,
        v.draft && v.draft.change_log && v.draft.change_log.length ? el('ul', { class: 'v-log' }, v.draft.change_log.map(function (c) { return el('li', { text: c }); })) : null,
        el('div', { class: 'v-actions' }, [
          isViewed ? el('span', { class: 'v-viewing', text: '보는 중' }) : el('button', {
            type: 'button', class: 'btn btn--small', 'data-key': 'view-' + v.version, text: '이 버전 보기',
            onclick: function () { S.viewVersion = v.version; S.editing = false; renderDetail(false); var h = document.getElementById('libraryTitle'); if (h) h.scrollIntoView({ block: 'start', behavior: U.reduceMotion() ? 'auto' : 'smooth' }); }
          }),
          prev ? el('button', {
            type: 'button', class: 'btn btn--small', 'data-key': 'diff-' + v.version,
            'aria-pressed': String(!!(S.diff && S.diff.to === v.version)),
            disabled: !!(isDemo() && (v.demoPartial || prev.demoPartial)),
            title: isDemo() && (v.demoPartial || prev.demoPartial) ? '데모 기록에는 이전 라운드 전문이 없어 비교할 수 없어요' : null,
            text: '이전 버전(v' + prev.version + ')과 비교',
            onclick: function () { S.diff = S.diff && S.diff.to === v.version ? null : { from: prev.version, to: v.version }; renderDetail(false); }
          }) : null
        ]),
        S.diff && S.diff.to === v.version ? diffView(prev, v) : null
      ]);
    }));
    return el('details', { class: 'card fold', open: true }, [
      el('summary', { class: 'fold-head' }, [el('h3', { class: 'card-title', text: '버전 기록' }), el('span', { class: 'fold-meta', text: vs.length + '개' })]),
      el('div', { class: 'fold-body' }, [
        isDemo() ? el('p', { class: 'pane-note', text: '데모 기록에는 최종 버전의 전문만 들어 있어서 비교는 로컬 앱에서만 할 수 있어요.' }) : null,
        list
      ])
    ]);
  }

  function diffView(a, b) {
    var da = (a && a.draft) || {}, db = (b && b.draft) || {};
    var ops = ws.lineDiff(da.content || '', db.content || '');
    var adds = ops.filter(function (o) { return o.op === 'add'; }).length;
    var dels = ops.filter(function (o) { return o.op === 'del'; }).length;
    var rows = [];
    var CONTEXT = 1;
    // collapse long runs of unchanged lines, keeping one line of context around changes
    for (var i = 0; i < ops.length; i++) {
      if (ops[i].op !== 'eq') { rows.push(ops[i]); continue; }
      var j = i;
      while (j < ops.length && ops[j].op === 'eq') j++;
      var run = j - i;
      var lead = i === 0 ? 0 : CONTEXT, trail = j === ops.length ? 0 : CONTEXT;
      if (run > lead + trail + 1) {
        for (var k = 0; k < lead; k++) rows.push(ops[i + k]);
        rows.push({ op: 'skip', n: run - lead - trail });
        for (k = j - trail; k < j; k++) rows.push(ops[k]);
      } else {
        for (k = i; k < j; k++) rows.push(ops[k]);
      }
      i = j - 1;
    }
    var tagsA = (da.hashtags || []).join(' '), tagsB = (db.hashtags || []).join(' ');
    return el('div', { class: 'diff', role: 'region', 'aria-label': '버전 비교: v' + a.version + ' → v' + b.version }, [
      el('p', { class: 'diff-sum' }, [
        el('b', { text: 'v' + a.version + ' → v' + b.version }),
        ' · ', el('span', { class: 'd-add', text: '+' + adds + '줄' }), ' ', el('span', { class: 'd-del', text: '−' + dels + '줄' })
      ]),
      da.title !== db.title ? el('p', { class: 'diff-meta' }, [el('b', { text: '제목 ' }), el('del', { text: da.title || '(없음)' }), ' → ', el('ins', { text: db.title || '(없음)' })]) : null,
      tagsA !== tagsB ? el('p', { class: 'diff-meta' }, [el('b', { text: '태그 ' }), el('del', { text: tagsA || '(없음)' }), ' → ', el('ins', { text: tagsB || '(없음)' })]) : null,
      !adds && !dels ? el('p', { class: 'empty', text: '본문은 바뀌지 않았어요.' }) : el('ol', { class: 'diff-lines' }, rows.map(function (o) {
        if (o.op === 'skip') return el('li', { 'data-op': 'skip', text: '… 같은 줄 ' + o.n + '개' });
        return el('li', { 'data-op': o.op }, [
          el('span', { class: 'd-mark', 'aria-hidden': 'true', text: o.op === 'add' ? '+' : o.op === 'del' ? '−' : ' ' }),
          o.op !== 'eq' ? el('span', { class: 'sr-only', text: o.op === 'add' ? '추가: ' : '삭제: ' }) : null,
          el('span', { class: 'd-text', text: o.text || ' ' })
        ]);
      }))
    ]);
  }
})();
