"""运行结果 Excel 导出
==================================================================
把 run 的 final_output（ProductResearchOutput 或 ProductReductionModel
的 model_dump）导出为符合《铜产品减量化模型_通用输出模板》6小节结构的
xlsx 文件：
  - 调研 run：Section 1 概览 / Section 2 分类维度 / Section 3 子系统与含铜部位
  - 减量化 run：追加 Section 4 措施清单 / Section 5 情景定义 / Section 6 强度路径
配色约定与 build_template.py 一致（黄=待填数据、灰=结构、橙=示例）。
"""

from __future__ import annotations
import io

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

FONT_NAME = "Arial"
TITLE_FONT = Font(name=FONT_NAME, size=13, bold=True, color="FFFFFF")
SECTION_FONT = Font(name=FONT_NAME, size=11, bold=True, color="FFFFFF")
HEADER_FONT = Font(name=FONT_NAME, size=9, bold=True)
BODY_FONT = Font(name=FONT_NAME, size=9)

TITLE_FILL = PatternFill("solid", fgColor="1F4E78")
SECTION_FILL = PatternFill("solid", fgColor="2E75B6")
HEADER_FILL = PatternFill("solid", fgColor="D9E1F2")
FIXED_FILL = PatternFill("solid", fgColor="F2F2F2")

THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _style(cell, font=BODY_FONT, fill=None):
    cell.font = font
    if fill:
        cell.fill = fill
    cell.alignment = Alignment(vertical="center", wrap_text=True)
    cell.border = BORDER


def _section(ws, row, text, ncols):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncols)
    c = ws.cell(row=row, column=1, value=text)
    _style(c, font=SECTION_FONT, fill=SECTION_FILL)
    ws.row_dimensions[row].height = 20
    return row + 1


def _header_row(ws, row, headers):
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=i, value=h)
        _style(c, font=HEADER_FONT, fill=HEADER_FILL)
    return row + 1


def _data_row(ws, row, values):
    for i, v in enumerate(values, start=1):
        c = ws.cell(row=row, column=i, value=v if v is not None else "－")
        _style(c)
    return row + 1


def _quant(q) -> str:
    if not isinstance(q, dict):
        return "－"
    v = q.get("value")
    u = q.get("unit", "")
    return f"{v} {u}".strip()


def export_run_to_xlsx(run: dict) -> bytes:
    result = run.get("result") or {}
    is_reduction = "reduction_measures" in result

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("调研成果" if not is_reduction else "减量化模型")
    ws.sheet_view.showGridLines = False

    ncols = 10 if not is_reduction else 13
    for col in range(1, ncols + 1):
        ws.column_dimensions[get_column_letter(col)].width = 18

    # 标题
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    tc = ws.cell(row=1, column=1,
                 value=f"{run['product_name']} 铜减量化模型"
                       f"（{'调研+减量化' if is_reduction else '调研阶段'}）")
    _style(tc, font=TITLE_FONT, fill=TITLE_FILL)
    tc.alignment = Alignment(vertical="center", horizontal="center")
    ws.row_dimensions[1].height = 24
    row = 3

    # -------- Section 1 概览 --------
    row = _section(ws, row, "1. 研究对象、边界与口径", ncols)
    meta = [
        ("产品名称", run["product_name"]),
        ("研究区域", run["region"]),
        ("调研基准年", run["baseline_year"]),
        ("补充约束", run["constraints"] or "无"),
        ("运行模式", "在线LLM调研" if run["llm_mode"] == "online" else "演示模式（占位数据）"),
        ("运行ID", run["run_id"]),
    ]
    if is_reduction:
        meta.append(("分析运行ID", result.get("analysis_run_id", "")))
        meta.append(("依据调研版本", f"v{result.get('based_on_research_version', '')}"))
        meta.append(("功能单位", result.get("chosen_functional_unit", "")))
        meta.append(("基准强度", _quant(result.get("baseline_unit_intensity"))))
    if not is_reduction and result.get("research_status"):
        meta.append(("成果状态", result.get("research_status")))
        meta.append(("审核人", result.get("approved_by") or "－"))
    for label, value in meta:
        lc = ws.cell(row=row, column=1, value=label)
        _style(lc, font=HEADER_FONT, fill=FIXED_FILL)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=ncols)
        vc = ws.cell(row=row, column=2, value=value)
        _style(vc)
        row += 1
    row += 1

    # -------- Section 2 分类维度（仅调研成果含此节） --------
    if "classification_dimensions" in result:
        row = _section(ws, row, "2. 主流产品分类与代表型号（分类维度体系）", ncols)
        row = _header_row(ws, row, [
            "维度ID", "分类维度名称", "分类维度类别", "分类轴定义(axis_of_variation)",
            "取值ID", "型号/取值名称", "轴符合性说明", "市场地位描述",
            "典型规格范围", "引用数",
        ])
        for dim in result["classification_dimensions"]:
            for v in dim.get("values", []):
                row = _data_row(ws, row, [
                    dim.get("dimension_id"), dim.get("dimension_name"),
                    dim.get("dimension_category"), dim.get("axis_of_variation"),
                    v.get("value_id"), v.get("value_name"),
                    v.get("axis_conformity_justification"),
                    v.get("prevalence_desc") or "－",
                    v.get("typical_spec_range") or "－",
                    len(v.get("citations", [])),
                ])
        row += 1

        # -------- Section 3 功能子系统与含铜部位 --------
        row = _section(ws, row, "3. 功能子系统分解（结构树）", ncols)
        row = _header_row(ws, row, [
            "子系统ID", "子系统名称", "层级", "父级子系统", "功能描述",
            "随维度变化说明", "质量占比描述", "引用数", "", "",
        ][:10])
        for s in result["functional_subsystems"]:
            row = _data_row(ws, row, [
                s.get("subsystem_id"), s.get("subsystem_name"),
                s.get("decomposition_level"), s.get("parent_subsystem_id") or "－",
                s.get("function_description"),
                "、".join(s.get("varies_by_dimension_ids", [])) or "－",
                s.get("mass_share_desc") or "－", len(s.get("evidence", [])),
            ])
        row += 1

        row = _section(ws, row, "3b. 含铜部位清单与单位铜强度证据", ncols)
        row = _header_row(ws, row, [
            "部件ID", "部件名称", "所属子系统", "铜形态", "选用铜的原因",
            "单位质量", "质量数据依据", "适用维度取值", "可替代标记", "引用数",
        ])
        for c in result["copper_components"]:
            row = _data_row(ws, row, [
                c.get("component_id"), c.get("component_name"),
                c.get("parent_subsystem_id"), c.get("copper_form"),
                c.get("function_of_copper"), _quant(c.get("unit_mass")),
                c.get("mass_data_basis"),
                str(c.get("applies_to_dimension_values") or "全部取值通用"),
                {True: "是", False: "否", None: "未知"}.get(c.get("substitutable")),
                len(c.get("citations", [])),
            ])
        row += 1

    # -------- 减量化 run 追加 Section 4-6 --------
    if is_reduction:
        row = _section(ws, row, "4. 减量化措施清单", ncols)
        row = _header_row(ws, row, [
            "措施ID", "措施名称", "减量化类别", "技术机理", "适用范围/子类别",
            "作用部件", "预期降幅", "权衡", "成熟度", "最早可行年",
            "工程案例", "案例规模", "引用数",
        ])
        for m in result["reduction_measures"]:
            case = m.get("engineering_case") or {}
            row = _data_row(ws, row, [
                m.get("measure_id"), m.get("measure_name"),
                m.get("mechanism_category"), m.get("mechanism"),
                m.get("applicable_scope") or "(无子类别)",
                "、".join(m.get("target_component_ids", [])),
                _quant(m.get("expected_reduction")),
                m.get("trade_offs") or "－", m.get("maturity"),
                m.get("earliest_feasible_year"),
                f"{case.get('project_or_product_name', '')} / "
                f"{case.get('implementing_entity', '')} / {case.get('year', '')}",
                case.get("scale"), len(m.get("additional_citations", [])),
            ])
        row += 1

        row = _section(ws, row, "5. 减量化情景定义（S0-S3统一框架）", ncols)
        row = _header_row(ws, row, [
            "情景代码", "情景名称", "适用范围/子类别", "情景定义", "纳入措施ID",
            "满实施减量幅度Δmax", "措施启动年", "目标实现年", "目标实现率(%)",
            "扩散方式", "情景依据",
        ])
        for s in result["scenarios"]:
            row = _data_row(ws, row, [
                s.get("scenario_id"), s.get("scenario_name"),
                s.get("applicable_scope") or "(无子类别)",
                s.get("scenario_definition"),
                "、".join(s.get("included_measure_ids", [])) or "无",
                _quant(s.get("full_implementation_reduction_pct")),
                s.get("measure_start_year"), s.get("target_achievement_year"),
                s.get("target_achievement_rate_pct"), s.get("diffusion_method"),
                s.get("scenario_rationale"),
            ])
        row += 1

        row = _section(ws, row, "6. 单位铜强度变化路径（基准年–2035）", ncols)
        row = _header_row(ws, row, [
            "年份", "情景代码", "适用范围/子类别", "情景实现率(%)",
            "单位铜强度", "功能单位", "较基准ΔCu", "纳入措施",
        ])
        points = sorted(result["trajectory"], key=lambda p: (p["scenario_id"], p["year"]))
        for p in points:
            row = _data_row(ws, row, [
                p.get("year"), p.get("scenario_id"),
                p.get("applicable_scope") or "(无子类别)",
                p.get("scenario_realization_rate_pct"),
                _quant(p.get("unit_copper_intensity")), p.get("functional_unit"),
                _quant(p.get("delta_vs_baseline")),
                "、".join(p.get("applied_measure_ids", [])) or "－",
            ])

    # -------- 校验与质询（两类run都输出） --------
    row += 1
    row = _section(ws, row, "附：校验标记与质询记录", ncols)
    row = _header_row(ws, row, ["类型", "级别", "描述", "关联ID", "已解决", "", "", "", "", ""][:10])
    for f in result.get("validation_flags", []):
        row = _data_row(ws, row, [
            "校验标记", f.get("severity"), f.get("description"),
            "、".join(f.get("related_ids", [])), "是" if f.get("resolved") else "否",
        ])
    for o in result.get("objections", []):
        row = _data_row(ws, row, [
            f"质询({o.get('flag_type')})", o.get("severity"), o.get("detail"),
            "、".join(o.get("target_field_refs", [])),
            "已回应" if o.get("addressed") else "未回应",
        ])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
