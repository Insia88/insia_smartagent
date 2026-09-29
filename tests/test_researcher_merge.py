"""A follow-up pack must not bind a dangling source id to another channel's source."""

from __future__ import annotations

from insia_agents.agents.researcher import _drop_unknown_source_ids
from insia_agents.backends.base import merge_research
from insia_agents.models import Finding, ResearchPack, Source


def _src(sid: str, url: str) -> Source:
    return Source(id=sid, title=sid, url=url, tier=2)


def _fin(fid: str, *sids: str) -> Finding:
    return Finding(id=fid, question_id="q9", claim=f"claim {fid}", source_ids=list(sids), confidence="medium")


def test_dangling_id_is_not_bound_to_a_source_merged_meanwhile():
    snapshot = ResearchPack(findings=[_fin("f1", "s1")], sources=[_src("s1", "https://a.kr/1")])
    # channel B merged s2 and s3 while channel A's follow-up call was running
    current = ResearchPack(findings=snapshot.findings,
                           sources=snapshot.sources + [_src("s2", "https://b.kr/2"), _src("s3", "https://b.kr/3")])
    # channel A's structuring output lists its own s2 but cites a dangling s3
    raw = ResearchPack(findings=[_fin("f2", "s2"), _fin("f3", "s3")], sources=[_src("s2", "https://a.kr/new")])

    cleaned = _drop_unknown_source_ids(raw, snapshot)
    merged, added = merge_research(current, cleaned)

    urls = {s.id: s.url for s in merged.sources}
    for finding in added.findings:
        assert all(not urls[sid].startswith("https://b.kr/") for sid in finding.source_ids), finding
    assert any("claim f3" in gap for gap in merged.gaps)  # the unsourced claim is dropped into gaps


def test_valid_ids_pass_through_unchanged():
    snapshot = ResearchPack(findings=[], sources=[_src("s1", "https://a.kr/1")])
    raw = ResearchPack(findings=[_fin("f2", "s1", "s2")], sources=[_src("s2", "https://a.kr/2")])
    assert _drop_unknown_source_ids(raw, snapshot) is raw
