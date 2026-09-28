/* INSIA 에이전트 스튜디오 — dashboard runtime.
 *
 * One classic script (no modules, no build step) so it can be inlined into the
 * claude.ai artifact as-is. Same renderer for demo replay and live runs:
 *   events --applyEvent(state, ev)--> state --render()--> DOM
 * applyEvent is pure (returns a new state object, never mutates the old one);
 * packets and flashes are side effects triggered only for events applied in
 * real time, never while seeking.
 */
(function () {
  'use strict';

  // ------------------------------------------------------------------ constants
  var AGENT_IDS = ['orchestrator', 'researcher', 'reviewer'];
  var CHANNEL_IDS = ['bizplan', 'naver_blog', 'linkedin', 'instagram'];
  var AGENT_SHORT = { orchestrator: '총괄', researcher: '리서치', reviewer: '검수', system: '시스템' };
  var CHANNEL_NAMES = { bizplan: '사업계획서', naver_blog: '네이버 블로그', linkedin: '링크드인', instagram: '인스타그램' };
  var STATUS_LABEL = {
    idle: '대기', planning: '기획 중', searching: '검색 중', reading: '자료 읽는 중', writing: '작성 중',
    reviewing: '검수 중', revising: '수정 중', waiting: '대기', done: '완료', error: '오류'
  };
  var ACTIVE_STATUSES = { planning: 1, searching: 1, reading: 1, writing: 1, reviewing: 1, revising: 1 };
  var CH_STATE_LABEL = {
    waiting: '대기', drafting: '초안 작성', queued: '검수 대기', reviewing: '검수 중',
    revision: '수정 요청', passed: '완료', failed: '미통과', error: '오류'
  };
  var TIER_LABEL = { 1: 'Tier 1 공식·공공', 2: 'Tier 2 언론·리서치', 3: 'Tier 3 기타' };
  var SEVERITY_LABEL = { critical: '치명', major: '중요', minor: '사소' };
  var PHASES = ['plan', 'research', 'draft', 'review', 'done'];
  var MODEL_VIEWER_SRC = 'https://cdn.jsdelivr.net/npm/@google/model-viewer@4.0.0/dist/model-viewer.min.js';
  var DEFAULT_MANIFEST = {
    version: 1,
    credit: '',
    agents: {
      orchestrator: { name: '총괄 에이전트', en: 'Orchestrator', nickname: '유자 디렉터', color: '#6D5EF5', image: 'agents/orchestrator.webp', video: null, model: null },
      researcher: { name: '리서치 에이전트', en: 'Researcher', nickname: '돋보기 탐험가', color: '#14B8A6', image: 'agents/researcher.webp', video: null, model: null },
      reviewer: { name: '검수 에이전트', en: 'Reviewer', nickname: '꼼꼼 검수관', color: '#FF7A59', image: 'agents/reviewer.webp', video: null, model: null }
    },
    hero: {},
    channels: {
      bizplan: { name: '사업계획서', icon: 'channels/bizplan.webp', color: '#3B5BDB' },
      naver_blog: { name: '네이버 블로그', icon: 'channels/naver_blog.webp', color: '#03C75A' },
      linkedin: { name: '링크드인', icon: 'channels/linkedin.webp', color: '#0A66C2' },
      instagram: { name: '인스타그램', icon: 'channels/instagram.webp', color: '#E1306C' }
    }
  };

  // ------------------------------------------------------------------ small helpers
  function $(id) { return document.getElementById(id); }
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === 'class') node.className = v;
        else if (k === 'text') node.textContent = v;
        else if (k === 'style') node.setAttribute('style', v);
        else if (k.slice(0, 2) === 'on' && typeof v === 'function') node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? '' : String(v));
      });
    }
    appendChildren(node, children);
    return node;
  }
  function appendChildren(node, children) {
    if (children === null || children === undefined || children === false) return;
    if (!Array.isArray(children)) children = [children];
    children.forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      if (Array.isArray(c)) appendChildren(node, c);
      else if (typeof c === 'string' || typeof c === 'number') node.appendChild(document.createTextNode(String(c)));
      else node.appendChild(c);
    });
  }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function fmtClock(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    return pad2(Math.floor(sec / 60)) + ':' + pad2(sec % 60);
  }
  function fmtDuration(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    var m = Math.floor(sec / 60), s = sec % 60;
    return m ? m + '분 ' + pad2(s) + '초' : s + '초';
  }
  function fmtNum(n) { return typeof n === 'number' ? n.toLocaleString('ko-KR') : String(n === undefined ? '' : n); }
  function clip(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
  function last(arr) { return arr && arr.length ? arr[arr.length - 1] : null; }
  function isAgent(id) { return AGENT_IDS.indexOf(id) >= 0; }
  function isChannel(id) { return CHANNEL_IDS.indexOf(id) >= 0; }
  function chName(id) { return CHANNEL_NAMES[id] || String(id || ''); }
  function storageGet(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } }
  function storageSet(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* storage blocked */ } }
  function charsLabel(channel, d) {
    if (!d) return '';
    if (channel === 'bizplan' || channel === 'naver_blog') {
      return fmtNum(d.chars_no_space !== undefined ? d.chars_no_space : d.chars) + '자(공백 제외)';
    }
    return fmtNum(d.chars !== undefined ? d.chars : d.chars_no_space) + '자';
  }

  // ------------------------------------------------------------------ reducer
  function newChannel(id) {
    return { id: id, state: 'waiting', round: 0, score: null, passed: null, drafts: [], reviews: [], final: null, topIssue: null, error: '' };
  }

  /** Round the pipeline keeps as final: passed first, then score, then the later round (pipeline._best_index). */
  function bestRound(reviews) {
    var best = null;
    (reviews || []).forEach(function (r) {
      var k = [r.passed ? 1 : 0, Number(r.score) || 0, r.round || 0];
      if (!best || k[0] > best[0] || (k[0] === best[0] && (k[1] > best[1] || (k[1] === best[1] && k[2] > best[2])))) best = k;
    });
    return best ? best[2] : null;
  }
  function finalReview(ch) {
    if (!ch.final || typeof ch.final.round !== 'number') return null;
    return ch.reviews.filter(function (x) { return (x.round || 0) === ch.final.round; })[0] || null;
  }

  function initialState() {
    var agents = {};
    AGENT_IDS.forEach(function (id) { agents[id] = { status: 'idle', message: '', since: 0 }; });
    var channels = {};
    CHANNEL_IDS.forEach(function (id) { channels[id] = newChannel(id); });
    return {
      run: null,
      t: 0,
      agents: agents,
      plan: null,
      research: { queries: [], sources: [], findings: [], gaps: [], completed: 0, followups: 0, activeQuestion: null },
      channels: channels,
      channelOrder: CHANNEL_IDS.slice(),
      timeline: []
    };
  }

  /** Pure reducer: returns a new state with the event applied. */
  function applyEvent(prev, ev) {
    if (!ev || typeof ev !== 'object') return prev;
    var d = ev.data || {};
    var s = Object.assign({}, prev, { t: Math.max(prev.t || 0, Number(ev.t) || 0) });
    var passScore = (prev.run && prev.run.passScore) || 80;
    var maxRounds = prev.run && typeof prev.run.maxRounds === 'number' ? prev.run.maxRounds : 2;

    function log(entry) {
      s.timeline = (s.timeline || prev.timeline).concat([Object.assign({
        seq: ev.seq, t: Number(ev.t) || 0, agent: ev.agent || 'system', type: ev.type, weight: 'minor'
      }, entry)]);
    }
    function upd(id, fn) {
      if (!id) return;
      var cur = s.channels[id] || newChannel(id);
      var next = {};
      next[id] = Object.assign({}, cur, fn(cur));
      s.channels = Object.assign({}, s.channels, next);
      if (s.channelOrder.indexOf(id) < 0) s.channelOrder = s.channelOrder.concat([id]);
    }
    function setResearch(patch) { s.research = Object.assign({}, s.research, patch); }

    switch (ev.type) {
      case 'run.started': {
        var brief = d.brief || {};
        var chans = (Array.isArray(d.channels) && d.channels.length ? d.channels : (brief.channels || CHANNEL_IDS)).filter(isChannel);
        if (!chans.length) chans = CHANNEL_IDS.slice();
        var channels = {};
        chans.forEach(function (c) { channels[c] = newChannel(c); });
        s.channels = channels;
        s.channelOrder = chans;
        s.run = {
          id: ev.run_id || '', mode: d.mode || '', model: d.model || '', brief: brief, channels: chans,
          maxRounds: typeof d.max_rounds === 'number' ? d.max_rounds : 2,
          passScore: typeof d.pass_score === 'number' ? d.pass_score : 80,
          status: 'running', startedAt: ev.ts || '', duration: null, scores: null, passed: null, outputDir: '', error: ''
        };
        log({ weight: 'key', parts: ['실행 시작 · 채널 ', { b: chans.length + '개' }, ' · 통과 기준 ' + s.run.passScore + '점 · 최대 수정 ' + s.run.maxRounds + '회'] });
        break;
      }
      case 'agent.status': {
        if (!isAgent(ev.agent)) break;
        var st = d.status || 'idle';
        var ag = {};
        ag[ev.agent] = { status: st, message: d.message || '', since: Number(ev.t) || 0 };
        s.agents = Object.assign({}, prev.agents, ag);
        if (ev.agent === 'orchestrator' && st === 'writing') {
          s.channelOrder.forEach(function (c) {
            if (s.channels[c] && s.channels[c].state === 'waiting') upd(c, function () { return { state: 'drafting' }; });
          });
        }
        log({ parts: [{ b: AGENT_SHORT[ev.agent] }, ' · ' + (d.message || STATUS_LABEL[st] || st)], status: st });
        break;
      }
      case 'handoff': {
        var from = d.from || ev.agent, to = d.to || '';
        log({
          weight: 'key', tone: d.kind === 'feedback' ? 'warn' : '',
          parts: [{ b: (AGENT_SHORT[from] || from) + ' → ' + (AGENT_SHORT[to] || chName(to) || to) }, ' · ' + (d.label || '')]
        });
        break;
      }
      case 'plan.created': {
        s.plan = {
          summary: d.summary || '', key_messages: d.key_messages || [],
          questions: d.questions || [], outlines: d.outlines || []
        };
        log({ weight: 'key', parts: [{ b: '작업 계획 완성' }, ' · 리서치 질문 ' + s.plan.questions.length + '개 · 핵심 메시지 ' + s.plan.key_messages.length + '개'] });
        break;
      }
      case 'research.query': {
        setResearch({ queries: prev.research.queries.concat([{ question_id: d.question_id, query: d.query, t: ev.t }]), activeQuestion: d.question_id || null });
        log({ parts: ['검색 “' + (d.query || '') + '”'] });
        break;
      }
      case 'research.source': {
        var src = d.source;
        if (!src) break;
        var dupe = prev.research.sources.some(function (x) { return (src.id && x.id === src.id) || (src.url && x.url === src.url); });
        if (!dupe) setResearch({ sources: prev.research.sources.concat([src]) });
        log({ parts: ['출처 확보 ', { b: 'T' + (src.tier || '?') }, ' ' + (src.publisher ? src.publisher + ' — ' : '') + clip(src.title, 48)] });
        break;
      }
      case 'research.finding': {
        var f = d.finding;
        if (!f) break;
        if (!prev.research.findings.some(function (x) { return x.id === f.id; })) {
          setResearch({ findings: prev.research.findings.concat([f]) });
        }
        log({ parts: ['근거 ' + (f.id || '') + ' · ' + clip(f.claim, 64)] });
        break;
      }
      case 'research.completed': {
        var gaps = d.followup ? prev.research.gaps.concat((d.gaps || []).filter(function (g) { return prev.research.gaps.indexOf(g) < 0; })) : (d.gaps || []);
        setResearch({
          completed: prev.research.completed + 1,
          followups: prev.research.followups + (d.followup ? 1 : 0),
          gaps: gaps, activeQuestion: null
        });
        log({
          weight: 'key',
          parts: [{ b: d.followup ? '추가 리서치 완료' : '리서치 완료' }, ' · 근거 ' + s.research.findings.length + '개 · 출처 ' + s.research.sources.length + '개' + (gaps.length ? ' · 빈틈 ' + gaps.length + '개' : '')]
        });
        break;
      }
      case 'draft.created': {
        var ch = d.channel;
        if (!ch) break;
        upd(ch, function (c) {
          return { drafts: c.drafts.concat([d]), round: d.round || 0, state: 'queued' };
        });
        log({ weight: 'key', parts: [{ b: chName(ch) }, ' ' + (d.round ? '수정본' : '초안') + ' R' + (d.round || 0) + ' 완성 · ' + charsLabel(ch, d)] });
        break;
      }
      case 'review.started': {
        upd(d.channel, function () { return { state: 'reviewing', round: d.round || 0 }; });
        log({ parts: [{ b: chName(d.channel) }, ' R' + (d.round || 0) + ' 검수 시작'] });
        break;
      }
      case 'review.completed': {
        var r = d;
        upd(r.channel, function (c) {
          var reviews = c.reviews.filter(function (x) { return x.round !== r.round; }).concat([r]);
          var state = r.passed ? 'passed' : ((r.round || 0) < maxRounds ? 'revision' : 'failed');
          return { reviews: reviews, score: r.score, passed: !!r.passed, state: state, round: r.round || 0 };
        });
        log({
          weight: 'key', tone: r.passed ? 'pass' : 'warn',
          parts: [{ b: chName(r.channel) }, ' R' + (r.round || 0) + ' 검수 ', { b: r.score + '점' }, r.passed ? ' · 통과' : ' · 미통과 (기준 ' + passScore + '점)']
        });
        break;
      }
      case 'revision.requested': {
        upd(d.channel, function () { return { state: 'revision', topIssue: d.top_issue || null }; });
        log({ weight: 'key', tone: 'warn', parts: [{ b: chName(d.channel) }, ' 수정 요청 ' + (d.issues || 0) + '건 · ' + clip(d.top_issue, 60)] });
        break;
      }
      case 'channel.completed': {
        upd(d.channel, function (c) {
          // the final is the best round, not necessarily the last one
          var fr = typeof d.final_round === 'number' ? d.final_round : bestRound(c.reviews);
          if (fr === null) fr = c.round || 0;
          return {
            final: { title: d.title || '', content: d.content || '', hashtags: d.hashtags || [], score: d.score, passed: !!d.passed, rounds: d.rounds || 0, round: fr },
            state: d.passed ? 'passed' : 'failed', score: typeof d.score === 'number' ? d.score : null, passed: !!d.passed, round: fr, error: ''
          };
        });
        log({ weight: 'key', tone: d.passed ? 'pass' : 'warn', parts: [{ b: chName(d.channel) }, ' 최종본 확정 · ' + d.score + '점 · ' + (d.passed ? '통과' : '미통과') + (d.rounds ? ' · 수정 ' + d.rounds + '회' : '')] });
        break;
      }
      case 'run.completed': {
        var errs = d.errors && typeof d.errors === 'object' ? d.errors : {};
        s.run = Object.assign({}, prev.run || {}, {
          status: 'completed', duration: typeof d.duration_s === 'number' ? d.duration_s : Number(ev.t) || 0,
          scores: d.scores || {}, passed: d.passed || {}, outputDir: d.output_dir || '', errors: errs
        });
        // a channel that raised never gets channel.completed; don't leave it "in progress".
        // Item jobs (재검수) end with a review and no channel.completed: keep that verdict.
        s.channelOrder.forEach(function (c) {
          var cur = s.channels[c];
          if (!cur || cur.final) return;
          var lastReview = last(cur.reviews);
          if (!errs[c] && lastReview) {
            upd(c, function () { return { state: lastReview.passed ? 'passed' : 'failed', score: lastReview.score, passed: !!lastReview.passed }; });
          } else {
            upd(c, function () { return { state: 'error', error: String(errs[c] || '작업이 끝나지 않았어요') }; });
          }
        });
        var nErr = s.channelOrder.filter(function (c) { return s.channels[c] && s.channels[c].state === 'error'; }).length;
        log({
          weight: 'key', tone: nErr ? 'warn' : 'pass',
          parts: [{ b: nErr ? '작업 종료 · 채널 ' + nErr + '개 실패' : '모든 작업 완료' }, ' · ' + fmtDuration(s.run.duration) + (d.output_dir ? ' · ' + d.output_dir : '')]
        });
        break;
      }
      case 'run.failed': {
        var why = d.error || '원인을 알 수 없어요';
        s.run = Object.assign({}, prev.run || {}, { status: 'failed', error: why });
        // stop every agent that was still working or waiting; the pipeline sends no status reset
        var stopped = {};
        AGENT_IDS.forEach(function (id) {
          var a = prev.agents[id];
          if (a && ACTIVE_STATUSES[a.status]) stopped[id] = { status: 'error', message: why, since: Number(ev.t) || 0 };
          else if (a && a.status === 'waiting') stopped[id] = { status: 'idle', message: '실행이 중단됐어요', since: Number(ev.t) || 0 };
        });
        s.agents = Object.assign({}, prev.agents, stopped);
        s.channelOrder.forEach(function (c) {
          var cur = s.channels[c];
          if (cur && !cur.final && cur.state !== 'waiting') {
            upd(c, function () { return { state: 'error', error: '실행이 중단돼 최종본을 만들지 못했어요' }; });
          }
        });
        log({ weight: 'key', level: 'error', parts: [{ b: '실행 실패' }, ' · ' + why] });
        break;
      }
      case 'log': {
        var level = d.level || 'info';
        log({ weight: level === 'info' ? 'minor' : 'key', level: level, parts: [d.message || ''] });
        break;
      }
      default:
        return prev;
    }
    return s;
  }

  function currentPhase(s) {
    if (s.run && s.run.status === 'completed') return 'done';
    if (!s.plan) return 'plan';
    if (!s.research.completed) return 'research';
    var reviewing = s.channelOrder.some(function (c) {
      var ch = s.channels[c];
      return ch && (ch.reviews.length || ch.state === 'reviewing');
    });
    return reviewing ? 'review' : 'draft';
  }

  // ------------------------------------------------------------------ markdown (escape first)
  function mdEmphasis(t) {
    t = t.replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>');
    return t.replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\*)/g, '$1<em>$2</em>');
  }
  function mdInline(raw, ctx) {
    // \u0000 is reserved for link placeholders below
    var s = esc(String(raw === null || raw === undefined ? '' : raw).replace(/\u0000/g, ''));
    return s.split(/(`[^`]+`)/g).map(function (seg) {
      if (seg.length > 2 && seg.charAt(0) === '`' && seg.charAt(seg.length - 1) === '`') return '<code>' + seg.slice(1, -1) + '</code>';
      var t = seg;
      var links = [];
      t = t.replace(/\[이미지\s*[:：]\s*([^\]]+)\]/g, '<span class="md-img">이미지 · $1</span>');
      // finished anchors are parked behind placeholders so the [sN] and emphasis passes never touch their hrefs
      t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function (m, txt, url) {
        links.push('<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + mdEmphasis(txt) + '</a>');
        return '\u0000' + (links.length - 1) + '\u0000';
      });
      t = t.replace(/\[(s\d+)\]/g, function (m, id) {
        var src = ctx && ctx.sources && ctx.sources[id];
        if (src && /^https?:\/\//i.test(src.url || '')) {
          links.push('<a class="md-ref" href="' + esc(src.url) + '" target="_blank" rel="noopener noreferrer" title="' + esc((src.publisher ? src.publisher + ' · ' : '') + (src.title || '')) + '">' + id + '</a>');
          return '\u0000' + (links.length - 1) + '\u0000';
        }
        return '<span class="md-ref">' + id + '</span>';
      });
      t = mdEmphasis(t);
      return t.replace(/\u0000(\d+)\u0000/g, function (m, i) { return links[+i] || ''; });
    }).join('');
  }

  function renderMarkdown(src, ctx) {
    var lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
    var out = [];
    var i = 0;
    var reFence = /^\s*```/;
    var reHeading = /^(#{1,6})\s+(.*)$/;
    var reHr = /^\s*(-{3,}|\*{3,}|_{3,})\s*$/;
    var reQuote = /^\s*>/;
    var reUl = /^\s*[-*•]\s+/;
    var reOl = /^\s*(\d+)[.)]\s+/;
    function isSep(l) { return typeof l === 'string' && l.indexOf('-') >= 0 && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l); }
    function isTableStart(idx) { return lines[idx].trim().charAt(0) === '|' && isSep(lines[idx + 1]); }
    function row(l) {
      var t = l.trim();
      if (t.charAt(0) === '|') t = t.slice(1);
      if (t.charAt(t.length - 1) === '|') t = t.slice(0, -1);
      return t.split('|').map(function (c) { return c.trim(); });
    }
    function blockStart(idx) {
      var l = lines[idx];
      return reFence.test(l) || reHeading.test(l) || reHr.test(l) || reQuote.test(l) || reUl.test(l) || reOl.test(l) || isTableStart(idx);
    }
    while (i < lines.length) {
      var line = lines[i];
      var m;
      if (!line.trim()) { i++; continue; }
      if (reFence.test(line)) {
        var code = [];
        i++;
        while (i < lines.length && !reFence.test(lines[i])) code.push(lines[i++]);
        i++;
        out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>');
        continue;
      }
      if ((m = reHeading.exec(line))) {
        var lvl = m[1].length, tag = 'h' + Math.min(6, lvl + 2);
        out.push('<' + tag + ' class="md-h' + lvl + '">' + mdInline(m[2].replace(/\s+#+\s*$/, ''), ctx) + '</' + tag + '>');
        i++;
        continue;
      }
      if (reHr.test(line)) { out.push('<hr>'); i++; continue; }
      if (isTableStart(i)) {
        var head = row(line);
        i += 2;
        var rows = [];
        while (i < lines.length && lines[i].trim().charAt(0) === '|') rows.push(row(lines[i++]));
        out.push('<div class="md-table"><table><thead><tr>' + head.map(function (c) { return '<th>' + mdInline(c, ctx) + '</th>'; }).join('') +
          '</tr></thead><tbody>' + rows.map(function (r) {
            return '<tr>' + head.map(function (_, k) { return '<td>' + mdInline(r[k] || '', ctx) + '</td>'; }).join('') + '</tr>';
          }).join('') + '</tbody></table></div>');
        continue;
      }
      if (reQuote.test(line)) {
        var q = [];
        while (i < lines.length && reQuote.test(lines[i])) q.push(lines[i++].replace(/^\s*>\s?/, ''));
        out.push('<blockquote>' + q.map(function (x) { return mdInline(x, ctx); }).join('<br>') + '</blockquote>');
        continue;
      }
      if (reUl.test(line)) {
        var items = [];
        while (i < lines.length && reUl.test(lines[i])) items.push(lines[i++].replace(reUl, ''));
        out.push('<ul>' + items.map(function (x) { return '<li>' + mdInline(x, ctx) + '</li>'; }).join('') + '</ul>');
        continue;
      }
      if ((m = reOl.exec(line))) {
        var start = parseInt(m[1], 10), oitems = [];
        while (i < lines.length && reOl.test(lines[i])) oitems.push(lines[i++].replace(reOl, ''));
        out.push('<ol' + (start > 1 ? ' start="' + start + '"' : '') + '>' + oitems.map(function (x) { return '<li>' + mdInline(x, ctx) + '</li>'; }).join('') + '</ol>');
        continue;
      }
      var para = [];
      while (i < lines.length && lines[i].trim() && (para.length === 0 || !blockStart(i))) para.push(lines[i++]);
      out.push('<p>' + para.map(function (x) { return mdInline(x.trim(), ctx); }).join('<br>') + '</p>');
    }
    return out.join('\n');
  }

  // ------------------------------------------------------------------ runtime state
  var root = document.documentElement;
  var reduceMotion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  var manifest = DEFAULT_MANIFEST;
  var state = initialState();
  var rendered = null;          // last rendered state (for dirty checks)
  var dirty = true;
  var forceRender = true;
  var traceMeta = null;
  var sourceKind = 'none';      // sample | recorded | live | none
  var serverInfo = null;
  var player = { events: [], idx: 0, t: 0, duration: 0, speed: 2, playing: false, mode: 'replay', lastFrame: 0, liveArrival: 0 };
  var dom = { agents: {}, cards: {}, wires: {}, packets: [] };
  var drawerChannel = null, drawerTab = 'content', drawerReviewRound = null, drawerRef = null, lastTrigger = null;
  var liveSource = null;
  var liveEnd = null;           // onEnd callback of the run being streamed
  var liveRunId = '';
  var stageHidden = false;      // the studio view is not on screen (another tab of the app is open)
  var stageReady = false;

  function assetUrl(p) {
    if (!p || typeof p !== 'string') return null;
    if (/^(data:|blob:)/.test(p)) return p;
    if (/^[a-z]+:/i.test(p)) return null; // no external hosts
    return 'assets/' + p.replace(/^\.?\//, '');
  }

  function readEmbedded(id) {
    var node = document.getElementById(id);
    if (!node) return null;
    try { return JSON.parse(node.textContent); } catch (e) { return null; }
  }

  function fetchJson(url, opts) {
    return fetch(url, Object.assign({ cache: 'no-store', headers: { Accept: 'application/json' } }, opts || {})).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  function loadManifest() {
    var embedded = readEmbedded('insia-manifest');
    if (embedded && embedded.agents) return Promise.resolve(mergeManifest(embedded));
    return fetchJson('assets/manifest.json').then(mergeManifest).catch(function () { return DEFAULT_MANIFEST; });
  }
  function mergeManifest(m) {
    var out = { version: m.version || 1, credit: m.credit || '', hero: m.hero || {}, agents: {}, channels: {} };
    AGENT_IDS.forEach(function (id) { out.agents[id] = Object.assign({}, DEFAULT_MANIFEST.agents[id], (m.agents || {})[id] || {}); });
    CHANNEL_IDS.forEach(function (id) { out.channels[id] = Object.assign({}, DEFAULT_MANIFEST.channels[id], (m.channels || {})[id] || {}); });
    return out;
  }

  function validTrace(t) { return t && Array.isArray(t.events) && t.events.length > 0; }
  function traceKind(t) {
    var meta = (t && t.meta) || {};
    var title = String(meta.title || '');
    return meta.sample || /샘플|sample/i.test(title) ? 'sample' : 'recorded';
  }
  function loadTrace() {
    var embedded = readEmbedded('insia-trace');
    if (validTrace(embedded)) return Promise.resolve(embedded);
    return fetchJson('demo/demo-run.json').then(function (t) {
      if (!validTrace(t)) throw new Error('empty');
      return t;
    }).catch(function () {
      return fetchJson('demo/sample-trace.json').then(function (t) { return validTrace(t) ? t : null; });
    }).catch(function () { return null; });
  }

  /** Resolves to the health JSON, {authRequired: true} when the server wants a token (401), or null (demo). */
  function detectServer() {
    // file:// pages and the published artifact (trace embedded by build_artifact.py) never have the API
    if (!/^https?:$/.test(location.protocol) || document.getElementById('insia-trace')) return Promise.resolve(null);
    var ctrl = window.AbortController ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, 1500);
    return fetch('/api/health', { cache: 'no-store', credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: ctrl ? ctrl.signal : undefined })
      .then(function (r) {
        clearTimeout(timer);
        var ct = r.headers.get('content-type') || '';
        if (r.status === 401 && ct.indexOf('json') >= 0) return { authRequired: true };
        if (!r.ok || ct.indexOf('json') < 0) return null;
        return r.json().then(function (j) { return j && typeof j.mode === 'string' ? j : null; });
      })
      .catch(function () { clearTimeout(timer); return null; });
  }

  /** Switch the header between demo and live once the API answered (boot, or after login). */
  function setServer(info) {
    serverInfo = info && !info.authRequired ? info : null;
    var badge = $('modeBadge');
    if (serverInfo) {
      badge.textContent = 'LIVE';
      badge.dataset.mode = 'live';
      badge.title = '서버 연결됨 · ' + (serverInfo.mode || '') + (serverInfo.model ? ' · ' + serverInfo.model : '');
    } else {
      badge.textContent = info && info.authRequired ? 'LOCKED' : 'DEMO';
      badge.dataset.mode = info && info.authRequired ? 'locked' : 'demo';
      badge.title = info && info.authRequired ? '접근 토큰이 필요한 서버예요' : '데모 재생 (서버 없이 기록을 재생해요)';
    }
    $('btnNewRun').hidden = !serverInfo;
    summaryKey = '';
    dirty = true;
  }

  // ------------------------------------------------------------------ build DOM
  function buildStage() {
    var monitor = $('monitor');
    var studio = manifest.hero && assetUrl(manifest.hero.studio);
    if (studio) {
      var bg = el('img', { class: 'backdrop', src: studio, alt: '', 'aria-hidden': 'true', decoding: 'async' });
      bg.addEventListener('error', function () { bg.remove(); });
      monitor.insertBefore(bg, monitor.firstChild);
    }
    var field = $('stageField');
    field.textContent = '';
    AGENT_IDS.forEach(function (id) {
      var m = manifest.agents[id];
      var frame = el('div', { class: 'agent-frame' }, el('span', { class: 'agent-fallback', 'aria-hidden': 'true', text: (m.name || AGENT_SHORT[id]).charAt(0) }));
      var art = el('article', { class: 'agent', 'data-agent': id, 'data-status': 'idle' });
      var img = null, video = null;
      if (m.image) {
        img = el('img', { src: assetUrl(m.image), alt: m.name + ' ' + (m.nickname || '') + ' 캐릭터', decoding: 'async', draggable: 'false' });
        img.addEventListener('error', function () { img.remove(); });
        frame.appendChild(img);
      }
      if (m.video) {
        video = document.createElement('video');
        video.muted = true;
        video.defaultMuted = true;
        video.loop = true;
        video.playsInline = true;
        video.setAttribute('muted', '');
        video.setAttribute('playsinline', '');
        video.setAttribute('preload', 'none');
        video.setAttribute('aria-hidden', 'true');
        video.setAttribute('tabindex', '-1');
        video.disablePictureInPicture = true;
        if (m.image) video.poster = assetUrl(m.image);
        video.dataset.src = assetUrl(m.video) || '';
        video.addEventListener('playing', function () { if (art.classList.contains('is-active')) art.classList.add('is-video'); });
        video.addEventListener('error', function () { video.dataset.failed = '1'; art.classList.remove('is-video'); markBob(id); });
        frame.appendChild(video);
      }
      frame.appendChild(el('span', { class: 'agent-ring', 'aria-hidden': 'true' }));
      var tile = el('div', { class: 'agent-tile' }, frame);
      if (m.model && assetUrl(m.model)) {
        tile.appendChild(el('button', {
          type: 'button', class: 'btn-3d', title: m.name + ' 3D 모델 보기', 'aria-label': m.name + ' 3D로 보기',
          onclick: function (e) { openModel(id, e.currentTarget); }
        }, ['3D', el('span', { class: 'btn-3d-long', 'aria-hidden': 'true', text: '로 보기' })]));
      }
      var chip = el('span', { class: 'status-chip', text: '대기' });
      var meta = el('div', { class: 'agent-meta' }, [
        el('span', { class: 'agent-nick', text: m.nickname || '' }),
        el('div', { class: 'agent-line' }, [el('h2', { class: 'agent-name', text: m.name || AGENT_SHORT[id] }), chip])
      ]);
      var bubble = el('p', { class: 'bubble' });
      art.appendChild(tile);
      art.appendChild(meta);
      art.appendChild(bubble);
      field.appendChild(art);
      dom.agents[id] = { art: art, frame: frame, img: img, video: video, chip: chip, bubble: bubble, active: false, msg: null };
    });

    var rail = $('rail');
    rail.textContent = '';
    CHANNEL_IDS.forEach(function (id) {
      var m = manifest.channels[id];
      var icon;
      if (m.icon) {
        icon = el('img', { class: 'ch-icon', src: assetUrl(m.icon), alt: '', decoding: 'async' });
        icon.addEventListener('error', function () { icon.replaceWith(el('span', { class: 'ch-icon-fallback', 'aria-hidden': 'true', text: (m.name || chName(id)).charAt(0) })); });
      } else {
        icon = el('span', { class: 'ch-icon-fallback', 'aria-hidden': 'true', text: (m.name || chName(id)).charAt(0) });
      }
      var parts = {
        round: el('span', { class: 'ch-round', text: 'R0' }),
        state: el('span', { class: 'ch-state', text: '대기' }),
        num: el('span', { class: 'ch-score-num', text: '—' }),
        fill: el('span', { class: 'gauge-fill' }),
        pass: el('span', { class: 'gauge-pass', 'data-label': '80' }),
        chips: el('span', { class: 'ch-chips' }),
        history: el('span', { class: 'ch-history' })
      };
      parts.gauge = el('span', { class: 'gauge', 'aria-hidden': 'true' }, [parts.fill, parts.pass]);
      var card = el('button', {
        type: 'button', class: 'ch-card', 'data-channel': id, 'data-state': 'waiting',
        style: '--ch:' + (m.color || '#3B5BDB'),
        onclick: function (e) { openDrawer(id, e.currentTarget); }
      }, [
        el('span', { class: 'ch-head' }, [icon, el('span', { class: 'ch-name', text: m.name || chName(id) })]),
        el('span', { class: 'ch-status' }, [parts.state, parts.round]),
        el('span', { class: 'ch-score' }, [parts.num, parts.gauge]),
        parts.chips,
        parts.history
      ]);
      rail.appendChild(card);
      parts.card = card;
      dom.cards[id] = parts;
    });
    $('credit').textContent = manifest.credit || '';
    buildWires();
    stageReady = true;
  }

  function markBob(id) {
    var a = dom.agents[id];
    if (a) a.art.classList.toggle('is-bob', a.active && !reduceMotion.matches);
  }

  // ------------------------------------------------------------------ wires + packets
  var SVGNS = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs) {
    var n = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }
  function buildWires() {
    var w = $('wires');
    w.textContent = '';
    dom.wires = {};
    var keys = ['orchestrator|researcher', 'orchestrator|reviewer', 'researcher|reviewer'];
    CHANNEL_IDS.forEach(function (c) { keys.push('orchestrator|' + c); keys.push('reviewer|' + c); keys.push('researcher|' + c); });
    keys.forEach(function (k) {
      var target = k.split('|')[1];
      var cls = 'wire' + (isChannel(target) ? ' wire--ch' : '');
      if (isChannel(target) && k.indexOf('orchestrator') !== 0) cls += ' wire--ghost';
      var p = svg('path', { class: cls, d: 'M0 0' });
      w.appendChild(p);
      dom.wires[k] = { path: p, hot: 0 };
    });
  }

  function layoutWires() {
    if (!stageReady) return;
    var monitor = $('monitor');
    var mb = monitor.getBoundingClientRect();
    if (!mb.width) return;
    ['wires', 'packets'].forEach(function (id) { $(id).setAttribute('viewBox', '0 0 ' + mb.width + ' ' + mb.height); });
    function box(node) {
      var r = node.getBoundingClientRect();
      return { x: r.left - mb.left, y: r.top - mb.top, w: r.width, h: r.height, cx: r.left - mb.left + r.width / 2, cy: r.top - mb.top + r.height / 2, b: r.bottom - mb.top, r: r.right - mb.left };
    }
    var A = {};
    AGENT_IDS.forEach(function (id) { A[id] = box(dom.agents[id].frame); });
    function curve(p0, p1, bend) {
      var k = bend !== undefined ? bend : Math.max(24, Math.abs(p1.y - p0.y) * 0.55);
      return 'M' + p0.x.toFixed(1) + ' ' + p0.y.toFixed(1) + ' C' + p0.x.toFixed(1) + ' ' + (p0.y + k).toFixed(1) + ' ' +
        p1.x.toFixed(1) + ' ' + (p1.y - k).toFixed(1) + ' ' + p1.x.toFixed(1) + ' ' + p1.y.toFixed(1);
    }
    var o = A.orchestrator, rs = A.researcher, rv = A.reviewer;
    var oLeft = { x: o.x + o.w * 0.22, y: o.b - 4 }, oRight = { x: o.x + o.w * 0.78, y: o.b - 4 };
    set('orchestrator|researcher', curve(oLeft, { x: rs.cx, y: rs.y + 4 }));
    set('orchestrator|reviewer', curve(oRight, { x: rv.cx, y: rv.y + 4 }));
    var dy = Math.max(30, Math.abs(rv.cx - rs.cx) * 0.12);
    set('researcher|reviewer', 'M' + (rs.r - 4).toFixed(1) + ' ' + (rs.cy + rs.h * 0.18).toFixed(1) + ' C' + (rs.r + 60).toFixed(1) + ' ' + (rs.cy + rs.h * 0.18 + dy).toFixed(1) + ' ' +
      (rv.x - 60).toFixed(1) + ' ' + (rv.cy + rv.h * 0.18 + dy).toFixed(1) + ' ' + (rv.x + 4).toFixed(1) + ' ' + (rv.cy + rv.h * 0.18).toFixed(1));
    CHANNEL_IDS.forEach(function (c) {
      var card = dom.cards[c].card;
      if (card.hidden) {
        // channel not in this run: drop the boot-time geometry so no wire points at an empty slot
        ['orchestrator|', 'reviewer|', 'researcher|'].forEach(function (pre) { set(pre + c, 'M0 0'); });
        return;
      }
      var cb = box(card);
      var top = { x: cb.cx, y: cb.y + 2 };
      set('orchestrator|' + c, curve({ x: o.cx, y: o.b - 4 }, top));
      set('reviewer|' + c, curve({ x: rv.cx, y: rv.b - 4 }, top, 40));
      set('researcher|' + c, curve({ x: rs.cx, y: rs.b - 4 }, top, 40));
    });
    function set(k, d) { if (dom.wires[k]) dom.wires[k].path.setAttribute('d', d); }
  }

  var layoutQueued = false;
  function queueLayout() {
    if (layoutQueued) return;
    layoutQueued = true;
    requestAnimationFrame(function () { layoutQueued = false; layoutWires(); });
  }

  function agentColor(id) {
    var m = manifest.agents[id];
    return (m && m.color) || '#8C9BBD';
  }

  function launchPacket(from, to, label, color, onArrive) {
    if (reduceMotion.matches) { if (onArrive) onArrive(); return; }
    var key, reverse = false;
    if (dom.wires[from + '|' + to]) key = from + '|' + to;
    else if (dom.wires[to + '|' + from]) { key = to + '|' + from; reverse = true; }
    if (!key) { if (onArrive) onArrive(); return; }
    var wire = dom.wires[key];
    var path = wire.path;
    var len = 0;
    try { len = path.getTotalLength(); } catch (e) { len = 0; }
    if (!len) { if (onArrive) onArrive(); return; }
    var layer = $('packets');
    var g = svg('g', { class: 'packet' });
    var halo = svg('circle', { r: '11', fill: color, opacity: '0.22' });
    var dot = svg('circle', { r: '5', fill: color, class: 'packet-dot', style: 'color:' + color });
    g.appendChild(halo);
    g.appendChild(dot);
    var chip = null, chipW = 0;
    if (label) {
      chip = svg('g', { class: 'packet-chip' });
      var rect = svg('rect', { rx: '6', ry: '6', height: '22', stroke: color });
      var text = svg('text', { x: '9', y: '15' });
      text.textContent = clip(label, 22);
      chip.appendChild(rect);
      chip.appendChild(text);
      g.appendChild(chip);
      layer.appendChild(g);
      try { chipW = text.getComputedTextLength() + 18; } catch (e) { chipW = text.textContent.length * 11 + 18; }
      rect.setAttribute('width', chipW.toFixed(1));
    } else {
      layer.appendChild(g);
    }
    wire.hot++;
    path.classList.add('is-hot');
    path.style.setProperty('--hot', color);
    var vb = layer.viewBox && layer.viewBox.baseVal;
    var W = vb && vb.width ? vb.width : 1000;
    var duration = (player.mode === 'live' ? 1400 : 1500 / Math.sqrt(player.speed || 1));
    dom.packets.push({
      g: g, chip: chip, chipW: chipW, path: path, len: len, reverse: reverse, start: performance.now(), duration: duration, wire: wire, W: W,
      done: onArrive
    });
  }

  function animatePackets(now) {
    if (!dom.packets.length) return;
    dom.packets = dom.packets.filter(function (p) {
      var u = Math.min(1, (now - p.start) / p.duration);
      var e = u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2;
      var at = (p.reverse ? 1 - e : e) * p.len;
      var pt;
      try { pt = p.path.getPointAtLength(at); } catch (err) { pt = { x: 0, y: 0 }; }
      p.g.setAttribute('transform', 'translate(' + pt.x.toFixed(1) + ' ' + pt.y.toFixed(1) + ')');
      if (p.chip) {
        var cx = Math.max(4 - pt.x, Math.min(p.W - pt.x - p.chipW - 4, 10));
        p.chip.setAttribute('transform', 'translate(' + cx.toFixed(1) + ' -32)');
      }
      p.g.style.opacity = u > 0.9 ? String(1 - (u - 0.9) * 10) : '1';
      if (u >= 1) {
        p.g.remove();
        p.wire.hot = Math.max(0, p.wire.hot - 1);
        if (!p.wire.hot) p.path.classList.remove('is-hot');
        if (p.done) p.done();
        return false;
      }
      return true;
    });
  }

  function clearPackets() {
    dom.packets.forEach(function (p) { p.g.remove(); p.wire.hot = 0; p.path.classList.remove('is-hot'); });
    dom.packets = [];
  }

  function flashAgent(id) {
    var a = dom.agents[id];
    if (!a) return;
    a.art.classList.remove('is-receiving');
    void a.art.offsetWidth;
    a.art.classList.add('is-receiving');
    setTimeout(function () { a.art.classList.remove('is-receiving'); }, 700);
  }
  function flashCard(id, color) {
    var c = dom.cards[id];
    if (!c) return;
    c.card.style.setProperty('--hot', color);
    c.card.classList.remove('is-receiving');
    void c.card.offsetWidth;
    c.card.classList.add('is-receiving');
    setTimeout(function () { c.card.classList.remove('is-receiving'); }, 800);
  }

  /** Side effects for an event applied in real time. */
  function effects(ev) {
    var d = ev.data || {};
    switch (ev.type) {
      case 'handoff': {
        var from = d.from || ev.agent, to = d.to;
        var color = agentColor(from);
        if (isAgent(to)) launchPacket(from, to, d.label, color, function () { flashAgent(to); });
        else if (isChannel(to)) launchPacket(from, to, d.label, color, function () { flashCard(to, color); });
        break;
      }
      case 'draft.created':
        launchPacket('orchestrator', d.channel, (d.round ? '수정본 R' : '초안 R') + (d.round || 0), agentColor('orchestrator'), function () { flashCard(d.channel, agentColor('orchestrator')); });
        break;
      case 'review.completed': {
        var c = d.passed ? '#F5C451' : agentColor('reviewer');
        launchPacket('reviewer', d.channel, d.score + '점', c, function () { flashCard(d.channel, c); });
        break;
      }
      case 'channel.completed':
        launchPacket('orchestrator', d.channel, '최종본', d.passed ? '#F5C451' : '#F2616D', function () { flashCard(d.channel, d.passed ? '#F5C451' : '#F2616D'); });
        break;
      case 'run.completed': {
        var failedCh = d.errors && typeof d.errors === 'object' ? Object.keys(d.errors) : [];
        $('srStatus').textContent = failedCh.length
          ? '작업이 끝났어요. 실패한 채널: ' + failedCh.map(chName).join(', ') + '.'
          : '모든 채널 작업이 끝났어요.';
        break;
      }
      case 'run.failed':
        $('srStatus').textContent = '실행이 실패했어요: ' + (d.error || '원인을 알 수 없어요');
        break;
      default:
        break;
    }
  }

  // ------------------------------------------------------------------ player
  function safeApply(ev) {
    try { state = applyEvent(state, ev); } catch (err) { if (window.console) console.warn('이벤트 처리 실패', ev, err); }
  }

  function advanceTo(target, withFx) {
    var fxBudget = 10;
    while (player.idx < player.events.length && (Number(player.events[player.idx].t) || 0) <= target + 1e-6) {
      var ev = player.events[player.idx++];
      safeApply(ev);
      if (withFx && fxBudget-- > 0) effects(ev);
      dirty = true;
    }
  }

  function seek(t) {
    t = Math.max(0, Math.min(player.duration, t));
    state = initialState();
    player.idx = 0;
    player.t = t;
    clearPackets();
    advanceTo(t, false);
    dirty = true;
    forceRender = true;
  }

  function loadEvents(events) {
    var evs = events.slice().filter(function (e) { return e && typeof e === 'object' && e.type; });
    evs.forEach(function (e, i) { if (typeof e.t !== 'number') e.t = Number(e.t) || 0; if (typeof e.seq !== 'number') e.seq = i + 1; });
    evs.sort(function (a, b) { return a.t - b.t || a.seq - b.seq; });
    player.events = evs;
    player.duration = evs.length ? evs[evs.length - 1].t : 0;
    player.mode = 'replay';
    seek(0);
  }

  function play() {
    if (player.mode !== 'replay' || !player.events.length) return;
    if (player.t >= player.duration) seek(0);
    player.playing = true;
    player.lastFrame = performance.now();
    dirty = true;
  }
  function pause() { player.playing = false; dirty = true; }

  function frame(now) {
    requestAnimationFrame(frame);
    if (!stageReady) return;
    if (player.mode === 'replay' && player.playing) {
      var dt = Math.min(0.25, Math.max(0, (now - player.lastFrame) / 1000));
      player.t = Math.min(player.duration, player.t + dt * player.speed);
      advanceTo(player.t, true);
      if (player.t >= player.duration) { player.playing = false; dirty = true; }
    }
    player.lastFrame = now;
    animatePackets(now);
    if (dirty) {
      dirty = false;
      try { render(forceRender); } catch (err) { if (window.console) console.error('렌더링 오류', err); }
      forceRender = false;
    }
    renderTransport(now);
  }

  // ------------------------------------------------------------------ live mode
  function startLiveRun(body) {
    // same-origin relative URL with an explicit JSON content type (the server rejects anything else on /api)
    return fetch('/api/runs', {
      method: 'POST', mode: 'same-origin', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok || !j.run_id) throw new Error(j.error || j.detail || ('서버 응답 ' + r.status));
        return j.run_id;
      });
    }).then(enterLive);
  }

  /**
   * Stream a server run into the stage. opts.onEnd(state, lastEvent) fires once when the run
   * completes, fails or is lost; opts.title replaces the summary tag's run label.
   */
  function enterLive(runId, opts) {
    opts = opts || {};
    if (liveSource) { try { liveSource.close(); } catch (e) { /* ignore */ } }
    var prevEnd = liveEnd;
    liveEnd = null;
    if (prevEnd) { try { prevEnd(null, { type: 'replaced' }); } catch (e) { /* ignore */ } }
    clearPackets();
    player.mode = 'live';
    player.playing = false;
    player.events = [];
    player.idx = 0;
    player.t = 0;
    player.duration = 0;
    player.liveArrival = performance.now();
    state = initialState();
    sourceKind = 'live';
    traceMeta = { title: opts.title || ('실시간 실행 ' + runId) };
    liveRunId = runId;
    forceRender = true;
    dirty = true;
    var seen = {};
    var warned = false;
    var es = new EventSource('/api/runs/' + encodeURIComponent(runId) + '/events');
    liveSource = es;
    liveEnd = typeof opts.onEnd === 'function' ? opts.onEnd : null;
    function finish(ev) {
      var cb = liveSource === null && liveEnd ? liveEnd : null;
      liveEnd = null;
      if (cb) { try { cb(state, ev); } catch (err) { if (window.console) console.warn('작업 종료 처리 실패', err); } }
    }
    es.onmessage = function (msg) {
      var ev;
      try { ev = JSON.parse(msg.data); } catch (e) { return; }
      if (!ev || !ev.type) return;
      if (typeof ev.seq === 'number') { if (seen[ev.seq]) return; seen[ev.seq] = 1; }
      warned = false;
      ev.t = Number(ev.t) || 0;
      player.events.push(ev);
      player.idx = player.events.length;
      player.t = Math.max(player.t, ev.t);
      player.duration = player.t;
      player.liveArrival = performance.now();
      safeApply(ev);
      effects(ev);
      dirty = true;
      if (ev.type === 'run.completed' || ev.type === 'run.failed') {
        es.close();
        liveSource = null;
        player.mode = 'replay';
        player.playing = false;
        dirty = true;
        finish(ev);
      }
    };
    es.onerror = function () {
      if (liveSource !== es) return;
      if (es.readyState === 2) {
        // EventSource.CLOSED: the browser gave up (e.g. 404 after a server restart) and will not reconnect
        es.close();
        liveSource = null;
        player.mode = 'replay';
        player.playing = false;
        var lost = { seq: -1, t: player.t, type: 'run.failed', agent: 'system', data: { error: '서버에서 이 실행을 더 이상 찾을 수 없어요. 새 실행을 시작해 주세요.' } };
        player.events.push(lost);
        player.idx = player.events.length;
        safeApply(lost);
        effects(lost);
        dirty = true;
        finish(lost);
        return;
      }
      if (!warned) {
        warned = true;
        state = applyEvent(state, { seq: -1, t: player.t, type: 'log', agent: 'system', data: { level: 'warn', message: '서버 연결이 잠시 끊겼어요. 다시 연결하는 중이에요.' } });
        dirty = true;
      }
    };
  }

  // ------------------------------------------------------------------ render
  function render(force) {
    var s = state, p = rendered;
    renderAgents(s, p, force);
    if (force || !p || s.channels !== p.channels || s.run !== p.run || s.channelOrder !== p.channelOrder) renderRail(s, p, force);
    if (force || !p || s.plan !== p.plan) renderPlan(s);
    if (force || !p || s.research !== p.research || s.plan !== p.plan || s.agents.researcher !== p.agents.researcher) renderResearch(s, force);
    if (force || !p || s.timeline !== p.timeline) renderTimeline(s, force);
    renderSummary(s);
    renderPhase(s);
    if (drawerChannel && $('drawer').open && s.channels[drawerChannel] !== drawerRef) renderDrawer(true);
    rendered = s;
  }

  function renderAgents(s, p, force) {
    AGENT_IDS.forEach(function (id) {
      var a = s.agents[id];
      if (!force && p && p.agents[id] === a) return;
      var d = dom.agents[id];
      var active = !!ACTIVE_STATUSES[a.status];
      d.art.dataset.status = a.status;
      d.art.classList.toggle('is-active', active);
      d.art.classList.toggle('is-done', a.status === 'done');
      d.art.classList.toggle('is-error', a.status === 'error');
      d.chip.textContent = STATUS_LABEL[a.status] || a.status;
      if (d.msg !== a.message) {
        d.bubble.textContent = a.message || '';
        if (!force && a.message) {
          d.bubble.classList.remove('is-pop');
          void d.bubble.offsetWidth;
          d.bubble.classList.add('is-pop');
        }
        d.msg = a.message;
      }
      d.art.setAttribute('aria-label', (manifest.agents[id].name || AGENT_SHORT[id]) + ', ' + (STATUS_LABEL[a.status] || a.status) + (a.message ? ': ' + a.message : ''));
      d.active = active;
      setAgentMotion(id, active);
    });
  }

  function setAgentMotion(id, on) {
    var d = dom.agents[id];
    var v = d.video;
    var canVideo = v && !v.dataset.failed && v.dataset.src && !reduceMotion.matches && !document.hidden && !stageHidden;
    if (on && canVideo) {
      if (!v.getAttribute('src')) v.src = v.dataset.src;
      if (v.paused) {
        var pr = v.play();
        if (pr && pr.catch) pr.catch(function () { markBob(id); });
      } else {
        d.art.classList.add('is-video');
      }
      d.art.classList.remove('is-bob');
    } else {
      if (v && !v.paused) v.pause();
      d.art.classList.remove('is-video');
      d.art.classList.toggle('is-bob', on && !reduceMotion.matches && !(v && !v.dataset.failed && v.dataset.src));
    }
  }

  function formatChips(channel, ch) {
    // once final, show the checks of the round that became the final, not of the last round
    var rev = finalReview(ch) || last(ch.reviews);
    var draft = last(ch.drafts);
    var out = [];
    if (rev && rev.format_checks && rev.format_checks.length) {
      var checks = rev.format_checks;
      var psst = checks.filter(function (c) { return /^psst/.test(c.id); });
      var prio = ['length', 'hook_length', 'hashtags', 'tags', 'slides', 'caption_length', 'no_link', 'headings', 'images', 'title_length', 'title_keyword'];
      var others = checks.filter(function (c) { return !/^psst/.test(c.id); }).sort(function (a, b) {
        return (a.passed - b.passed) || (prio.indexOf(a.id) - prio.indexOf(b.id));
      });
      if (psst.length) {
        var ok = psst.filter(function (c) { return c.passed; }).length;
        out.push({ text: 'PSST ' + ok + '/' + psst.length, ok: ok === psst.length, title: 'PSST 섹션 ' + ok + '/' + psst.length });
      }
      others.forEach(function (c) { out.push({ text: chipText(c), ok: !!c.passed, title: c.label + ': ' + c.value + ' (기준 ' + c.expected + ')' }); });
      if (psst.length) {
        // keep length first for bizplan
        out.sort(function (a, b) { return (a.ok - b.ok); });
      }
      return out.slice(0, 3);
    }
    if (draft) {
      out.push({ text: charsLabel(channel, draft).replace('(공백 제외)', ''), ok: null, title: '초안 분량' });
      if (draft.hashtags && draft.hashtags.length) out.push({ text: (channel === 'naver_blog' ? '태그 ' : '해시태그 ') + draft.hashtags.length + '개', ok: null });
    }
    return out;
  }
  function chipText(c) {
    var max = /~\s*(\d+)/.exec(c.expected || '');
    var n = /(\d+)/.exec(c.value || '');
    switch (c.id) {
      case 'length': return c.value;
      case 'hashtags': return '해시태그 ' + (n ? n[1] : c.value) + (max ? '/' + max[1] : '');
      case 'tags': return '태그 ' + (n ? n[1] : c.value) + (max ? '/' + max[1] : '');
      case 'hook_length': return (/첫 줄/.test(c.label) ? '첫 줄 ' : '첫 2줄 ') + c.value;
      case 'no_link': return c.passed ? '본문 링크 없음' : '본문 링크';
      case 'slides': return '슬라이드 ' + c.value;
      case 'caption_length': return '캡션 ' + c.value;
      case 'headings': return '소제목 ' + c.value;
      case 'images': return '이미지 ' + c.value;
      case 'title_length': return '제목 ' + c.value;
      case 'title_keyword': return c.passed ? '제목 키워드 포함' : '제목 키워드 없음';
      default: return c.label + ' ' + c.value;
    }
  }

  function renderRail(s, p, force) {
    var passScore = (s.run && s.run.passScore) || 80;
    var visible = CHANNEL_IDS.filter(function (id) { return s.channelOrder.indexOf(id) >= 0; }).length || 4;
    $('rail').style.setProperty('--n', String(visible));
    CHANNEL_IDS.forEach(function (id) {
      var parts = dom.cards[id];
      var inRun = s.channelOrder.indexOf(id) >= 0;
      if (parts.card.hidden === inRun) { parts.card.hidden = !inRun; queueLayout(); }
      if (!inRun) return;
      var ch = s.channels[id];
      if (!force && p && p.channels[id] === ch && p.run === s.run) return;
      parts.card.dataset.state = ch.state;
      parts.state.textContent = CH_STATE_LABEL[ch.state] || ch.state;
      parts.round.textContent = 'R' + (ch.round || 0);
      parts.round.title = ch.final
        ? '최종본은 R' + (ch.round || 0) + (ch.final.rounds ? ' (수정 ' + ch.final.rounds + '회 중 가장 좋은 라운드)' : ' (첫 초안)')
        : '라운드 ' + (ch.round || 0) + (ch.round ? ' (수정 ' + ch.round + '회)' : ' (첫 초안)');
      if (typeof ch.score === 'number') {
        parts.num.textContent = String(ch.score);
        parts.fill.style.width = Math.max(0, Math.min(100, ch.score)) + '%';
        parts.gauge.dataset.pass = String(ch.score >= passScore);
      } else {
        parts.num.textContent = '—';
        parts.fill.style.width = '0%';
        parts.gauge.dataset.pass = '';
      }
      parts.pass.style.left = 'calc(' + passScore + '% - 1px)';
      parts.pass.setAttribute('data-label', String(passScore));
      parts.chips.textContent = '';
      if (ch.state === 'error') {
        parts.chips.appendChild(el('span', { class: 'ch-error', title: ch.error || null, text: ch.error || '작업이 실패했어요' }));
      } else {
        formatChips(id, ch).forEach(function (c) {
          parts.chips.appendChild(el('span', { class: 'chip', 'data-ok': c.ok === null ? null : String(c.ok), title: c.title || null, text: c.text }));
        });
      }
      parts.history.textContent = ch.reviews.length ? ch.reviews.map(function (r) { return 'R' + r.round + ' ' + r.score; }).join(' → ') : (ch.drafts.length && ch.state !== 'error' ? '검수 전' : '');
      var name = (manifest.channels[id] && manifest.channels[id].name) || chName(id);
      parts.card.setAttribute('aria-label', name + ', ' + (CH_STATE_LABEL[ch.state] || '') + (ch.state === 'error' && ch.error ? ': ' + ch.error : '') + (typeof ch.score === 'number' ? ', ' + ch.score + '점' : '') + ', 자세히 보기');
    });
  }

  function renderPlan(s) {
    var body = $('planBody');
    body.textContent = '';
    if (!s.plan) {
      $('planCount').textContent = '대기';
      body.appendChild(el('p', { class: 'empty', text: '총괄 에이전트가 브리프를 읽고 계획을 세우면 여기에 정리돼요.' }));
      return;
    }
    $('planCount').textContent = '메시지 ' + s.plan.key_messages.length + ' · 질문 ' + s.plan.questions.length;
    if (s.plan.summary) body.appendChild(el('p', { class: 'plan-summary', text: s.plan.summary }));
    if (s.plan.key_messages.length) {
      body.appendChild(el('div', null, [
        el('h3', { class: 'sub-title', text: '핵심 메시지' }),
        el('ul', { class: 'key-msgs' }, s.plan.key_messages.map(function (k) { return el('li', { text: k }); }))
      ]));
    }
  }

  var renderedSourceIds = {};
  function renderResearch(s, force) {
    var r = s.research;
    var body = $('researchBody');
    var researcher = s.agents.researcher;
    var active = !!ACTIVE_STATUSES[researcher.status];
    $('researchState').textContent = active ? (STATUS_LABEL[researcher.status] || '진행 중') : researcher.status === 'error' ? '중단' : (r.completed ? '완료' + (r.followups ? ' · 추가 ' + r.followups + '회' : '') : (s.plan ? '준비' : '대기'));
    // the board is rebuilt on every research event; remember which source link had keyboard focus
    var ae = document.activeElement;
    var focusKey = ae && ae !== body && body.contains(ae) && ae.getAttribute('data-src') || null;
    try {
      buildResearch(s, r, body, active, force);
    } finally {
      if (focusKey && document.activeElement !== ae) {
        var links = body.querySelectorAll('a[data-src]');
        for (var i = 0; i < links.length; i++) {
          if (links[i].getAttribute('data-src') === focusKey) { links[i].focus({ preventScroll: true }); break; }
        }
      }
    }
  }
  function buildResearch(s, r, body, active, force) {
    body.textContent = '';
    if (!s.plan && !r.sources.length && !r.queries.length) {
      body.appendChild(el('p', { class: 'empty', text: '리서치 에이전트가 찾은 출처와 근거가 여기에 쌓여요.' }));
      renderedSourceIds = {};
      return;
    }
    body.appendChild(el('div', { class: 'stat-row' }, [
      stat('출처', r.sources.length), stat('근거', r.findings.length), stat('빈틈', r.gaps.length)
    ]));

    var tiers = { 1: 0, 2: 0, 3: 0 };
    r.sources.forEach(function (x) { tiers[x.tier] = (tiers[x.tier] || 0) + 1; });
    if (r.sources.length) {
      body.appendChild(el('div', null, [
        el('div', { class: 'tierbar', role: 'img', 'aria-label': '출처 신뢰도 분포: Tier 1 ' + tiers[1] + '개, Tier 2 ' + tiers[2] + '개, Tier 3 ' + tiers[3] + '개' }, [
          el('span', { class: 't1', style: 'flex-grow:' + tiers[1] }),
          el('span', { class: 't2', style: 'flex-grow:' + tiers[2] }),
          el('span', { class: 't3', style: 'flex-grow:' + tiers[3] })
        ]),
        el('p', { class: 'tier-legend' }, [
          el('span', null, [el('i', { class: 't1' }), 'Tier 1 공식·공공 ' + tiers[1]]),
          el('span', null, [el('i', { class: 't2' }), 'Tier 2 언론·리서치 ' + tiers[2]]),
          el('span', null, [el('i', { class: 't3' }), 'Tier 3 기타 ' + tiers[3]])
        ])
      ]));
    }

    if (s.plan && s.plan.questions.length) {
      var counts = {};
      r.findings.forEach(function (f) { counts[f.question_id] = (counts[f.question_id] || 0) + 1; });
      body.appendChild(el('div', null, [
        el('h3', { class: 'sub-title', text: '리서치 질문' }),
        el('ol', { class: 'questions' }, s.plan.questions.map(function (q) {
          return el('li', { 'data-active': String(active && r.activeQuestion === q.id) }, [
            el('span', { class: 'q-id', text: q.id }),
            el('span', { class: 'q-text', text: q.question }),
            el('span', { class: 'q-count', text: '근거 ' + (counts[q.id] || 0), title: '이 질문에 연결된 근거 수' })
          ]);
        }))
      ]));
    }

    if (r.sources.length) {
      var fresh = {};
      body.appendChild(el('div', null, [
        el('h3', { class: 'sub-title', text: '출처' }),
        el('ul', { class: 'sources' }, r.sources.map(function (src) {
          var isNew = !force && !renderedSourceIds[src.id || src.url];
          fresh[src.id || src.url] = 1;
          var safeUrl = /^https?:\/\//i.test(src.url || '') ? src.url : null;
          return el('li', { class: 'source' + (isNew ? ' is-new' : '') }, [
            el('span', { class: 'tier-badge', 'data-tier': String(src.tier || 3), title: TIER_LABEL[src.tier] || '', text: 'T' + (src.tier || '?') }),
            safeUrl ? el('a', { href: safeUrl, target: '_blank', rel: 'noopener noreferrer', 'data-src': src.id || src.url, text: src.title || safeUrl }) : el('span', { text: src.title || '' }),
            el('span', { class: 'source-meta' }, [
              el('span', { class: 'source-id', text: src.id || '' }), ' · ',
              (TIER_LABEL[src.tier] || '') + (src.publisher ? ' · ' + src.publisher : '') + (src.published ? ' · ' + src.published : '')
            ])
          ]);
        }))
      ]));
      renderedSourceIds = fresh;
    } else {
      renderedSourceIds = {};
    }

    if (r.gaps.length) {
      body.appendChild(el('div', null, [
        el('h3', { class: 'sub-title', text: '빈틈 · 추가 확인 필요' }),
        el('ul', { class: 'gaps' }, r.gaps.map(function (g) { return el('li', { text: g }); }))
      ]));
    }
  }
  function stat(label, value) {
    return el('div', { class: 'stat' }, [el('span', { class: 'stat-label', text: label }), el('span', { class: 'stat-value', text: fmtNum(value) })]);
  }

  var timelineCount = 0;
  var logStick = true, logProgrammatic = false;
  function scrollLogToEnd(list) {
    logProgrammatic = true;
    list.scrollTop = list.scrollHeight;
  }
  function renderTimeline(s, force) {
    var list = $('timeline');
    var items = s.timeline;
    var nearBottom = logStick;
    if (force || items.length < timelineCount || (timelineCount && list.children.length && list.firstElementChild.dataset.seq !== String(items[0] && items[0].seq))) {
      list.setAttribute('aria-live', 'off');
      list.textContent = '';
      timelineCount = 0;
      nearBottom = true;
      logStick = true;
      setTimeout(function () { list.setAttribute('aria-live', 'polite'); }, 50);
    }
    if (!items.length) {
      if (!list.children.length) list.appendChild(el('li', { class: 'log-empty', text: '이벤트가 들어오면 시간순으로 기록돼요.' }));
      return;
    }
    if (timelineCount === 0) list.textContent = '';
    var frag = document.createDocumentFragment();
    for (var i = timelineCount; i < items.length; i++) frag.appendChild(timelineItem(items[i], !force));
    list.appendChild(frag);
    timelineCount = items.length;
    if (nearBottom) scrollLogToEnd(list);
  }
  function timelineItem(it, animate) {
    var text = el('span', { class: 'log-text' });
    (it.parts || []).forEach(function (part) {
      if (part && typeof part === 'object') text.appendChild(el('b', { text: part.b }));
      else text.appendChild(document.createTextNode(String(part)));
    });
    return el('li', {
      class: animate ? 'is-new' : null, 'data-seq': String(it.seq), 'data-agent': it.agent, 'data-weight': it.weight || 'minor',
      'data-level': it.level || null, 'data-tone': it.tone || null
    }, [
      el('span', { class: 'log-t', text: fmtClock(it.t) }),
      el('span', { class: 'log-dot', 'aria-hidden': 'true' }),
      el('span', { class: 'sr-only', text: AGENT_SHORT[it.agent] || '' }),
      text
    ]);
  }

  var summaryKey = '';
  function renderSummary(s) {
    var brief = (s.run && s.run.brief) || (traceMeta && traceMeta.brief) || {};
    var run = s.run || {};
    var chans = s.channelOrder;
    var doneCount = chans.filter(function (c) { return s.channels[c] && s.channels[c].final; }).length;
    var errCount = chans.filter(function (c) { return s.channels[c] && s.channels[c].state === 'error'; }).length;
    var key = [sourceKind, brief.topic, run.status, run.error, run.model, run.mode, doneCount, errCount, s.research.sources.length, s.research.findings.length, chans.join(','), serverInfo ? 1 : 0, run.duration].join('|');
    if (key === summaryKey) return;
    summaryKey = key;

    var tag = $('sourceTag');
    var tagText = { sample: '샘플 트레이스 · 예시 데이터', recorded: '기록된 실행 재생', live: '실시간 실행', none: '기록 없음' }[sourceKind] || '';
    tag.textContent = tagText;
    tag.dataset.kind = sourceKind;
    $('summaryTopic').textContent = brief.topic || (sourceKind === 'none' ? '재생할 기록을 찾지 못했어요' : '브리프를 기다리는 중이에요');
    var meta = $('summaryMeta');
    meta.textContent = '';
    if (sourceKind === 'none') {
      meta.textContent = 'web/demo/demo-run.json 또는 sample-trace.json이 필요해요. 파일로 직접 열었다면 python3 -m http.server로 web 폴더를 띄워 주세요.';
    } else {
      var bits = [];
      bits.push(['채널', chans.map(chName).join(' · ')]);
      if (run.passScore) bits.push(['통과 기준', run.passScore + '점']);
      if (run.mode) bits.push(['모드', run.mode === 'mock' ? '모의 실행' : run.mode === 'live' ? '실제 API' : run.mode]);
      if (run.model) bits.push(['모델', run.model]);
      bits.forEach(function (b, i) {
        if (i) meta.appendChild(document.createTextNode('  ·  '));
        meta.appendChild(document.createTextNode(b[0] + ' '));
        meta.appendChild(el('b', { text: b[1] }));
      });
    }
    // the call-to-action only matters before the first live run; afterwards the header's 새 실행 button is enough
    $('summaryLive').hidden = !serverInfo || sourceKind === 'live';

    var prog = $('summaryProgress');
    prog.textContent = '';
    var complete = run.status === 'completed';
    prog.appendChild(stat(complete ? '총 소요' : '완료 채널', complete ? fmtDuration(run.duration) : doneCount + ' / ' + chans.length));
    prog.appendChild(stat('출처', s.research.sources.length + '개'));
    prog.appendChild(stat('근거', s.research.findings.length + '개'));
    if (run.status === 'failed') {
      // run.failed.error is a sentence meant to be shown as-is (docs/event-schema.md)
      prog.appendChild(el('p', { class: 'summary-error' }, [el('b', { text: '실행 실패' }), ' · ' + (run.error || '원인을 알 수 없어요')]));
    }

    var res = $('summaryResults');
    res.textContent = '';
    res.hidden = !complete;
    if (complete) {
      chans.forEach(function (c) {
        var ch = s.channels[c];
        var m = manifest.channels[c] || {};
        var failed = !!(ch && ch.state === 'error');
        var score = ch && typeof ch.score === 'number' ? ch.score : (run.scores && run.scores[c]);
        var hasScore = typeof score === 'number';
        var passed = ch && ch.passed !== null ? ch.passed : (run.passed && run.passed[c]);
        var icon = m.icon ? el('img', { src: assetUrl(m.icon), alt: '' }) : el('span', { class: 'mini-fallback', style: '--ch:' + (m.color || '#3B5BDB'), text: chName(c).charAt(0) });
        var label = failed
          ? chName(c) + ' 작업 실패, 자세히 보기'
          : chName(c) + ' 최종 ' + (hasScore ? score + '점' : '점수 없음') + ', ' + (passed ? '통과' : '미통과') + ', 결과물 보기';
        res.appendChild(el('button', {
          type: 'button', class: 'result-tile', 'data-passed': String(!!passed), 'data-error': failed ? 'true' : null,
          'aria-label': label, title: failed ? ch.error : null,
          onclick: function (e) { openDrawer(c, e.currentTarget); }
        }, [icon, el('span', { class: 'rt-name', text: chName(c) }), el('span', { class: 'rt-score', text: failed ? '오류' : hasScore ? String(score) : '—' })]));
      });
    }
  }

  function renderPhase(s) {
    var ph = currentPhase(s);
    var idx = PHASES.indexOf(ph);
    var started = !!s.run;
    var failed = started && s.run.status === 'failed';
    Array.prototype.forEach.call($('phase').children, function (li) {
      var i = PHASES.indexOf(li.dataset.phase);
      // a failed run stays on the step where it stopped, marked as failed
      var st = !started ? '' : i < idx ? 'done' : i === idx ? (failed ? 'failed' : 'active') : '';
      if (li.dataset.state !== st) li.dataset.state = st;
      if (st === 'active' || st === 'failed') li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current');
      if (st === 'failed') li.title = '이 단계에서 실행이 멈췄어요'; else li.removeAttribute('title');
    });
  }

  var transportKey = '';
  function renderTransport(now) {
    var live = player.mode === 'live';
    var t = player.t;
    if (live && state.run && state.run.status === 'running') t = player.t + (now - player.liveArrival) / 1000;
    var finished = !live && player.events.length && player.t >= player.duration;
    var runFailed = !!(finished && state.run && state.run.status === 'failed');
    var recState = live ? 'live' : runFailed ? 'failed' : finished ? 'done' : player.playing ? 'playing' : 'paused';
    var recText = live ? 'LIVE' : runFailed ? '실행 실패' : finished ? '재생 완료' : player.playing ? '재생 중 ' + player.speed + '×' : (player.events.length ? '일시정지' : '대기');
    var key = [Math.floor(t), recState, recText, player.duration, player.speed, live, player.events.length].join('|');
    if (key === transportKey) return;
    transportKey = key;
    $('hudClock').textContent = fmtClock(t);
    var rec = $('hudRec');
    rec.dataset.state = recState;
    $('hudRecText').textContent = recText;
    var btn = $('btnPlay');
    btn.dataset.state = player.playing ? 'playing' : 'paused';
    btn.setAttribute('aria-label', player.playing ? '일시정지' : (finished ? '처음부터 다시 재생' : '재생'));
    var noEvents = !player.events.length;
    btn.disabled = live || noEvents;
    $('btnRestart').disabled = live || noEvents;
    var scrub = $('scrubber');
    scrub.disabled = live || noEvents;
    if (document.activeElement !== scrub || !scrubbing) scrub.value = String(player.duration ? Math.round(player.t / player.duration * 1000) : 0);
    scrub.setAttribute('aria-valuetext', fmtClock(player.t) + ' / ' + fmtClock(player.duration));
    $('timeLabel').textContent = fmtClock(t) + ' / ' + fmtClock(live ? t : player.duration);
    document.querySelector('.speed').setAttribute('aria-disabled', String(live));
  }

  // ------------------------------------------------------------------ drawer
  function openDrawer(id, trigger) {
    drawerChannel = id;
    drawerTab = 'content';
    drawerReviewRound = null;
    lastTrigger = trigger || null;
    renderDrawer(false);
    selectTab('content', false);
    var dlg = $('drawer');
    if (!dlg.open) {
      if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
    }
    $('drawerClose').focus();
  }

  function sourcesById() {
    var map = {};
    state.research.sources.forEach(function (x) { if (x.id) map[x.id] = x; });
    return map;
  }

  function renderDrawer(keepScroll) {
    var id = drawerChannel;
    var ch = state.channels[id] || newChannel(id);
    drawerRef = state.channels[id];
    var m = manifest.channels[id] || {};
    var body = document.querySelector('.drawer-body');
    var scroll = keepScroll ? body.scrollTop : 0;
    var icon = $('drawerIcon');
    if (m.icon) { icon.src = assetUrl(m.icon); icon.hidden = false; } else { icon.hidden = true; }
    $('drawerEyebrow').textContent = (m.name || chName(id)) + ' · ' + (CH_STATE_LABEL[ch.state] || '') + ' · R' + (ch.round || 0);
    var draft = last(ch.drafts);
    $('drawerTitle').textContent = (ch.final && ch.final.title) || (draft && draft.title) || (m.name || chName(id));
    var sc = $('drawerScore');
    sc.textContent = '';
    if (typeof ch.score === 'number') {
      sc.appendChild(el('span', { class: 'big', 'data-passed': String(!!ch.passed), text: String(ch.score) }));
      sc.appendChild(el('span', { class: 'small', text: (ch.passed ? '통과' : '기준 ' + ((state.run && state.run.passScore) || 80) + '점 미달') }));
    }
    renderContentPane(id, ch);
    renderReviewPane(id, ch);
    renderHistoryPane(id, ch);
    body.scrollTop = scroll;
  }

  function composeCopy(ch) {
    var f = ch.final;
    if (!f) return '';
    var text = (f.title ? f.title + '\n\n' : '') + String(f.content || '').trim();
    var tags = (f.hashtags || []).join(' ');
    if (tags && String(f.content || '').indexOf(f.hashtags[0]) < 0) text += '\n\n' + tags;
    return text;
  }

  function renderContentPane(id, ch) {
    var pane = $('pane-content');
    pane.textContent = '';
    var draft = last(ch.drafts);
    if (ch.state === 'error') {
      pane.appendChild(el('p', { class: 'pane-error' }, [el('b', { text: '이 채널 작업이 실패했어요' }), ' · ' + (ch.error || '원인을 알 수 없어요')]));
    }
    if (ch.final) {
      var status = el('span', { class: 'copy-status', role: 'status' });
      var fallback = el('textarea', { class: 'copy-fallback field-like', rows: '8', readonly: true, 'aria-label': '복사할 텍스트', hidden: true });
      var copyBtn = el('button', {
        type: 'button', class: 'btn btn--primary', text: '전체 복사',
        onclick: function () { copyText(composeCopy(ch), status, fallback); }
      });
      pane.appendChild(el('div', { class: 'copy-row' }, [copyBtn, status]));
      pane.appendChild(fallback);
      // bizplan content already opens with "# <title>"; don't show the title twice
      if (ch.final.title && !/^\s*#\s+/.test(ch.final.content || '')) pane.appendChild(el('h3', { class: 'out-title', text: ch.final.title }));
      var md = el('div', { class: 'md' });
      md.innerHTML = renderMarkdown(ch.final.content, { sources: sourcesById() });
      pane.appendChild(md);
      if (ch.final.hashtags && ch.final.hashtags.length) {
        pane.appendChild(el('div', null, [
          el('h4', { class: 'sub-title', text: id === 'naver_blog' ? '태그' : '해시태그' }),
          el('div', { class: 'hashtags' }, ch.final.hashtags.map(function (h) { return el('span', { text: h }); }))
        ]));
      }
    } else if (draft) {
      pane.appendChild(el('p', {
        class: 'pane-note',
        text: ch.state === 'error'
          ? '마지막 초안 R' + (draft.round || 0) + ' 발췌예요. 최종본은 만들어지지 않았어요.'
          : '최신 초안 R' + (draft.round || 0) + ' 발췌예요. 최종본이 확정되면 전체 본문이 여기에 표시돼요.'
      }));
      pane.appendChild(el('h3', { class: 'out-title', text: draft.title || '' }));
      pane.appendChild(el('div', { class: 'md' }, el('blockquote', { text: draft.excerpt || '' })));
      pane.appendChild(el('p', { class: 'pane-note', text: '분량 ' + charsLabel(id, draft) }));
      if (draft.hashtags && draft.hashtags.length) {
        pane.appendChild(el('div', { class: 'hashtags' }, draft.hashtags.map(function (h) { return el('span', { text: h }); })));
      }
    } else if (ch.state !== 'error') {
      pane.appendChild(el('p', { class: 'empty', text: '아직 초안이 없어요. 총괄 에이전트가 쓰기 시작하면 여기에서 볼 수 있어요.' }));
    }
  }

  function copyText(text, statusEl, fallbackEl) {
    function fallback() {
      fallbackEl.hidden = false;
      fallbackEl.value = text;
      fallbackEl.focus();
      fallbackEl.select();
      var ok = false;
      try { ok = document.execCommand && document.execCommand('copy'); } catch (e) { ok = false; }
      statusEl.textContent = ok ? '복사했어요.' : '아래 텍스트를 선택해 두었어요. Ctrl+C(⌘+C)로 복사하세요.';
    }
    if (!text) { statusEl.textContent = '복사할 내용이 없어요.'; return; }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          fallbackEl.hidden = true;
          statusEl.textContent = '복사했어요. 붙여넣기만 하면 돼요.';
        }, fallback);
      } else {
        fallback();
      }
    } catch (e) { fallback(); }
  }

  function renderReviewPane(id, ch) {
    var pane = $('pane-review');
    pane.textContent = '';
    if (ch.state === 'error') {
      pane.appendChild(el('p', { class: 'pane-error' }, [el('b', { text: '검수를 마치지 못했어요' }), ' · ' + (ch.error || '원인을 알 수 없어요')]));
    }
    if (!ch.reviews.length) {
      if (ch.state !== 'error') pane.appendChild(el('p', { class: 'empty', text: ch.state === 'reviewing' ? '검수 에이전트가 채점하는 중이에요.' : '아직 검수 결과가 없어요.' }));
      return;
    }
    var rounds = ch.reviews.map(function (r) { return r.round; });
    var finalRound = ch.final && rounds.indexOf(ch.final.round) >= 0 ? ch.final.round : null;
    var round = drawerReviewRound !== null && rounds.indexOf(drawerReviewRound) >= 0 ? drawerReviewRound
      : finalRound !== null ? finalRound : rounds[rounds.length - 1];
    var r = ch.reviews.filter(function (x) { return x.round === round; })[0];
    if (rounds.length > 1) {
      pane.appendChild(el('div', { class: 'seg', role: 'group', 'aria-label': '검수 라운드' }, rounds.map(function (rd) {
        var rv = ch.reviews.filter(function (x) { return x.round === rd; })[0];
        return el('button', {
          type: 'button', 'aria-pressed': String(rd === round), text: 'R' + rd + ' · ' + rv.score + '점' + (rd === finalRound ? ' · 최종' : ''),
          onclick: function () { drawerReviewRound = rd; renderReviewPane(id, state.channels[id]); }
        });
      })));
    }
    appendChildren(pane, reviewDetails(r));
  }

  var VERDICT_LABEL = { supported: '근거 있음', unsupported: '근거와 다름', needs_source: '출처 필요' };

  /**
   * Review body shared by the stage drawer and the 보관함 detail: summary, rubric, issues,
   * code format checks, fact checks and follow-up research questions. `r` is either a
   * review.completed event payload (fact_checks = counts) or a stored Review (fact_checks = list).
   * Returns an array of nodes; every model string goes through textContent.
   */
  function reviewDetails(r) {
    var out = [];
    if (r.summary) out.push(el('p', { class: 'review-summary', text: r.summary }));

    var rubric = r.rubric || [];
    if (rubric.length) {
      var total = rubric.reduce(function (a, x) { return a + (x.score || 0); }, 0);
      var totalMax = rubric.reduce(function (a, x) { return a + (x.max || 0); }, 0);
      out.push(el('div', null, [
        el('h3', { class: 'sub-title', text: '루브릭' }),
        el('div', { class: 'table-wrap' }, el('table', { class: 'rubric' }, [
          el('thead', null, el('tr', null, [el('th', { scope: 'col', text: '항목' }), el('th', { scope: 'col', text: '점수' }), el('th', { scope: 'col', text: '코멘트' })])),
          el('tbody', null, rubric.map(function (x) {
            var pct = x.max ? Math.round(100 * x.score / x.max) : 0;
            return el('tr', null, [
              el('td', null, [x.label, el('span', { class: 'bar', 'aria-hidden': 'true' }, el('i', { style: 'width:' + pct + '%' }))]),
              el('td', { class: 'num', text: x.score + ' / ' + x.max }),
              el('td', { class: 'comment', text: x.comment || '' })
            ]);
          })),
          el('tfoot', null, el('tr', null, [el('td', { text: '합계 (100점 환산)' }), el('td', { class: 'num', text: total + ' / ' + totalMax }), el('td', { text: r.score + '점 · ' + (r.passed ? '통과' : '미통과') })]))
        ]))
      ]));
    }

    var issues = r.issues || [];
    out.push(el('div', null, [
      el('h3', { class: 'sub-title', text: '이슈 ' + issues.length + '건' }),
      issues.length ? el('ul', { class: 'issues' }, issues.map(function (x) {
        return el('li', { class: 'issue', 'data-sev': x.severity }, [
          el('span', { class: 'sev', text: (SEVERITY_LABEL[x.severity] || x.severity) + (x.location ? ' · ' + x.location : '') }),
          el('span', { text: x.problem }),
          x.fix ? el('span', { class: 'fix', text: '고칠 점: ' + x.fix }) : null
        ]);
      })) : el('p', { class: 'empty', text: '지적된 이슈가 없어요.' })
    ]));

    var checks = r.format_checks || [];
    if (checks.length) out.push(formatChecksBlock(checks, '형식 검사 (코드 자동 채점)'));

    var fc = r.fact_checks;
    if (fc && typeof fc === 'object' && !Array.isArray(fc)) {
      out.push(el('div', null, [
        el('h3', { class: 'sub-title', text: '사실 확인' }),
        el('div', { class: 'facts' }, [stat('근거 있음', fc.supported || 0), stat('근거와 다름', fc.unsupported || 0), stat('출처 필요', fc.needs_source || 0)])
      ]));
    } else if (Array.isArray(fc) && fc.length) {
      var cnt = { supported: 0, unsupported: 0, needs_source: 0 };
      fc.forEach(function (x) { cnt[x.verdict] = (cnt[x.verdict] || 0) + 1; });
      // problems first: unsupported, then needs_source, then supported
      var order = { unsupported: 0, needs_source: 1, supported: 2 };
      var sorted = fc.slice().sort(function (a, b) { return (order[a.verdict] || 0) - (order[b.verdict] || 0); });
      out.push(el('div', null, [
        el('h3', { class: 'sub-title', text: '사실 확인' }),
        el('div', { class: 'facts' }, [stat('근거 있음', cnt.supported), stat('근거와 다름', cnt.unsupported), stat('출처 필요', cnt.needs_source)]),
        el('details', { class: 'fact-list' }, [
          el('summary', { text: '주장별 확인 결과 ' + fc.length + '건 보기' }),
          el('ul', null, sorted.map(function (x) {
            return el('li', { 'data-verdict': x.verdict }, [
              el('span', { class: 'verdict', text: VERDICT_LABEL[x.verdict] || x.verdict }),
              el('span', { class: 'claim', text: x.claim }),
              (x.source_ids && x.source_ids.length) || x.note
                ? el('span', { class: 'fact-note', text: [(x.source_ids || []).join(', '), x.note || ''].filter(Boolean).join(' · ') })
                : null
            ]);
          }))
        ])
      ]));
    }
    if (r.needs_research && r.needs_research.length) {
      out.push(el('div', null, [
        el('h3', { class: 'sub-title', text: '리서치에 넘긴 추가 질문' }),
        el('ul', { class: 'gaps' }, r.needs_research.map(function (q) { return el('li', { text: q }); }))
      ]));
    }
    return out;
  }

  function formatChecksBlock(checks, title) {
    return el('div', null, [
      el('h3', { class: 'sub-title', text: title }),
      el('ul', { class: 'checks-list' }, checks.map(function (c) {
        return el('li', null, [
          el('span', { class: c.passed ? 'ok' : 'no', text: c.passed ? '✓' : '✕', 'aria-label': c.passed ? '통과' : '미통과' }),
          el('span', { text: c.label }),
          el('span', { class: 'val', text: c.value + ' / 기준 ' + c.expected })
        ]);
      }))
    ]);
  }

  function renderHistoryPane(id, ch) {
    var pane = $('pane-history');
    pane.textContent = '';
    var rounds = {};
    ch.drafts.forEach(function (d) { rounds[d.round || 0] = rounds[d.round || 0] || {}; rounds[d.round || 0].draft = d; });
    ch.reviews.forEach(function (r) { rounds[r.round || 0] = rounds[r.round || 0] || {}; rounds[r.round || 0].review = r; });
    var keys = Object.keys(rounds).map(Number).sort(function (a, b) { return a - b; });
    if (!keys.length) {
      pane.appendChild(el('p', { class: 'empty', text: '아직 기록된 라운드가 없어요.' }));
      return;
    }
    pane.appendChild(el('ol', { class: 'round-list' }, keys.map(function (k) {
      var x = rounds[k];
      var d = x.draft, r = x.review;
      return el('li', { class: 'round' }, [
        el('span', { class: 'round-tag', text: 'R' + k }),
        el('div', { class: 'round-body' }, [
          el('div', null, [
            el('b', { text: k ? '수정본' : '첫 초안' }), d ? ' · ' + charsLabel(id, d) : '',
            r ? ' · ' : '', r ? el('span', { class: 'pill', 'data-tone': r.passed ? 'pass' : 'fail', text: r.score + '점 ' + (r.passed ? '통과' : '미통과') }) : '',
            ch.final && ch.final.round === k ? [' ', el('span', { class: 'pill', 'data-tone': 'final', text: '최종본' })] : ''
          ]),
          d && d.change_log && d.change_log.length ? el('ul', null, d.change_log.map(function (c) { return el('li', { text: c }); })) : null,
          r && !r.passed && r.issues && r.issues[0] ? el('span', { class: 'pane-note', text: '주요 지적: ' + r.issues[0].problem }) : null
        ])
      ]);
    })));
  }

  function selectTab(name, focus) {
    drawerTab = name;
    ['content', 'review', 'history'].forEach(function (t) {
      var tab = $('tab-' + t), pane = $('pane-' + t);
      var on = t === name;
      tab.setAttribute('aria-selected', String(on));
      tab.tabIndex = on ? 0 : -1;
      pane.hidden = !on;
      if (on && focus) tab.focus();
    });
  }

  // ------------------------------------------------------------------ 3D viewer
  var mvPromise = null;
  function loadModelViewer() {
    if (window.customElements && customElements.get('model-viewer')) return Promise.resolve();
    if (!mvPromise) {
      mvPromise = new Promise(function (resolve, reject) {
        var sc = document.createElement('script');
        sc.type = 'module';
        sc.src = MODEL_VIEWER_SRC;
        sc.onload = function () { customElements.whenDefined('model-viewer').then(resolve); };
        sc.onerror = function () { mvPromise = null; reject(new Error('model-viewer 로드 실패')); };
        (document.head || document.documentElement).appendChild(sc);
      });
    }
    return mvPromise;
  }
  function openModel(id, trigger) {
    var m = manifest.agents[id];
    var src = assetUrl(m && m.model);
    if (!src) return;
    lastTrigger = trigger || null;
    var host = $('modelHost');
    host.textContent = '3D 모델을 불러오는 중이에요…';
    $('modelTitle').textContent = m.name + ' · ' + (m.nickname || '');
    var dlg = $('modelDialog');
    if (!dlg.open) dlg.showModal();
    loadModelViewer().then(function () {
      host.textContent = '';
      var mv = document.createElement('model-viewer');
      mv.setAttribute('src', src);
      mv.setAttribute('alt', m.name + ' 3D 카피바라 캐릭터');
      mv.setAttribute('camera-controls', '');
      mv.setAttribute('touch-action', 'pan-y');
      if (!reduceMotion.matches) mv.setAttribute('auto-rotate', '');
      mv.setAttribute('shadow-intensity', '1');
      mv.setAttribute('exposure', '1.05');
      if (m.image) mv.setAttribute('poster', assetUrl(m.image));
      mv.addEventListener('error', function () { host.textContent = '3D 모델 파일을 열지 못했어요.'; });
      host.appendChild(mv);
    }).catch(function () {
      host.textContent = '3D 뷰어를 불러오지 못했어요. 네트워크 연결을 확인해 주세요.';
    });
  }

  // ------------------------------------------------------------------ brief form (live)
  var briefPrefilled = false;
  function openBrief(trigger) {
    lastTrigger = trigger || null;
    var dlg = $('briefDialog');
    $('briefError').hidden = true;
    if (!briefPrefilled) {
      briefPrefilled = true;
      fillBrief((state.run && state.run.brief) || (traceMeta && traceMeta.brief) || null);
      fetchJson('/api/sample-brief').then(function (b) {
        var brief = b && (b.brief || b);
        if (brief && brief.topic && !$('f-topic').dataset.touched) fillBrief(brief);
      }).catch(function () { /* keep the prefilled values */ });
    }
    if (!dlg.open) dlg.showModal();
    $('f-topic').focus();
  }
  function fillBrief(b) {
    if (!b) return;
    $('f-topic').value = b.topic || '';
    $('f-goal').value = b.goal || '';
    $('f-audience').value = b.audience || '';
    $('f-tone').value = b.tone || '';
    $('f-notes').value = b.notes || '';
    $('f-keywords').value = (b.keywords || []).join(', ');
    var chans = b.channels && b.channels.length ? b.channels : CHANNEL_IDS;
    CHANNEL_IDS.forEach(function (c) { $('f-ch-' + c).checked = chans.indexOf(c) >= 0; });
  }
  function submitBrief(e) {
    e.preventDefault();
    var err = $('briefError');
    var topic = $('f-topic').value.trim();
    var channels = CHANNEL_IDS.filter(function (c) { return $('f-ch-' + c).checked; });
    if (!topic) { err.textContent = '주제를 입력해 주세요.'; err.hidden = false; $('f-topic').focus(); return; }
    if (!channels.length) { err.textContent = '채널을 하나 이상 골라 주세요.'; err.hidden = false; return; }
    var options = {};
    var mode = $('f-mode').value;
    if (mode) options.mode = mode;
    var speed = parseFloat($('f-speed').value);
    if (!isNaN(speed)) options.speed = speed;
    var rounds = parseInt($('f-rounds').value, 10);
    if (!isNaN(rounds)) options.max_rounds = rounds;
    var pass = parseInt($('f-pass').value, 10);
    if (!isNaN(pass)) options.pass_score = pass;
    var body = {
      topic: topic,
      goal: $('f-goal').value.trim(),
      audience: $('f-audience').value.trim(),
      channels: channels,
      tone: $('f-tone').value.trim(),
      keywords: $('f-keywords').value.split(/[,，]/).map(function (k) { return k.trim(); }).filter(Boolean),
      notes: $('f-notes').value.trim(),
      language: 'ko',
      options: options
    };
    var submit = $('briefSubmit');
    submit.disabled = true;
    submit.textContent = '시작하는 중…';
    startLiveRun(body).then(function () {
      $('briefDialog').close();
    }).catch(function (ex) {
      err.textContent = '실행을 시작하지 못했어요: ' + (ex && ex.message ? ex.message : '알 수 없는 오류') + '. 서버 로그를 확인해 주세요.';
      err.hidden = false;
    }).then(function () {
      submit.disabled = false;
      submit.textContent = '실행 시작';
    });
  }

  // ------------------------------------------------------------------ theme
  function effectiveTheme() {
    var a = root.getAttribute('data-theme');
    if (a === 'light' || a === 'dark') return a;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  function syncThemeButton() {
    var next = effectiveTheme() === 'dark' ? '밝은' : '어두운';
    $('btnTheme').setAttribute('aria-label', next + ' 테마로 바꾸기');
  }

  // ------------------------------------------------------------------ wiring
  var scrubbing = false;
  function setupControls() {
    $('btnPlay').addEventListener('click', function () { if (player.playing) pause(); else play(); });
    $('btnRestart').addEventListener('click', function () { seek(0); play(); });
    Array.prototype.forEach.call(document.querySelectorAll('.speed button'), function (b) {
      b.addEventListener('click', function () {
        player.speed = Number(b.dataset.speed) || 1;
        document.querySelectorAll('.speed button').forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
        transportKey = '';
      });
    });
    var log = $('timeline');
    log.addEventListener('scroll', function () {
      if (logProgrammatic) { logProgrammatic = false; return; }
      logStick = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    }, { passive: true });
    if (window.ResizeObserver) new ResizeObserver(function () { if (logStick) scrollLogToEnd(log); }).observe(log);
    var scrub = $('scrubber');
    scrub.addEventListener('input', function () {
      scrubbing = true;
      seek(Number(scrub.value) / 1000 * player.duration);
    });
    scrub.addEventListener('change', function () { scrubbing = false; });
    Array.prototype.forEach.call(document.querySelectorAll('.seg[data-role="log-filter"] button'), function (b) {
      b.addEventListener('click', function () {
        $('timeline').dataset.filter = b.dataset.filter;
        document.querySelectorAll('.seg[data-role="log-filter"] button').forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
        storageSet('insia.logFilter', b.dataset.filter);
      });
    });
    var savedFilter = storageGet('insia.logFilter');
    if (savedFilter === 'key' || savedFilter === 'all') {
      var fb = document.querySelector('.seg[data-role="log-filter"] button[data-filter="' + savedFilter + '"]');
      if (fb) fb.click();
    }

    $('btnTheme').addEventListener('click', function () {
      var next = effectiveTheme() === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      storageSet('insia.theme', next);
      syncThemeButton();
    });
    var savedTheme = storageGet('insia.theme');
    if (savedTheme === 'light' || savedTheme === 'dark') root.setAttribute('data-theme', savedTheme);
    syncThemeButton();
    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: light)');
      if (mq.addEventListener) mq.addEventListener('change', syncThemeButton);
    }

    // drawer
    var drawer = $('drawer');
    $('drawerClose').addEventListener('click', function () { drawer.close(); });
    drawer.addEventListener('click', function (e) { if (e.target === drawer) drawer.close(); });
    drawer.addEventListener('close', function () { drawerChannel = null; restoreFocus(); });
    var tabs = ['content', 'review', 'history'];
    tabs.forEach(function (t, i) {
      var tab = $('tab-' + t);
      tab.addEventListener('click', function () { selectTab(t, false); });
      tab.addEventListener('keydown', function (e) {
        var j = null;
        if (e.key === 'ArrowRight') j = (i + 1) % tabs.length;
        else if (e.key === 'ArrowLeft') j = (i + tabs.length - 1) % tabs.length;
        else if (e.key === 'Home') j = 0;
        else if (e.key === 'End') j = tabs.length - 1;
        if (j !== null) { e.preventDefault(); selectTab(tabs[j], true); }
      });
    });

    // brief + model dialogs
    $('btnNewRun').addEventListener('click', function (e) { openBrief(e.currentTarget); });
    $('btnOpenBrief').addEventListener('click', function (e) { openBrief(e.currentTarget); });
    $('briefClose').addEventListener('click', function () { $('briefDialog').close(); });
    $('briefCancel').addEventListener('click', function () { $('briefDialog').close(); });
    $('briefForm').addEventListener('submit', submitBrief);
    $('f-topic').addEventListener('input', function () { $('f-topic').dataset.touched = '1'; });
    $('briefDialog').addEventListener('close', restoreFocus);
    $('modelClose').addEventListener('click', function () { $('modelDialog').close(); });
    $('modelDialog').addEventListener('close', function () { $('modelHost').textContent = ''; restoreFocus(); });
    [$('briefDialog'), $('modelDialog')].forEach(function (dlg) {
      dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });
    });

    // layout + motion preferences
    if (window.ResizeObserver) new ResizeObserver(queueLayout).observe($('monitor'));
    window.addEventListener('resize', queueLayout);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(queueLayout);
    window.addEventListener('load', queueLayout);
    var onMotion = function () { AGENT_IDS.forEach(function (id) { setAgentMotion(id, dom.agents[id].active); }); if (reduceMotion.matches) clearPackets(); };
    if (reduceMotion.addEventListener) reduceMotion.addEventListener('change', onMotion);
    document.addEventListener('visibilitychange', onMotion);
  }

  function restoreFocus() {
    var t = lastTrigger;
    lastTrigger = null;
    if (t && document.contains(t) && typeof t.focus === 'function') t.focus();
  }

  function boot() {
    setupControls();
    requestAnimationFrame(frame);
    loadManifest().then(function (m) {
      manifest = m;
      buildStage();
      forceRender = true;
      dirty = true;
      queueLayout();
      return Promise.all([detectServer(), loadTrace()]);
    }).then(function (res) {
      var info = res[0];
      var trace = res[1];
      bootTrace = trace;
      setServer(info);
      if (trace) {
        traceMeta = trace.meta || {};
        sourceKind = traceKind(trace);
        loadEvents(trace.events);
        play();
      } else {
        sourceKind = 'none';
      }
      summaryKey = '';
      dirty = true;
      forceRender = true;
      queueLayout();
      resolveReady({ server: info, trace: trace });
    }).catch(function (err) {
      if (window.console) console.error('초기화 오류', err);
      resolveReady({ server: null, trace: null });
    });
  }

  var bootTrace = null;
  var resolveReady;
  var ready = new Promise(function (resolve) { resolveReady = resolve; });

  // Exposed for the workspace views (js/*.js), debugging and tests.
  window.INSIA = {
    applyEvent: applyEvent,
    initialState: initialState,
    renderMarkdown: renderMarkdown,
    getState: function () { return state; },
    player: player,
    seek: function (t) { seek(t); },
    play: play,
    pause: pause,
    openDrawer: openDrawer,
    /** Resolves once the manifest, the demo trace and the /api/health probe are settled: {server, trace}. */
    ready: ready,
    views: {},
    util: {
      $: $, el: el, esc: esc, appendChildren: appendChildren, fmtNum: fmtNum, fmtClock: fmtClock, fmtDuration: fmtDuration,
      clip: clip, last: last, chName: chName, isChannel: isChannel, charsLabel: charsLabel, stat: stat,
      storageGet: storageGet, storageSet: storageSet, assetUrl: assetUrl, fetchJson: fetchJson,
      renderMarkdown: renderMarkdown, reviewDetails: reviewDetails, formatChecksBlock: formatChecksBlock, copyText: copyText,
      CHANNEL_IDS: CHANNEL_IDS, CHANNEL_NAMES: CHANNEL_NAMES, SEVERITY_LABEL: SEVERITY_LABEL, TIER_LABEL: TIER_LABEL,
      manifest: function () { return manifest; },
      reduceMotion: function () { return !!reduceMotion.matches; }
    },
    studio: {
      /** Stream a server run (pipeline or item job) into the stage; see enterLive. */
      watchRun: function (runId, opts) { enterLive(runId, opts); },
      liveRunId: function () { return liveSource ? liveRunId : ''; },
      detectServer: detectServer,
      setServer: setServer,
      server: function () { return serverInfo; },
      bootTrace: function () { return bootTrace; },
      openBrief: function (trigger) { openBrief(trigger); },
      /** The studio view was shown or hidden by the app shell (pause videos, re-measure wires). */
      setVisible: function (on) {
        stageHidden = !on;
        AGENT_IDS.forEach(function (id) { if (dom.agents[id]) setAgentMotion(id, dom.agents[id].active); });
        if (on) { queueLayout(); forceRender = true; dirty = true; }
      }
    }
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
