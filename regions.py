"""GCAM 区域注册表
==================================================================
从 5.GCAM Region.xlsx 加载 32 个 GCAM 区域及成员国映射，并叠加手写的
中文名/检索别名/查询语言元数据。国家级证据经 Sheet2 成员表归区
（如 Germany → EU-15），这是国家区分度在输入层的落点。

接口：
  GCAM_REGIONS            区域注册表列表
  normalize_region(text)  自由文本 → RegionInfo（精确ID/中文名/别名/成员国）
  country_to_region(cc)   国家名 → region_id
"""

from __future__ import annotations
import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
XLSX_PATH = _ROOT / "5.GCAM Region.xlsx"


@dataclass
class RegionInfo:
    region_id: str
    region_name_cn: str
    member_countries: list[str] = field(default_factory=list)
    query_aliases: list[str] = field(default_factory=list)
    query_lang: str = "both"  # zh / en / both

    @property
    def all_hit_terms(self) -> list[str]:
        """区域命中词：别名 + 全部成员国（大小写不敏感匹配用）"""
        return list(dict.fromkeys(
            [*self.query_aliases, self.region_name_cn, *self.member_countries]))


@dataclass(frozen=True)
class CountryInfo:
    """国家级研究对象；country_id 使用 GCAM 成员表中的规范英文国名。"""
    country_id: str
    country_name: str
    gcam_region_id: str
    query_aliases: tuple[str, ...] = ()
    query_lang: str = "both"

    @property
    def all_hit_terms(self) -> list[str]:
        return list(dict.fromkeys((self.country_id, self.country_name, *self.query_aliases)))


# 手写元数据：region_id -> (中文名, 额外检索别名, 查询语言)
# 检索别名为空时默认使用 [中文名, region_id]；主要成员国名由 all_hit_terms 覆盖。
_REGION_META: dict[str, tuple[str, list[str], str]] = {
    "Africa_Eastern": ("东非", ["Eastern Africa"], "en"),
    "Africa_Northern": ("北非", ["Northern Africa"], "en"),
    "Africa_Southern": ("非洲南部", ["Southern Africa"], "en"),
    "Africa_Western": ("西非", ["Western Africa", "Nigeria", "Ghana"], "en"),
    "Argentina": ("阿根廷", [], "en"),
    "Australia_NZ": ("澳大利亚新西兰", ["Australia", "New Zealand"], "en"),
    "Brazil": ("巴西", [], "en"),
    "Canada": ("加拿大", [], "en"),
    "Central America and the Caribbean": ("中美洲与加勒比", ["Central America", "Caribbean"], "en"),
    "Central Asia": ("中亚", ["Kazakhstan", "Uzbekistan"], "both"),
    "China": ("中国", ["China", "PRC"], "zh"),
    "Colombia": ("哥伦比亚", [], "en"),
    "EU-12": ("欧盟12国", ["EU-12", "Central Europe", "Poland"], "en"),
    "EU-15": ("欧盟15国", ["EU-15", "Western Europe", "European Union", "Germany", "France"], "en"),
    "Europe (Non EU)": ("欧洲非欧盟", ["Non-EU Europe", "Turkey", "Balkans"], "en"),
    "European Free Trade Association": ("欧洲自由贸易联盟", ["EFTA", "Switzerland", "Norway"], "en"),
    "India": ("印度", [], "en"),
    "Indonesia": ("印度尼西亚", ["Indonesia"], "en"),
    "Japan": ("日本", [], "en"),
    "Mexico": ("墨西哥", [], "en"),
    "Middle East": ("中东", ["Saudi Arabia", "UAE", "GCC"], "en"),
    "Pakistan": ("巴基斯坦", [], "en"),
    "Russia": ("俄罗斯", ["Russia", "Russian Federation"], "en"),
    "South Africa": ("南非", ["South Africa"], "en"),
    "South America_Northern": ("南美北部", ["Northern South America", "Venezuela"], "en"),
    "South America_Southern": ("南美南部", ["Southern South America", "Chile", "Andes"], "en"),
    "South Asia": ("南亚", ["South Asia", "Bangladesh", "Sri Lanka"], "both"),
    "South Korea": ("韩国", ["South Korea", "Korea"], "en"),
    "Southeast Asia": ("东南亚", ["Southeast Asia", "ASEAN", "Vietnam", "Thailand"], "both"),
    "Taiwan": ("中国台湾", ["Taiwan"], "zh"),
    "Ukraine": ("乌克兰", [], "en"),
    "USA": ("美国", ["USA", "United States", "US", "America"], "en"),
}


def _load_members() -> tuple[dict[str, list[str]], dict[str, str]]:
    """从 xlsx 的 Sheet2 长表加载 region→成员国 与 国家→region 映射。

    xlsx 缺失/损坏时回退为从 Sheet1 风格的空表（成员国仅靠别名兜底）。
    """
    members: dict[str, list[str]] = {}
    country_map: dict[str, str] = {}
    if not XLSX_PATH.exists():
        return members, country_map
    try:
        import openpyxl
        wb = openpyxl.load_workbook(XLSX_PATH, read_only=True)
        ws = wb["Sheet2"] if "Sheet2" in wb.sheetnames else wb.worksheets[-1]
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0 or not row or not row[0]:
                continue
            rid, country = str(row[0]).strip(), str(row[1] or "").strip()
            if not country:
                continue
            members.setdefault(rid, []).append(country)
            country_map[country.casefold()] = rid
    except Exception:
        pass
    return members, country_map


_MEMBERS, _COUNTRY_MAP = _load_members()

# 常用中文输入别名。规范国家全集仍来自 GCAM Sheet2，避免另建一套国家清单。
_COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "China": ("中国", "中华人民共和国", "PRC"),
    "United States": ("美国", "美利坚合众国", "USA", "United States of America", "US", "U.S."),
    "Germany": ("德国",), "France": ("法国",), "United Kingdom": ("英国", "UK"),
    "Japan": ("日本",), "South Korea": ("韩国", "Republic of Korea"),
    "India": ("印度",), "Brazil": ("巴西",), "Canada": ("加拿大",),
    "Australia": ("澳大利亚", "澳洲"), "Russia": ("俄罗斯", "Russian Federation"),
    "South Africa": ("南非",), "Mexico": ("墨西哥",),
    "Indonesia": ("印度尼西亚", "印尼"), "Taiwan": ("中国台湾", "台湾"),
}

COUNTRIES: list[CountryInfo] = []
_COUNTRY_BY_KEY: dict[str, CountryInfo] = {}
for _country_key, _region_id in sorted(_COUNTRY_MAP.items()):
    # _COUNTRY_MAP 的 key 已 casefold；从成员表取回规范拼写。
    _canonical = next((c for c in _MEMBERS.get(_region_id, [])
                       if c.casefold() == _country_key), _country_key)
    _aliases = _COUNTRY_ALIASES.get(_canonical, ())
    _lang = "zh" if _canonical in {"China", "Taiwan"} else "en"
    _info = CountryInfo(_canonical, _aliases[0] if _aliases else _canonical,
                        _region_id, _aliases, _lang)
    COUNTRIES.append(_info)
    for _key in (_canonical, *_aliases):
        _COUNTRY_BY_KEY.setdefault(_key.strip().casefold(), _info)


GCAM_REGIONS: list[RegionInfo] = []
for _rid in sorted(set(_REGION_META) | set(_MEMBERS)):
    cn, extra_aliases, lang = _REGION_META.get(
        _rid, (_rid, [], "both"))
    GCAM_REGIONS.append(RegionInfo(
        region_id=_rid,
        region_name_cn=cn,
        member_countries=_MEMBERS.get(_rid, []),
        query_aliases=list(dict.fromkeys([cn, _rid, *extra_aliases])),
        query_lang=lang,
    ))

_REGION_BY_KEY: dict[str, RegionInfo] = {}
for _r in GCAM_REGIONS:
    for _k in [_r.region_id.casefold(), _r.region_name_cn,
               *[a.casefold() for a in _r.query_aliases]]:
        _REGION_BY_KEY.setdefault(_k, _r)


def normalize_region(text: str | None) -> RegionInfo | None:
    """自由文本 → RegionInfo：精确 region_id / 中文名 / 别名 / 成员国。
    未匹配返回 None（调用方按自由文本兜底处理）。"""
    if not text or not text.strip():
        return None
    key = text.strip().casefold()
    hit = _REGION_BY_KEY.get(key)
    if hit:
        return hit
    country = resolve_country(text)
    return next((r for r in GCAM_REGIONS if country and
                 r.region_id == country.gcam_region_id), None)


def resolve_country(text: str | None) -> CountryInfo | None:
    """精确解析国家名称或别名；禁止子串猜测，未识别时返回 None。"""
    if not text or not text.strip():
        return None
    return _COUNTRY_BY_KEY.get(text.strip().casefold())


def country_to_region(country: str | None) -> str | None:
    """国家名 → region_id（Sheet2 成员表映射，大小写不敏感）"""
    if not country:
        return None
    return _COUNTRY_MAP.get(country.strip().casefold())


def region_options() -> list[dict]:
    """前端下拉/ datalist 用：按中文名排序"""
    return [{"region_id": r.region_id, "name_cn": r.region_name_cn,
             "members": r.member_countries} for r in GCAM_REGIONS]


def country_options() -> list[dict]:
    """国家级研究输入选项。"""
    return [{"country_id": c.country_id, "country_name": c.country_name,
             "gcam_region_id": c.gcam_region_id} for c in COUNTRIES]
