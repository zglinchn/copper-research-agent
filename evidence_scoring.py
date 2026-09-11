"""证据分层与区域充分度评分（确定性、零 LLM）
==================================================================
为 region_evidence_gate（图A）与 retrieval.py 提供共享的确定性逻辑：
  - classify_source_tier(publisher, url)  来源权威分层（白名单域名）
  - citation_region_hit(citation, region) 单条引用是否命中目标区域词
  - compute_region_assessment(...)        五维评分→三档→证据口径决策

评分权重与阈值集中在常量区，可按运行经验调整：
  s1 区域特异性40% / s2 来源权威25% / s3 来源多样性15% / s4 时效10% / s5 覆盖10%
  ≥70 sufficient → region_specific；40-69 partial → mixed；<40 insufficient → global_proxy
"""

from __future__ import annotations
import re
from datetime import datetime
from urllib.parse import urlparse

from regions import CountryInfo, RegionInfo, resolve_country
from schemas import RegionEvidenceAssessment

# ============================================================
# 来源权威白名单：权威机构/学术出版/政府/行业协会域名与出版方关键词
# ============================================================

AUTHORITATIVE_DOMAINS = (
    # 国际机构与研究机构
    "iea.org", "irena.org", "bnef.com", "nef.org", "energyinst.org",
    "worldbank.org", "oecd.org", "ireea", "unep.org", "itma",
    # 政府/标准机构
    "gov", "edu", "gbstandards", "standards", "iso.org", "iec.ch",
    # 学术出版
    "arxiv.org", "sciencedirect.com", "springer.com", "nature.com",
    "ieee.org", "mdpi.com", "wiley.com", "tandfonline.com", "iopscience",
    "acm.org", "aps.org", "rsc.org", "acs.org",
    # 企业/行业协会权威披露
    "schneider-electric.com", "se.com", "goldmansachs.com", "hitachienergy.com",
    "abb.com", "siemens.com", "nexans.com", "nkt.com", "prysmian.com",
    "copperalliance", "icsg.org", "cwea.org.cn", "cec.org.cn",
    "nea.gov.cn", "sgcc.com.cn", "cpiatj", "cpia",
)

MEDIUM_KEYWORDS = (
    "reuters", "bloomberg", "ft.com", "economist", "wsj", "nytimes",
    "财新", "澎湃", "界面新闻", "第一财经", "证券时报", "21经济",
    "statista", "pv-magazine", "windpower", "rechargenews", "electrive",
    "energy-utilities", "utilitydive", "greentechmedia",
)


def classify_source_tier(publisher: str = "", url: str = "") -> str:
    """authoritative / medium / low 三层（确定性、白名单优先）"""
    hay = f"{publisher or ''} {url or ''}".casefold()
    for d in AUTHORITATIVE_DOMAINS:
        if d in hay:
            return "authoritative"
    for kw in MEDIUM_KEYWORDS:
        if kw in hay:
            return "medium"
    return "low"


def _domain_of(url: str) -> str:
    try:
        netloc = urlparse(url or "").netloc.casefold()
        return netloc.split(":")[0]
    except Exception:
        return ""


def _contains_term(hay: str, term: str) -> bool:
    """按完整词组命中，避免 Austria 因包含 us 被识别为美国。"""
    if any(ord(ch) > 127 for ch in term):
        return term.casefold() in hay
    return bool(re.search(r"(?<![\w])" + re.escape(term.casefold()) + r"(?![\w])", hay))


def citation_region_hit(citation: dict, region: RegionInfo | CountryInfo | None,
                        evidence_country: str | None = None) -> bool:
    """单条引用是否命中目标区域：
    1. evidence_country 经成员国表归区命中本区域；
    2. 标题/出版方/URL/摘要中含区域别名或成员国名。
    region 为 None（自由文本区域）时用原始文本做子串匹配。"""
    if region is None:
        return False
    ec = evidence_country or citation.get("evidence_country")
    if ec:
        evidence_country_info = resolve_country(ec)
        if isinstance(region, CountryInfo):
            return bool(evidence_country_info and
                        evidence_country_info.country_id == region.country_id)
        from regions import country_to_region
        if country_to_region(ec) == region.region_id:
            return True
    if isinstance(region, CountryInfo):
        # 国家模型只接受来源显式声明的覆盖国家，标题关键词不能替代统计范围。
        return False
    hay = " ".join(str(citation.get(k) or "") for k in
                   ("title", "publisher", "url", "excerpt_note")).casefold()
    if not hay.strip():
        return False
    for term in region.all_hit_terms:
        if term and _contains_term(hay, term):
            return True
    return False


# ============================================================
# 五维评分与分档
# ============================================================

SCORE_WEIGHTS = {
    "s1_region_specificity": 0.40,
    "s2_source_authority": 0.25,
    "s3_source_diversity": 0.15,
    "s4_recency": 0.10,
    "s5_coverage": 0.10,
}
TIER_SUFFICIENT_AT = 70.0
TIER_PARTIAL_AT = 40.0
DIVERSITY_FULL_AT = 5          # 去重出版方/域名数≥5 记满分
RECENCY_WINDOW_YEARS = 5       # publish_date 在 baseline_year-5 年内记有效
RECENCY_ABSENT_NEUTRAL = 0.5   # 无日期时给中性分，不重复惩罚


def _year_of(date_str: str | None) -> int | None:
    if not date_str:
        return None
    m = re.search(r"(\d{4})", str(date_str))
    return int(m.group(1)) if m else None


def compute_region_assessment(citations: list[dict], region: RegionInfo | CountryInfo | None,
                              baseline_year: int | None = None,
                              coverage_score: float | None = None) -> RegionEvidenceAssessment:
    """对一次运行的引用集合做确定性评分。

    coverage_score：s5 覆盖度（0-100），由调用方传入（如 critic 评分或
    叶子子系统覆盖率折算）；缺省按 50 中性值。
    """
    baseline_year = baseline_year or datetime.now().year
    n = len(citations)
    if n == 0:
        return RegionEvidenceAssessment(
            region_id=(region.gcam_region_id if isinstance(region, CountryInfo) else
                       region.region_id if region else "unknown"),
            country_id=region.country_id if isinstance(region, CountryInfo) else None,
            score=0.0, tier="insufficient", component_scores={k: 0.0 for k in SCORE_WEIGHTS},
            evidence_policy="global_proxy", n_region_hits=0, n_citations=0,
            summary_note="无任何引用证据，强制采用全球主流型号替代口径",
        )

    hits, authoritative, years, sources = 0, 0, [], set()
    for c in citations:
        if citation_region_hit(c, region, c.get("evidence_country")):
            hits += 1
        if classify_source_tier(str(c.get("publisher") or ""),
                                str(c.get("url") or "")) == "authoritative":
            authoritative += 1
        y = _year_of(c.get("publish_date"))
        if y:
            years.append(y)
        src = str(c.get("publisher") or "") or _domain_of(str(c.get("url") or ""))
        if src:
            sources.add(src.casefold())

    s1 = hits / n * 100.0
    s2 = authoritative / n * 100.0
    s3 = min(len(sources), DIVERSITY_FULL_AT) / DIVERSITY_FULL_AT * 100.0
    if years:
        lo = baseline_year - RECENCY_WINDOW_YEARS
        s4 = sum(1 for y in years if y >= lo) / n * 100.0
    else:
        s4 = RECENCY_ABSENT_NEUTRAL * 100.0
    s5 = float(coverage_score) if coverage_score is not None else 50.0
    component = {
        "s1_region_specificity": round(s1, 1),
        "s2_source_authority": round(s2, 1),
        "s3_source_diversity": round(s3, 1),
        "s4_recency": round(s4, 1),
        "s5_coverage": round(s5, 1),
    }
    score = round(sum(component[k] * w for k, w in SCORE_WEIGHTS.items()), 1)

    if score >= TIER_SUFFICIENT_AT:
        tier, policy = "sufficient", "region_specific"
    elif score >= TIER_PARTIAL_AT:
        tier, policy = "partial", "mixed"
    else:
        tier, policy = "insufficient", "global_proxy"

    note = (f"引用{n}条，区域命中{hits}条，权威来源{authoritative}条，"
            f"去重来源{len(sources)}个；口径决策：{policy}"
            + ("" if tier == "sufficient" else
               "（区域证据不足的实体将标注 global_fallback 并采用全球主流型号替代）"))
    return RegionEvidenceAssessment(
        region_id=(region.gcam_region_id if isinstance(region, CountryInfo) else
                   region.region_id if region else "unknown"),
        country_id=region.country_id if isinstance(region, CountryInfo) else None,
        score=score, tier=tier, component_scores=component,
        evidence_policy=policy, n_region_hits=hits, n_citations=n,
        summary_note=note,
    )


def entity_has_region_hit(entity: dict, citation_key: str,
                          region: RegionInfo | CountryInfo | None) -> bool:
    """判断单个事实实体（维度取值/含铜部件）的引用是否存在区域命中"""
    if region is None:
        return False
    for c in entity.get(citation_key) or []:
        if citation_region_hit(c, region, c.get("evidence_country")):
            return True
    return False


def apply_region_fallback_marking(dimensions: list[dict], components: list[dict],
                                  region: RegionInfo | CountryInfo | None,
                                  evidence_policy: str) -> tuple[list[dict], list[dict]]:
    """按证据口径给事实实体写 region_basis（确定性标注，不重跑 agent）。

    - region_specific（sufficient）：保持默认，不逐条强制；
    - mixed（partial）/ global_proxy（insufficient）：逐实体检查引用，
      无区域命中 → region_basis=global_fallback + 全球主流替代说明，
      且数值口径同步切换为全球主流值并注明：
        * CopperComponent.regional_presence → None（全球默认=100%铜形态口径）
        * DimensionValue.market_share_caliber 追加“（全球主流口径兜底）”
    返回标注后的 (dimensions, components)。
    """
    if evidence_policy == "region_specific" or region is None:
        return dimensions, components

    note = "区域证据不足，采用全球主流型号替代" if evidence_policy == "global_proxy" \
        else "本实体引用无区域命中，采用全球主流型号替代（字段级兜底）"

    for dim in dimensions or []:
        for v in dim.get("values") or []:
            if not entity_has_region_hit(v, "citations", region):
                v["region_basis"] = "global_fallback"
                v["region_basis_note"] = v.get("region_basis_note") or note
                if v.get("market_share") is not None:
                    v["market_share_caliber"] = (
                        (v.get("market_share_caliber") or "市场占有率") + "（全球主流口径兜底）")
    for c in components or []:
        if not entity_has_region_hit(c, "citations", region):
            c["region_basis"] = "global_fallback"
            c["region_basis_note"] = c.get("region_basis_note") or note
            if c.get("regional_presence") is not None:
                # 全球主流型号替代口径：存在率回到全球默认（None=100%）
                c["regional_presence"] = None
    return dimensions, components


def find_region_unsupported(dimensions: list[dict], components: list[dict],
                            region: RegionInfo | CountryInfo | None) -> list[str]:
    """找出“声称 region_specific 却无任何区域命中引用”的实体（确定性审计）。

    供 region_evidence_gate 在 region_specific（sufficient）口径下生成
    region_basis_unsupported 校验标记；mixed/global_proxy 口径下实体已被
    兜底标注，不会出现在结果里。"""
    unsupported: list[str] = []
    for dim in dimensions or []:
        for v in dim.get("values") or []:
            if v.get("region_basis", "region_specific") == "region_specific" \
                    and not entity_has_region_hit(v, "citations", region):
                unsupported.append(f"维度取值 {v.get('value_id')}/{v.get('value_name')}")
    for c in components or []:
        if c.get("region_basis", "region_specific") == "region_specific" \
                and not entity_has_region_hit(c, "citations", region):
            unsupported.append(f"含铜部件 {c.get('component_id')}/{c.get('component_name')}")
    return unsupported
