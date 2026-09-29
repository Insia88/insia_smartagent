/* INSIA 에이전트 스튜디오 — 브랜드·자료 (company profile + user materials).
 *
 * Profile form with every Profile field (lists one per line, team editor with the
 * blind-review note), a completeness meter, and the documents list: upload .txt/.md
 * read in the browser (UTF-8, falling back to EUC-KR), paste text, preview, delete.
 * PDF/DOCX are not parsed here; the user is pointed to `insia docs add`.
 *
 * The "API 게시 연결" card (#/brand/connections, drawn by publish.js) is the only place where API publishing is
 * set up. `#/brand/connections/linkedin/<result>` is where the LinkedIn OAuth callback sends the browser back
 * (the router passes "connections/linkedin/<result>" as the param; publish.js shows the sentence and replaces
 * the address with #/brand/connections).
 */
(function () {
  'use strict';

  var I = window.INSIA;
  var U = I.util;
  var ws = I.ws;
  var ui = ws.ui;
  var el = U.el;

  var LIST_FIELDS = ['differentiators', 'traction', 'banned_words', 'required_phrases', 'default_hashtags', 'brand_colors'];
  var GROUPS = [
    { title: '기본 정보', lead: '모든 채널이 공통으로 쓰는 회사·서비스 사실이에요.', fields: [
      { k: 'company_name', label: '회사명' },
      { k: 'service_name', label: '서비스명' },
      { k: 'one_liner', label: '한 줄 소개', wide: true, hint: '예: 1인 창업자의 사업계획서와 SNS 글을 AI 에이전트 셋이 함께 써요' },
      { k: 'description', label: '서비스 설명', type: 'textarea', rows: 4, wide: true },
      { k: 'industry', label: '업종', hint: '예: AI 소프트웨어(SaaS)' },
      { k: 'stage', label: '창업 단계', list: ['예비창업', '초기(3년 이내)', '도약(3~7년)'] }
    ] },
    { title: '고객 · 문제 · 해결', lead: '사업계획서의 문제인식·실현가능성과 SNS 글의 관점에 쓰여요. 과장 없이 사실만 적어 주세요.', fields: [
      { k: 'target_customers', label: '타깃 고객', type: 'textarea', rows: 2, wide: true },
      { k: 'problem', label: '고객이 겪는 문제', type: 'textarea', rows: 3, wide: true },
      { k: 'solution', label: '해결 방법', type: 'textarea', rows: 3, wide: true },
      { k: 'differentiators', label: '차별점', type: 'list', rows: 3, wide: true, hint: '한 줄에 하나씩' },
      { k: 'business_model', label: '비즈니스 모델', type: 'textarea', rows: 2, wide: true },
      { k: 'pricing', label: '가격', hint: "확정 가격이 아니면 '가정'이라고 적어 주세요" },
      { k: 'traction', label: '실적·지표', type: 'list', rows: 3, wide: true, hint: '한 줄에 하나씩, 기준 시점과 함께 (예: 베타 사용자 120명, 2026-08 기준)' }
    ] },
    { title: '팀', team: true },
    { title: '브랜드 규칙', lead: '초안을 쓸 때 지키고, 검수할 때 코드로 확인해요.', fields: [
      { k: 'tone', label: '톤앤매너', type: 'textarea', rows: 2, wide: true },
      { k: 'banned_words', label: '금지 표현', type: 'list', rows: 3, hint: '한 줄에 하나씩 · 쓰면 형식 검사에서 걸려요' },
      { k: 'required_phrases', label: '필수 문구', type: 'list', rows: 3, hint: '한 줄에 하나씩 · SNS 글에만 적용 (예: 광고 표시)' },
      { k: 'default_hashtags', label: '기본 해시태그', type: 'list', rows: 3, hint: '한 줄에 하나씩 · #은 자동으로 붙어요' },
      { k: 'brand_colors', label: '브랜드 색', type: 'list', rows: 3, hint: '한 줄에 하나씩 #RRGGBB · 첫 번째가 주 색 (카드뉴스에 쓰여요)', colors: true },
      { k: 'cta', label: '기본 행동 유도 문구', wide: true, hint: '예: 무료 체험 신청은 프로필 링크에서' },
      { k: 'contact', label: '문의처', wide: true, hint: '이메일·네이버 톡톡 등' }
    ] },
    { title: '채널', fields: [
      { k: 'naver_blog_url', label: '네이버 블로그 주소', type: 'url', hint: 'https://blog.naver.com/…' },
      { k: 'linkedin_url', label: '링크드인 주소', type: 'url', hint: 'https://www.linkedin.com/…' },
      { k: 'instagram_handle', label: '인스타그램 계정', hint: '@계정이름' }
    ] },
    { title: '메모', fields: [
      { k: 'notes', label: '에이전트에게 알려 줄 기타 사실', type: 'textarea', rows: 3, wide: true }
    ] }
  ];
  var COMPLETE = [
    ['company_name', '회사명'], ['service_name', '서비스명'], ['one_liner', '한 줄 소개'], ['description', '서비스 설명'],
    ['target_customers', '타깃 고객'], ['problem', '고객이 겪는 문제'], ['solution', '해결 방법'], ['differentiators', '차별점'],
    ['business_model', '비즈니스 모델'], ['team', '팀 역할'], ['tone', '톤앤매너'], ['cta', '기본 행동 유도 문구']
  ];

  var B = {
    container: null,
    profile: null,
    form: null,
    loadError: null,
    saving: false,
    saveError: '',
    savedAt: '',
    docs: null,
    docsError: null,
    docMsgs: [],
    confirmDelete: '',
    previewDoc: '',
    connParam: '',        // the route param for the API 게시 연결 card, consumed by the next render
    connNav: false        // the next render follows a navigation: reload the publishing status
  };

  function show(container, param, ctx) {
    B.container = container;
    if (ws.mode !== 'live') {
      ws.mount(container, ui.demoExplainer('brand', '브랜드·자료',
        '회사 프로필과 참고 자료를 한 번 넣어 두면 세 에이전트가 모든 초안에 같은 사실과 브랜드 규칙을 써요. 프로필에 적은 사실은 출처 없이 쓸 수 있지만 부풀리지 않고, 사업계획서에는 “(자사 자료)”로 표시해요.', [
          '회사명·서비스·고객·문제·해결·가격·실적, 톤앤매너와 금지 표현·필수 문구·기본 해시태그·브랜드 색을 저장해요.',
          '팀원은 역할과 경력만 사업계획서에 쓰고, 이름은 블라인드 심사 규정 때문에 절대 넣지 않아요.',
          '회사 소개서나 IR 자료(.txt·.md)를 올리면 리서치 팩에 “사용자 제공 자료”로 들어가요. PDF·Word는 insia docs add로 올려요.',
          '완성도 표시로 무엇을 더 채우면 초안이 정확해지는지 알려 줘요.'
        ]));
      if (ctx && ctx.focus) ui.focusHeading(document.getElementById('brandTitle'));
      return;
    }
    B.connParam = param || '';
    B.connNav = true;
    render(ctx && ctx.focus && !/^connections/.test(B.connParam));
    if (!B.profile) loadProfile();
    loadDocs();
  }
  function reset() { B.profile = null; B.form = null; B.docs = null; }
  I.views.brand = { show: show, reset: reset };
  function isShown() { return B.container && !B.container.hidden && ws.route.view === 'brand'; }

  // ------------------------------------------------------------------ data
  function blankProfile() {
    return {
      company_name: '', service_name: '', one_liner: '', description: '', industry: '', stage: '', target_customers: '', problem: '',
      solution: '', differentiators: [], business_model: '', pricing: '', traction: [], team: [], tone: '', banned_words: [],
      required_phrases: [], default_hashtags: [], cta: '', contact: '', naver_blog_url: '', linkedin_url: '', instagram_handle: '',
      brand_colors: [], notes: '', updated_at: ''
    };
  }
  function clone(p) { return JSON.parse(JSON.stringify(p)); }
  function loadProfile() {
    ws.get('/api/profile').then(function (resp) {
      var p = Object.assign(blankProfile(), ws.unwrap(resp, 'profile') || {});
      p.team = Array.isArray(p.team) ? p.team : [];
      B.profile = p;
      B.form = clone(p);
      B.loadError = null;
      if (isShown()) render(false);
    }, function (err) {
      B.loadError = err;
      if (isShown()) render(false);
    });
  }
  function loadDocs() {
    ws.get('/api/documents').then(function (resp) {
      B.docs = ws.listOf(resp, 'documents');
      B.docsError = null;
      if (isShown()) renderDocs();
    }, function (err) {
      B.docsError = err;
      if (isShown()) renderDocs();
    });
  }

  function normalized(form) {
    var p = clone(form);
    LIST_FIELDS.forEach(function (k) {
      var v = p[k];
      var arr = Array.isArray(v) ? v : String(v || '').split(/\r?\n/);
      arr = arr.map(function (x) { return String(x).trim(); }).filter(Boolean);
      if (k === 'default_hashtags') arr = arr.map(function (t) { return t.charAt(0) === '#' ? t : '#' + t.replace(/^#+/, ''); });
      if (k === 'brand_colors') arr = arr.map(function (c) { return c.charAt(0) === '#' ? c.toUpperCase() : ('#' + c).toUpperCase(); });
      p[k] = arr;
    });
    p.team = (p.team || []).map(function (m) {
      return { role: String(m.role || '').trim(), name: String(m.name || '').trim(), background: String(m.background || '').trim(), hiring: !!m.hiring };
    }).filter(function (m) { return m.role || m.name || m.background; });
    Object.keys(p).forEach(function (k) { if (typeof p[k] === 'string') p[k] = p[k].trim(); });
    delete p.updated_at;
    return p;
  }
  function isDirty() {
    if (!B.profile || !B.form) return false;
    var a = normalized(B.form), b = normalized(B.profile);
    return JSON.stringify(a) !== JSON.stringify(b);
  }
  function filled(p, k) {
    if (k === 'team') return (p.team || []).some(function (m) { return String(m.role || '').trim(); });
    var v = p[k];
    return Array.isArray(v) ? v.some(function (x) { return String(x).trim(); }) : !!String(v || '').trim();
  }

  // ------------------------------------------------------------------ render
  function render(focus) {
    var head = ui.viewHead('brand', '브랜드·자료', '회사 프로필과 참고 자료를 넣어 두면 모든 초안이 같은 사실과 브랜드 규칙을 따라요. 여기 적은 사실만 출처 없이 쓸 수 있어요.');
    var formCol;
    if (B.loadError && !B.profile) formCol = ui.errorState(B.loadError, function () { B.loadError = null; render(false); loadProfile(); });
    else if (!B.form) formCol = ui.loading('프로필을 불러오는 중이에요…');
    else formCol = profileForm();
    ws.mount(B.container, [
      head,
      el('div', { class: 'brand-layout' }, [
        el('div', { class: 'brand-meter', id: 'brandMeter' }),
        el('div', { class: 'brand-form' }, formCol),
        el('section', { class: 'brand-docs card', id: 'brandDocs', 'aria-labelledby': 'docsTitle' }),
        // filled by publish.js only when the server has API publishing turned on (INSIA_PUBLISH=0 → stays hidden)
        el('section', { class: 'brand-conn card', id: 'brandConn', 'aria-labelledby': 'pubConnTitle', hidden: true })
      ])
    ]);
    refreshMeta();
    renderDocs();
    var conn = document.getElementById('brandConn');
    if (conn && I.publish && I.publish.connectionsCard) {
      var param = B.connParam, nav = B.connNav;
      B.connParam = '';
      B.connNav = false;
      I.publish.connectionsCard(conn, { param: param, focus: !!param, reload: nav });
    }
    if (focus) ui.focusHeading(document.getElementById('brandTitle'));
  }

  function fieldNode(f) {
    var id = 'pf-' + f.k;
    var v = B.form[f.k];
    var input;
    if (f.type === 'textarea' || f.type === 'list') {
      input = el('textarea', { id: id, rows: String(f.rows || 3), 'aria-describedby': f.hint ? id + '-hint' : null });
      input.value = Array.isArray(v) ? v.join('\n') : String(v || '');
    } else {
      input = el('input', {
        id: id, type: f.type === 'url' ? 'url' : 'text', value: String(v || ''), autocomplete: 'off',
        inputmode: f.type === 'url' ? 'url' : null, list: f.list ? id + '-list' : null, 'aria-describedby': f.hint ? id + '-hint' : null
      });
    }
    input.addEventListener('input', function () {
      B.form[f.k] = f.type === 'list' ? input.value.split(/\r?\n/) : input.value;
      if (f.colors) paintSwatches(swatches, input.value);
      refreshMeta();
    });
    var swatches = f.colors ? el('span', { class: 'swatches', 'aria-hidden': 'true' }) : null;
    if (swatches) paintSwatches(swatches, input.value);
    return el('div', { class: 'field' + (f.wide ? ' field--wide' : '') }, [
      el('label', { for: id, text: f.label }),
      input,
      f.list ? el('datalist', { id: id + '-list' }, f.list.map(function (o) { return el('option', { value: o }); })) : null,
      swatches,
      f.hint ? el('small', { class: 'field-hint', id: id + '-hint', text: f.hint }) : null
    ]);
  }
  function paintSwatches(node, text) {
    node.textContent = '';
    String(text || '').split(/\r?\n/).map(function (x) { return x.trim(); }).filter(Boolean).slice(0, 8).forEach(function (c) {
      var hex = c.charAt(0) === '#' ? c : '#' + c;
      var ok = /^#[0-9a-fA-F]{6}$/.test(hex);
      node.appendChild(el('span', { class: 'swatch', 'data-ok': String(ok), style: ok ? '--sw:' + hex : null, title: ok ? hex : hex + ' (형식 오류)' }));
    });
  }

  function teamEditor() {
    var wrap = el('div', { class: 'team', id: 'teamEditor' });
    function draw() {
      wrap.textContent = '';
      var rows = B.form.team.map(function (m, i) {
        var base = 'tm-' + i;
        var role = el('input', { id: base + '-role', type: 'text', value: m.role || '', autocomplete: 'off', placeholder: '예: 대표, CTO, 마케팅 담당' });
        var name = el('input', { id: base + '-name', type: 'text', value: m.name || '', autocomplete: 'off', 'aria-describedby': 'teamNote' });
        var bg = el('textarea', { id: base + '-bg', rows: '2', placeholder: '학위·전공, 경력, 보유 역량 (사실만)' });
        bg.value = m.background || '';
        var hiring = el('input', { id: base + '-hiring', type: 'checkbox', checked: !!m.hiring });
        role.addEventListener('input', function () { m.role = role.value; refreshMeta(); });
        name.addEventListener('input', function () { m.name = name.value; refreshMeta(); });
        bg.addEventListener('input', function () { m.background = bg.value; refreshMeta(); });
        hiring.addEventListener('change', function () { m.hiring = hiring.checked; refreshMeta(); });
        return el('li', { class: 'team-row' }, [
          el('div', { class: 'team-grid' }, [
            el('div', { class: 'field' }, [el('label', { for: base + '-role', text: '역할' }), role]),
            el('div', { class: 'field' }, [el('label', { for: base + '-name' }, ['이름 ', el('small', { text: 'SNS 글에만 · 사업계획서엔 안 나와요' })]), name]),
            el('div', { class: 'field field--wide' }, [el('label', { for: base + '-bg', text: '경력·역량' }), bg]),
            el('label', { class: 'check', for: base + '-hiring' }, [hiring, ' 채용 예정 인력']),
            el('button', {
              type: 'button', class: 'btn btn--small btn--ghost team-remove', 'aria-label': (m.role || '팀원 ' + (i + 1)) + ' 삭제', text: '삭제',
              onclick: function () {
                B.form.team.splice(i, 1);
                draw();
                refreshMeta();
                var next = wrap.querySelector('.team-remove') || document.getElementById('teamAdd');
                if (next) next.focus();
              }
            })
          ])
        ]);
      });
      U.appendChildren(wrap, [
        el('p', { class: 'team-note', id: 'teamNote' }, [
          el('b', { text: '이름은 사업계획서에 절대 나오지 않아요. ' }),
          '예비창업패키지 등은 블라인드 심사라서 실명·학교·회사명이 드러나면 감점이나 탈락 사유가 돼요. 사업계획서에는 역할과 경력만 “[대표자]”처럼 가려서 쓰고, 이름은 SNS 글에서만 써요.'
        ]),
        rows.length ? el('ol', { class: 'team-list' }, rows) : el('p', { class: 'empty', text: '아직 팀원이 없어요. 대표자부터 추가해 보세요.' }),
        el('button', {
          type: 'button', class: 'btn', id: 'teamAdd', text: '+ 팀원 추가', onclick: function () {
            B.form.team.push({ role: '', name: '', background: '', hiring: false });
            draw();
            refreshMeta();
            var r = document.getElementById('tm-' + (B.form.team.length - 1) + '-role');
            if (r) r.focus();
          }
        })
      ]);
    }
    draw();
    return wrap;
  }

  function profileForm() {
    var err = el('p', { class: 'form-error', id: 'profileError', role: 'alert', hidden: !B.saveError, text: B.saveError });
    var saveBtn = el('button', { type: 'submit', class: 'btn btn--primary', id: 'profileSave', text: B.saving ? '저장하는 중…' : '프로필 저장' });
    var status = el('span', { class: 'save-state', id: 'profileState', role: 'status' });
    var revert = el('button', {
      type: 'button', class: 'btn btn--ghost', id: 'profileRevert', text: '되돌리기', onclick: function () {
        B.form = clone(B.profile);
        B.saveError = '';
        render(false);
        var s = document.getElementById('profileSave');
        if (s) s.focus();
      }
    });
    var form = el('form', { class: 'profile-form', novalidate: true, 'aria-label': '회사 프로필' }, GROUPS.map(function (g, gi) {
      return el('fieldset', { class: 'card pf-group' }, [
        el('legend', { class: 'card-title', text: g.title }),
        g.lead ? el('p', { class: 'card-sub', text: g.lead }) : null,
        g.team ? teamEditor() : el('div', { class: 'pf-grid' }, g.fields.map(fieldNode))
      ]);
    }).concat([
      el('div', { class: 'save-bar' }, [err, el('div', { class: 'save-row' }, [status, revert, saveBtn])])
    ]));
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      save();
    });
    return form;
  }

  function refreshMeta() {
    var meter = document.getElementById('brandMeter');
    if (meter) {
      meter.textContent = '';
      if (B.form) {
        var p = B.form;
        var done = COMPLETE.filter(function (c) { return filled(p, c[0]); });
        var missing = COMPLETE.filter(function (c) { return !filled(p, c[0]); });
        var pct = Math.round(100 * done.length / COMPLETE.length);
        U.appendChildren(meter, el('section', { class: 'card meter-card', 'aria-labelledby': 'meterTitle' }, [
          el('h3', { class: 'card-title', id: 'meterTitle', text: '프로필 완성도' }),
          el('p', { class: 'meter-value' }, [el('b', { text: pct + '%' }), el('span', { text: ' · ' + done.length + ' / ' + COMPLETE.length + '개 항목' })]),
          el('div', { class: 'meter', role: 'meter', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(pct), 'aria-label': '프로필 완성도 ' + pct + '%', 'data-level': pct >= 80 ? 'good' : pct >= 50 ? 'mid' : 'low' },
            el('span', { class: 'meter-fill', style: 'width:' + pct + '%' })),
          missing.length ? el('div', { class: 'meter-missing' }, [
            el('p', { class: 'card-sub', text: '채우면 초안이 더 정확해져요' }),
            el('div', { class: 'miss-chips' }, missing.map(function (c) {
              return el('button', {
                type: 'button', class: 'miss-chip', text: c[1], onclick: function () {
                  var n = c[0] === 'team' ? (document.getElementById('tm-0-role') || document.getElementById('teamAdd')) : document.getElementById('pf-' + c[0]);
                  if (n) { n.focus(); if (n.scrollIntoView) n.scrollIntoView({ block: 'center', behavior: U.reduceMotion() ? 'auto' : 'smooth' }); }
                }
              });
            }))
          ]) : el('p', { class: 'card-sub', text: '핵심 항목을 모두 채웠어요.' }),
          B.profile && B.profile.updated_at ? el('p', { class: 'meter-foot', text: '마지막 저장 ' + ws.date.dateTime(B.profile.updated_at) }) : null
        ]));
      }
    }
    var st = document.getElementById('profileState');
    var save = document.getElementById('profileSave');
    var revert = document.getElementById('profileRevert');
    if (st && save) {
      var dirty = isDirty();
      st.textContent = B.saving ? '저장하는 중이에요' : dirty ? '저장하지 않은 변경 사항이 있어요' : (B.savedAt ? B.savedAt + '에 저장했어요' : '변경 사항 없음');
      st.dataset.dirty = String(dirty);
      save.disabled = B.saving || !dirty;
      if (revert) revert.hidden = !dirty;
    }
  }

  function save() {
    var p = normalized(B.form);
    var badColor = p.brand_colors.filter(function (c) { return !/^#[0-9A-F]{6}$/.test(c); })[0];
    var badUrl = ['naver_blog_url', 'linkedin_url'].filter(function (k) { return p[k] && !/^https?:\/\/\S+$/i.test(p[k]); })[0];
    var err = document.getElementById('profileError');
    function fail(msg, focusId) {
      B.saveError = msg;
      if (err) { err.textContent = msg; err.hidden = false; }
      var n = document.getElementById(focusId);
      if (n) n.focus();
    }
    if (badColor) return fail('브랜드 색 “' + badColor + '”는 #RRGGBB 형식이 아니에요.', 'pf-brand_colors');
    if (badUrl) return fail('채널 주소는 http:// 또는 https://로 시작해야 해요.', 'pf-' + badUrl);
    B.saving = true;
    B.saveError = '';
    if (err) err.hidden = true;
    refreshMeta();
    ws.put('/api/profile', p).then(function (resp) {
      var saved = Object.assign(blankProfile(), ws.unwrap(resp, 'profile') || p);
      B.profile = saved;
      if (ws.setProfile) ws.setProfile(saved);  // the 보관함 editor's brand checks use the new rules
      B.form = clone(saved);
      B.saving = false;
      // same clock as the rest of the app (KST, from the server's updated_at)
      var now = new Date();
      B.savedAt = saved.updated_at ? ws.date.dateTime(saved.updated_at) : (now.getHours() < 10 ? '0' : '') + now.getHours() + ':' + (now.getMinutes() < 10 ? '0' : '') + now.getMinutes();
      if (ws.health) ws.health.profile_complete = COMPLETE.every(function (c) { return filled(saved, c[0]); });
      ui.toast('프로필을 저장했어요. 다음 실행부터 반영돼요.', 'success');
      render(false);
      var s = document.getElementById('profileState');
      if (s) { s.setAttribute('tabindex', '-1'); s.focus(); }
    }, function (ex) {
      B.saving = false;
      refreshMeta();
      if (ex.auth) return;
      fail('저장하지 못했어요: ' + ex.message, 'profileSave');
    });
  }

  // ------------------------------------------------------------------ documents
  function docBudget() { return (ws.health && ws.health.max_document_chars) || 60000; }

  function renderDocs() {
    var box = document.getElementById('brandDocs');
    if (!box) return;
    var keep = document.activeElement && box.contains(document.activeElement) ? (document.activeElement.id || document.activeElement.getAttribute('data-key')) : null;
    box.textContent = '';
    var docs = B.docs || [];
    var total = docs.reduce(function (a, d) { return a + (Number(d.chars) || String(d.text || '').length); }, 0);
    var budget = docBudget();

    var fileInput = el('input', { id: 'docFile', type: 'file', class: 'sr-only', multiple: true, accept: '.txt,.md,.markdown,text/plain,text/markdown' });
    fileInput.addEventListener('change', function () { handleFiles(fileInput.files); fileInput.value = ''; });
    var drop = el('div', { class: 'dropzone' }, [
      el('label', { class: 'btn btn--primary file-btn', for: 'docFile' }, ['파일 올리기 (.txt · .md)', fileInput]),
      el('span', { class: 'drop-hint', text: '또는 파일을 여기로 끌어다 놓으세요' })
    ]);
    drop.addEventListener('dragover', function (e) { e.preventDefault(); drop.classList.add('is-over'); });
    drop.addEventListener('dragleave', function () { drop.classList.remove('is-over'); });
    drop.addEventListener('drop', function (e) { e.preventDefault(); drop.classList.remove('is-over'); if (e.dataTransfer && e.dataTransfer.files) handleFiles(e.dataTransfer.files); });

    var pTitle = el('input', { id: 'pasteTitle', type: 'text', autocomplete: 'off', placeholder: '예: 회사 소개서 요약' });
    var pText = el('textarea', { id: 'pasteText', rows: '6', placeholder: 'PDF·Word에서 복사한 내용을 붙여 넣어도 돼요' });
    var pErr = el('p', { class: 'form-error', role: 'alert', hidden: true });
    var paste = el('details', { class: 'paste', id: 'pasteBox' }, [
      el('summary', { text: '직접 붙여넣기' }),
      el('form', { class: 'paste-form', novalidate: true }, [
        el('label', { class: 'field', for: 'pasteTitle' }, [el('span', { text: '자료 제목' }), pTitle]),
        el('label', { class: 'field', for: 'pasteText' }, [el('span', { text: '본문' }), pText]),
        pErr,
        el('div', { class: 'form-foot' }, [el('button', { type: 'submit', class: 'btn btn--primary', id: 'pasteSubmit', text: '자료 추가' })])
      ])
    ]);
    paste.querySelector('form').addEventListener('submit', function (e) {
      e.preventDefault();
      var text = pText.value;
      if (!text.trim()) { pErr.textContent = '본문을 붙여 넣어 주세요.'; pErr.hidden = false; pText.focus(); return; }
      var title = pTitle.value.trim() || firstLine(text);
      var btn = document.getElementById('pasteSubmit');
      btn.disabled = true;
      addDocument({ title: title, text: text, kind: 'text', filename: '' }).then(function (ok) {
        btn.disabled = false;
        if (ok) { pTitle.value = ''; pText.value = ''; paste.open = false; }
      });
    });

    var list;
    if (B.docsError && !B.docs) list = ui.errorState(B.docsError, loadDocs);
    else if (!B.docs) list = ui.loading('자료 목록을 불러오는 중이에요…');
    else if (!docs.length) list = el('p', { class: 'empty', text: '아직 올린 자료가 없어요. 회사 소개서, IR 자료, 서비스 설명서처럼 에이전트가 참고할 사실이 담긴 글을 올려 보세요.' });
    else list = el('ul', { class: 'doc-list' }, docs.map(docRow));

    U.appendChildren(box, [
      el('div', { class: 'docs-head' }, [
        el('h3', { class: 'card-title', id: 'docsTitle', text: '참고 자료' }),
        el('span', { class: 'fold-meta', text: docs.length + '개 · ' + U.fmtNum(total) + '자' })
      ]),
      el('p', { class: 'card-sub', text: '올린 자료는 리서치 팩에 “사용자 제공 자료”(Tier 1, user://자료번호)로 들어가요. 에이전트는 검증되지 않은 자기 보고로 다루고, 실행 때 본문은 합계 ' + U.fmtNum(budget) + '자까지 보내요. 넘으면 잘라서 알려 드려요.' }),
      total > budget ? ui.notice('warn', '자료 합계가 ' + U.fmtNum(total) + '자예요. 실행 때 ' + U.fmtNum(budget) + '자까지만 보내요.') : null,
      drop,
      el('div', { class: 'doc-msgs', role: 'status', 'aria-live': 'polite' }, B.docMsgs.map(function (m) { return ui.notice(m.kind, m.text); })),
      paste,
      list
    ]);
    if (keep) { var n = document.getElementById(keep) || box.querySelector('[data-key="' + keep + '"]'); if (n) n.focus(); }
  }

  var KIND_LABEL = { text: '텍스트', markdown: '마크다운', pdf: 'PDF', docx: 'Word' };
  function docRow(d) {
    var confirming = B.confirmDelete === d.id;
    var previewing = B.previewDoc === d.id;
    return el('li', { class: 'doc' }, [
      el('div', { class: 'doc-main' }, [
        el('span', { class: 'doc-id', text: d.id }),
        el('span', { class: 'doc-title', text: d.title || d.filename || '(제목 없음)' }),
        el('span', { class: 'doc-meta', text: [KIND_LABEL[d.kind] || d.kind, U.fmtNum(Number(d.chars) || String(d.text || '').length) + '자', d.filename, d.created_at ? ws.date.dateTime(d.created_at) : ''].filter(Boolean).join(' · ') })
      ]),
      el('div', { class: 'doc-actions' }, confirming ? [
        el('span', { class: 'confirm-text', text: '삭제할까요?' }),
        el('button', { type: 'button', class: 'btn btn--small btn--danger', 'data-key': 'del-yes-' + d.id, text: '삭제', onclick: function () { deleteDoc(d); } }),
        el('button', { type: 'button', class: 'btn btn--small', 'data-key': 'del-no-' + d.id, text: '취소', onclick: function () { B.confirmDelete = ''; renderDocs(); focusKey('del-' + d.id); } })
      ] : [
        d.text ? el('button', { type: 'button', class: 'btn btn--small', 'data-key': 'prev-' + d.id, 'aria-expanded': String(previewing), text: previewing ? '미리보기 닫기' : '미리보기', onclick: function () { B.previewDoc = previewing ? '' : d.id; renderDocs(); focusKey('prev-' + d.id); } }) : null,
        el('button', { type: 'button', class: 'btn btn--small btn--ghost', 'data-key': 'del-' + d.id, text: '삭제', onclick: function () { B.confirmDelete = d.id; renderDocs(); focusKey('del-yes-' + d.id); } })
      ]),
      previewing ? el('pre', { class: 'doc-preview', tabindex: '0', 'aria-label': (d.title || d.id) + ' 미리보기', text: String(d.text || '').slice(0, 1500) + (String(d.text || '').length > 1500 ? '\n…' : '') }) : null
    ]);
  }
  function focusKey(k) { var n = document.querySelector('#brandDocs [data-key="' + k + '"]'); if (n) n.focus(); }
  function firstLine(t) { return String(t).trim().split(/\r?\n/)[0].slice(0, 60) || '붙여 넣은 자료'; }

  function addDocument(body) {
    return ws.post('/api/documents', body).then(function (resp) {
      var doc = ws.unwrap(resp, 'document');
      B.docMsgs = [{ kind: 'success', text: '“' + (body.title || '자료') + '”을(를) 추가했어요' + (doc && doc.id ? ' (자료 번호 ' + doc.id + ')' : '') + '. 다음 실행부터 리서치에 들어가요.' }];
      loadDocs();
      return true;
    }, function (ex) {
      if (ex.auth) return false;
      B.docMsgs = [{ kind: 'error', text: '“' + (body.title || '자료') + '”을(를) 올리지 못했어요: ' + ex.message }];
      renderDocs();
      return false;
    });
  }

  function deleteDoc(d) {
    ws.del('/api/documents/' + encodeURIComponent(d.id)).then(function () {
      B.confirmDelete = '';
      B.docs = (B.docs || []).filter(function (x) { return x.id !== d.id; });
      B.docMsgs = [{ kind: 'info', text: '“' + (d.title || d.id) + '”을(를) 삭제했어요.' }];
      renderDocs();
      var up = document.querySelector('#brandDocs .file-btn');
      if (up) up.focus();
    }, function (ex) {
      if (ex.auth) return;
      B.confirmDelete = '';
      B.docMsgs = [{ kind: 'error', text: '삭제하지 못했어요: ' + ex.message }];
      renderDocs();
    });
  }

  var MAX_FILE_BYTES = 2 * 1024 * 1024;
  function handleFiles(fileList) {
    var files = Array.prototype.slice.call(fileList || []);
    if (!files.length) return;
    B.docMsgs = [];
    var jobs = files.map(function (file) {
      var name = file.name || '파일';
      var ext = (name.split('.').pop() || '').toLowerCase();
      if (['pdf', 'docx', 'doc', 'hwp', 'hwpx', 'pptx', 'ppt'].indexOf(ext) >= 0) {
        B.docMsgs.push({ kind: 'warn', text: name + ': PDF·Word·한글·파워포인트 파일은 브라우저에서 글자를 뽑지 않아요. 터미널에서 insia docs add "' + name + '"로 올리거나, 내용을 복사해 ‘직접 붙여넣기’에 넣어 주세요.' });
        return Promise.resolve();
      }
      if (['txt', 'md', 'markdown', 'text'].indexOf(ext) < 0 && !/^text\//.test(file.type || '')) {
        B.docMsgs.push({ kind: 'warn', text: name + ': .txt나 .md 파일만 올릴 수 있어요.' });
        return Promise.resolve();
      }
      if (file.size > MAX_FILE_BYTES) {
        B.docMsgs.push({ kind: 'warn', text: name + ': 2MB가 넘어요. 필요한 부분만 나눠서 올려 주세요.' });
        return Promise.resolve();
      }
      return readText(file).then(function (text) {
        if (!text.trim()) { B.docMsgs.push({ kind: 'warn', text: name + ': 내용이 비어 있어요.' }); return; }
        var heading = /^\s*#\s+(.+?)\s*#*\s*$/m.exec(text);
        var title = (ext === 'md' || ext === 'markdown') && heading ? heading[1].slice(0, 120) : name.replace(/\.[^.]+$/, '');
        return ws.post('/api/documents', { title: title, text: text, kind: ext === 'md' || ext === 'markdown' ? 'markdown' : 'text', filename: name }).then(function (resp) {
          var doc = ws.unwrap(resp, 'document');
          B.docMsgs.push({ kind: 'success', text: name + ': 추가했어요 (' + (doc && doc.id ? '자료 번호 ' + doc.id + ' · ' : '') + U.fmtNum(text.length) + '자).' });
        }, function (ex) {
          if (!ex.auth) B.docMsgs.push({ kind: 'error', text: name + ': 올리지 못했어요 — ' + ex.message });
        });
      }, function () {
        B.docMsgs.push({ kind: 'error', text: name + ': 파일을 읽지 못했어요.' });
      });
    });
    renderDocs();
    Promise.all(jobs).then(function () { renderDocs(); loadDocs(); });
  }

  /** Read a text file: UTF-8 first; Korean Windows files saved as EUC-KR/CP949 fall back to that. */
  function readText(file) {
    var buf = file.arrayBuffer ? file.arrayBuffer() : new Promise(function (resolve, reject) {
      var r = new FileReader();
      r.onload = function () { resolve(r.result); };
      r.onerror = reject;
      r.readAsArrayBuffer(file);
    });
    return buf.then(function (ab) {
      var bytes = new Uint8Array(ab);
      var text;
      try { text = new TextDecoder('utf-8', { fatal: true }).decode(bytes); } catch (e) {
        try { text = new TextDecoder('euc-kr').decode(bytes); } catch (e2) { text = new TextDecoder('utf-8').decode(bytes); }
      }
      return text.replace(/^﻿/, '');
    });
  }
})();
