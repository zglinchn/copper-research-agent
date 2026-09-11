"""区域化证据链单元测试
==================================================================
覆盖：区域注册表加载与映射（德国→EU-15、哈萨克斯坦→Central Asia）、
来源权威分层、区域命中判定、五维评分三档、全球主流兜底标注、
region_evidence_gate 节点级行为（不发起整图运行）。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regions import (GCAM_REGIONS, normalize_region, country_to_region,
                     region_options, resolve_country, country_options)
from evidence_scoring import (
    apply_region_fallback_marking, classify_source_tier, citation_region_hit,
    compute_region_assessment,
)


def test_registry_loads_32_gcam_regions():
    ids = {r.region_id for r in GCAM_REGIONS}
    for expected in ("China", "USA", "EU-15", "Central Asia", "South Africa",
                     "Japan", "South Korea", "Taiwan", "Southeast Asia"):
        assert expected in ids
    assert len(GCAM_REGIONS) >= 32


def test_normalize_region_and_country_mapping():
    assert normalize_region("中国").region_id == "China"
    assert normalize_region("USA").region_id == "USA"
    assert normalize_region("美国").region_id == "USA"
    assert normalize_region("Germany").region_id == "EU-15"      # 成员国别名归区
    assert normalize_region("Kazakhstan").region_id == "Central Asia"
    assert country_to_region("Germany") == "EU-15"
    assert country_to_region("Kazakhstan") == "Central Asia"
    assert normalize_region("月球") is None                       # 自由文本兜底
    assert normalize_region("Austria").region_id == "EU-15"
    assert resolve_country("美国").country_id == "United States"
    assert resolve_country("EU-15") is None
    # 前端下拉数据
    opts = region_options()
    assert any(o["region_id"] == "China" for o in opts)
    assert any(o["country_id"] == "United States" for o in country_options())


def test_classify_source_tier():
    assert classify_source_tier("IEA", "https://www.iea.org/reports/x") == "authoritative"
    assert classify_source_tier("", "https://example.edu/paper.pdf") == "authoritative"
    assert classify_source_tier("Schneider Electric", "https://blog.se.com/a") == "authoritative"
    assert classify_source_tier("Reuters", "https://www.reuters.com/x") == "medium"
    assert classify_source_tier("某博客", "https://random-blog.example.org/p/1") == "low"


def test_citation_region_hit():
    china = normalize_region("中国")
    usa = normalize_region("USA")
    eu = normalize_region("EU-15")
    assert citation_region_hit({"title": "中国空调市场铜用量报告"}, china)
    assert citation_region_hit({"title": "US transformer winding market",
                                "url": "https://x.org/us-market"}, usa)
    assert not citation_region_hit({"title": "德国市场报告"}, china)
    # evidence_country 经成员国表归区：Germany → EU-15
    assert citation_region_hit({"title": "European market", "evidence_country": "Germany"}, eu)
    assert citation_region_hit({"title": "无关标题"}, eu, evidence_country="France")
    us_country = resolve_country("美国")
    assert citation_region_hit({"title": "US transformer market",
                                "evidence_country": "USA"}, us_country)
    assert not citation_region_hit({"title": "Industry report"}, us_country)
    assert not citation_region_hit({"title": "Austria transformer market"}, us_country)


def test_citation_schema_preserves_country_and_caliber():
    from schemas import Citation, ProductResearchOutput, ResearchGeography
    c = Citation(source_type="industry_report", title="x", confidence="high",
                 evidence_country="USA", evidence_year=2025,
                 evidence_scope="country", statistic_caliber="美国销量中的份额")
    assert c.model_dump()["evidence_country"] == "USA"
    geo = ResearchGeography(country_id="United States", country_name="美国", gcam_region_id="USA")
    out = ProductResearchOutput(product_id="x", product_name="x", geography=geo,
                                region_id="USA", region_name="美国",
                                classification_dimensions=[], functional_subsystems=[],
                                copper_components=[])
    assert out.geography.country_id == "United States"


def _cite(title, country=None, url="https://example.org/a", date="2025-01"):
    c = {"source_type": "industry_report", "title": title, "publisher": "测试",
         "url": url, "publish_date": date, "confidence": "high"}
    if country:
        c["evidence_country"] = country
    return c


def test_compute_region_assessment_three_tiers():
    china = normalize_region("中国")
    # 充足：全部区域命中 + 权威来源 + 多来源 + 新近
    good = [_cite(f"中国空调铜用量{i}", url=f"https://src{i}.gov.cn/a",
                  date="2025-05") for i in range(6)]
    a = compute_region_assessment(good, china, baseline_year=2025, coverage_score=100)
    assert a.tier == "sufficient" and a.evidence_policy == "region_specific"
    assert a.n_region_hits == 6

    # 不足：零区域命中、低权威、单一来源
    bad = [_cite("random global blog post", url="https://blog.example.org/a",
                 date="2010-01") for _ in range(4)]
    b = compute_region_assessment(bad, china, baseline_year=2025, coverage_score=0)
    assert b.tier == "insufficient" and b.evidence_policy == "global_proxy"

    # 空引用
    c = compute_region_assessment([], china, baseline_year=2025)
    assert c.tier == "insufficient"


def test_apply_region_fallback_marking():
    import copy
    china = normalize_region("中国")
    dims = [{"dimension_id": "d", "values": [
        {"value_id": "v1", "citations": [_cite("中国主流型号")]},
        {"value_id": "v2", "citations": [_cite("global report")]},
    ]}]
    comps = [{"component_id": "cu1", "citations": [_cite("global report")]}]
    # 标注函数会原地写入，保留一份干净副本供 sufficient 分支使用
    dims_pristine, comps_pristine = copy.deepcopy(dims), copy.deepcopy(comps)
    dims2, comps2 = apply_region_fallback_marking(dims, comps, china, "global_proxy")
    assert dims2[0]["values"][0].get("region_basis", "region_specific") == "region_specific"
    assert dims2[0]["values"][1]["region_basis"] == "global_fallback"
    assert "全球主流" in dims2[0]["values"][1]["region_basis_note"]
    assert comps2[0]["region_basis"] == "global_fallback"
    # mixed 同样逐实体标注；sufficient 不动（用干净副本验证幂等不侵入）
    dims3, comps3 = apply_region_fallback_marking(dims, comps, china, "mixed")
    assert comps3[0]["region_basis"] == "global_fallback"
    dims4, comps4 = apply_region_fallback_marking(dims_pristine, comps_pristine,
                                                  china, "region_specific")
    assert "region_basis" not in dims4[0]["values"][1]


def test_region_evidence_gate_node():
    import research_graph as rg
    china = normalize_region("中国")
    good = _cite("中国空调铜用量权威报告", url="https://www.iea.org/a")
    bad = _cite("global blog", url="https://blog.example.org/a")
    state = {
        "product_id": "hvac", "product_name": "空调",
        "region_id": "中国", "region_name": "中国",
        "total_round": 5, "objections": [], "debate_log": [],
        "evidence_gate_done": False,
        "dimension_proposal": [{
            "dimension_id": "d1", "values": [
                {"value_id": "v1", "citations": [good]},
                {"value_id": "v2", "citations": [bad]},
            ], "evidence": [good],
        }],
        "structure_proposal": [{
            "subsystem_id": "sub-1", "subsystem_name": "核心",
            "decomposition_level": 0, "function_description": "f",
            "evidence": [good],
        }],
        "copper_components": [{
            "component_id": "cu1", "parent_subsystem_id": "sub-1",
            "component_name": "绕组", "citations": [good],
        }],
    }
    cmd = rg.region_evidence_gate(state)
    upd = cmd.update
    assert cmd.goto == "country_agent"
    assert upd["evidence_gate_done"] is True
    assert upd["evidence_assessment"]["n_citations"] == 5
    # 实体存在国家证据缺口时先派国家研究角色补查，补查前不破坏原参数。
    v2 = next(v for v in upd["dimension_proposal"][0]["values"] if v["value_id"] == "v2")
    assessment = upd["evidence_assessment"]
    assert v2.get("region_basis", "region_specific") == "region_specific"


def test_gate_region_basis_unsupported_flag():
    """sufficient 口径下无区域命中的实体保留 region_specific，
    gate 必须生成 region_basis_unsupported 审计标记（warning）"""
    import research_graph as rg
    good = _cite("中国空调铜用量权威报告", url="https://www.iea.org/a", date="2025-06")
    bad = _cite("global blog", url="https://blog.example.org/a", date="2025-06")
    state = {
        "product_id": "hvac", "product_name": "空调",
        "region_id": "中国", "region_name": "中国",
        "total_round": 5, "objections": [], "debate_log": [],
        "evidence_gate_done": False, "critic_coverage_score": 100,
        "country_retry_count": 2,
        "validation_flags": [],
        # 大量区域命中 + 权威 + 多样来源 → sufficient
        "dimension_proposal": [{
            "dimension_id": "d1", "values": [
                {"value_id": f"v{i}", "value_name": f"型号{i}",
                 "citations": [_cite(f"中国权威报告{i}", url=f"https://src{i}.gov.cn/a",
                                     date="2025-06")],
                } for i in range(5)
            ] + [{"value_id": "vX", "value_name": "无区域证据型号", "citations": [bad]}],
            "evidence": [good],
        }],
        "structure_proposal": [{
            "subsystem_id": "sub-1", "subsystem_name": "核心",
            "decomposition_level": 0, "function_description": "f",
            "evidence": [good],
        }],
        "copper_components": [{
            "component_id": "cu1", "parent_subsystem_id": "sub-1",
            "component_name": "绕组", "citations": [good],
        }],
    }
    upd = rg.region_evidence_gate(state).update
    assert upd["evidence_assessment"]["tier"] == "sufficient"
    vX = next(v for v in upd["dimension_proposal"][0]["values"] if v["value_id"] == "vX")
    assert vX.get("region_basis", "region_specific") == "region_specific"
    flag_types = [f["flag_type"] for f in upd["validation_flags"]]
    assert "region_basis_unsupported" in flag_types
    flag = next(f for f in upd["validation_flags"]
                if f["flag_type"] == "region_basis_unsupported")
    assert "vX" in flag["description"]
    assert flag["severity"] == "blocking"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
