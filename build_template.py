# -*- coding: utf-8 -*-
"""
生成《铜产品减量化模型 — 通用输出样式模板》

设计原则：
1. 6个小节标题在7种产品间完全一致，不嵌入产品专属的单位/基准值/措施编号范围
   （原文件Section 2/5/7标题都嵌入了这类信息，是标题不统一的根源）。
2. 用"适用范围/子类别"列取代"列复制"处理产品内部的子分类（如风电陆上/海上），
   使表结构与子类别数量无关，7种产品共用同一套列头。
3. Section 2 新增"分类维度名称/分类维度类别"两列，强制调研时先声明"这一组型号
   是按哪个维度分类的"，防止像原Wind表那样把"应用场景"混进"技术路线"列。
4. 不含原文件的"7. 减量化计算参数"——按你的要求移除；具体理由见README。
5. Section 6 强度值用公式从Section 5的Δmax和实现率计算，不硬编码结果。
"""
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

FONT_NAME = "Arial"
TITLE_FONT = Font(name=FONT_NAME, size=13, bold=True, color="FFFFFF")
SECTION_FONT = Font(name=FONT_NAME, size=11, bold=True, color="FFFFFF")
HEADER_FONT = Font(name=FONT_NAME, size=9, bold=True)
NOTE_FONT = Font(name=FONT_NAME, size=9, italic=True, color="595959")
BODY_FONT = Font(name=FONT_NAME, size=9)
FORMULA_FONT = Font(name=FONT_NAME, size=9, color="0000FF")  # 蓝=公式自动计算(依skill规范:蓝=硬编码输入; 此处沿用原文件"蓝=公式"约定，见README说明)

TITLE_FILL = PatternFill("solid", fgColor="1F4E78")
SECTION_FILL = PatternFill("solid", fgColor="2E75B6")
HEADER_FILL = PatternFill("solid", fgColor="D9E1F2")
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")   # 黄=需要调研填写
FORMULA_FILL = PatternFill("solid", fgColor="E2EFDA")  # 绿=公式自动计算
FIXED_FILL = PatternFill("solid", fgColor="F2F2F2")    # 灰=固定结构/说明
EXAMPLE_FILL = PatternFill("solid", fgColor="FCE4D6")  # 橙=示例数据(非实际调研结果)

THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def style_cell(cell, font=BODY_FONT, fill=None, align=None, border=True):
    cell.font = font
    if fill:
        cell.fill = fill
    if align:
        cell.alignment = align
    else:
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    if border:
        cell.border = BORDER


def write_row(ws, row, values, font=BODY_FONT, fill=None, start_col=1):
    for i, v in enumerate(values):
        c = ws.cell(row=row, column=start_col + i, value=v)
        style_cell(c, font=font, fill=fill)
    return row + 1


def section_header(ws, row, text, ncols):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncols)
    c = ws.cell(row=row, column=1, value=text)
    style_cell(c, font=SECTION_FONT, fill=SECTION_FILL,
               align=Alignment(vertical="center", horizontal="left"))
    ws.row_dimensions[row].height = 20
    return row + 1


def field_row(ws, row, label, value_or_formula, ncols, is_formula=False, note=None):
    """左label(灰底) + 右value(横向合并到末列，黄底=待填 或 绿底=公式)"""
    lc = ws.cell(row=row, column=1, value=label)
    style_cell(lc, font=HEADER_FONT, fill=FIXED_FILL)
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=ncols)
    vc = ws.cell(row=row, column=2, value=value_or_formula)
    style_cell(vc, font=(FORMULA_FONT if is_formula else BODY_FONT),
               fill=(FORMULA_FILL if is_formula else INPUT_FILL))
    if note:
        vc.comment = openpyxl.comments.Comment(note, "Template")
    return row + 1


def build_template_sheet(wb, sheet_name="Template"):
    ws = wb.create_sheet(sheet_name)
    ws.sheet_view.showGridLines = False
    NCOLS = 14
    for col in range(1, NCOLS + 1):
        ws.column_dimensions[get_column_letter(col)].width = 16
    ws.column_dimensions["C"].width = 26  # 措施/情景定义等长文本列
    ws.column_dimensions["D"].width = 22

    row = 1
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NCOLS)
    tc = ws.cell(row=row, column=1, value="＜产品名称＞铜减量化模型")
    style_cell(tc, font=TITLE_FONT, fill=TITLE_FILL,
               align=Alignment(vertical="center", horizontal="center"))
    ws.row_dimensions[row].height = 24
    row += 1

    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NCOLS)
    note = (
        "填写说明：黄色＝需要调研/填写的数据；灰色＝固定结构或标签；"
        "橙色＝示例数据(演示格式用，非实际调研结果，正式使用前请删除或替换)。"
        "本模板的6个小节标题与列头在全部7种产品间保持一致，不因产品不同而改写标题或增减列；"
        "产品内部的子类别差异(如风电陆上/海上、电动车BEV/PHEV)一律通过“适用范围/子类别”列的不同行来表达，"
        "不通过新增专属列或修改标题实现——这样无论某产品有0个还是多个子类别，表结构都不需要改变。"
    )
    nc = ws.cell(row=row, column=1, value=note)
    style_cell(nc, font=NOTE_FONT, fill=FIXED_FILL)
    ws.row_dimensions[row].height = 46
    row += 2

    # ============================================================
    # Section 1: 研究对象、边界与口径
    # ============================================================
    row = section_header(ws, row, "1. 研究对象、边界与口径", NCOLS)
    row = field_row(ws, row, "产品名称", "＜7种产品之一，如：光伏电站＞", NCOLS)
    row = field_row(ws, row, "研究区域", "＜如：中国＞", NCOLS)
    row = field_row(ws, row, "情景时间范围", "＜如：2025–2035＞", NCOLS)
    row = field_row(ws, row, "单位铜强度口径", "＜功能单位定义，如：t Cu/MWp_DC，需说明归一化依据＞", NCOLS)
    row = field_row(ws, row, "产品系统边界/统计口径",
                     "＜明确纳入/不纳入哪些子系统，边界要能被后续Section2-4复现＞", NCOLS)
    row = field_row(ws, row, "情景作用对象", "＜如：当年新建的该产品＞", NCOLS)
    row = field_row(ws, row, "减量化的定义",
                     "＜措施如何先算绝对减铜量、再除以何种基准得到可比的减量潜力＞", NCOLS)
    row = field_row(ws, row, "关键建模口径",
                     "＜基准强度取值范围、高低值参考依据，避免与2035目标年混淆＞", NCOLS)
    row += 1

    # ============================================================
    # Section 2: 主流产品分类与代表型号
    # ============================================================
    row = section_header(ws, row, "2. 主流产品分类与代表型号", NCOLS)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NCOLS)
    warn = ws.cell(row=row, column=1, value=(
        "【重要】同一产品可能存在多个独立的分类维度(如技术路线、外形规格、容量等级、应用场景等)。"
        "请先在“分类维度名称”列写清楚这一组型号是按哪一个角度划分的，再列出该维度下的所有取值。"
        "同一分类维度下的取值必须严格属于同一个分类角度——例如“技术路线”维度下不能混入按"
        "“应用场景”才能区分的取值(反面案例见下方橙色示例行)。若某组取值实际上跨了两个角度，"
        "应拆分成两个独立的分类维度分别列出。"
    ))
    style_cell(warn, font=NOTE_FONT, fill=FIXED_FILL)
    ws.row_dimensions[row].height = 46
    row += 1

    headers2 = ["分类维度名称", "分类维度类别", "型号/取值名称", "关键额定参数/容量",
                "主要制造商/代表厂商", "市场占有率(%)", "占有率口径", "占有率年份",
                "市场占有率来源名称", "来源链接", "数据质量", "代表性选择理由", "口径/边界说明"]
    row = write_row(ws, row, headers2, font=HEADER_FONT, fill=HEADER_FILL)

    # 正例示例(黄色区应填内容以橙色示例呈现)
    row = write_row(ws, row, [
        "电池技术路线", "technology_route", "TOPCon晶硅组件", "额定功率635W；效率23.5%",
        "通威等", 87.6, "中国电池技术路线市场占比", 2025,
        "中国光伏行业协会路线图", "＜URL＞", "A-/B+", "当前主流技术路线", "市场占比为电池技术路线份额",
    ], font=BODY_FONT, fill=EXAMPLE_FILL)

    row = write_row(ws, row, [
        "应用场景", "application_scenario", "陆上风力发电机组", "2025全球新增平均6.16MW",
        "Vestas/金风等", 94.9, "2024年中国新增风电场景划分", 2024,
        "国家能源局并网运行数据", "＜URL＞", "A", "中国新增风电绝大部分为陆上", "按建设场景统计",
    ], font=BODY_FONT, fill=EXAMPLE_FILL)

    row = write_row(ws, row, [
        "❌反面示例：技术路线", "technology_route",
        "❌陆上风力发电机组(实际是应用场景取值，混入了技术路线维度)", "－", "－", None,
        "－", None, "－", "－", "－", "此行演示axis_mismatch错误，正式使用时应删除", "－",
    ], font=Font(name=FONT_NAME, size=9, italic=True, color="C00000"), fill=EXAMPLE_FILL)
    row += 1

    # ============================================================
    # Section 3: 含铜部位清单与单位铜强度证据
    # ============================================================
    row = section_header(ws, row, "3. 含铜部位清单与单位铜强度证据", NCOLS)
    headers3 = ["型号/取值名称", "适用范围/子类别", "功能子系统/部件名称",
                "含铜部件清单及含铜量", "部件铜量单位", "该型号的铜使用强度(基准)",
                "强度单位", "案例/数据来源标题", "数据来源网址", "高铜基准构成权重(%)"]
    row = write_row(ws, row, headers3, font=HEADER_FONT, fill=HEADER_FILL)
    row = write_row(ws, row, [
        "TOPCon晶硅组件(研究基准)", "(无子类别)", "组件本体",
        "互联焊带0.3559＋汇流条0.0709＋其余铜0.1465", "t Cu/MWp_DC",
        0.5733, "t Cu/MWp_DC", "＜数据来源标题＞", "＜URL＞", 14.8,
    ], font=BODY_FONT, fill=EXAMPLE_FILL)
    row = write_row(ws, row, [
        "陆上风力发电机组(研究基准)", "陆上", "发电机+电气设备+塔筒",
        "发电机0.715＋电气设备0.12＋塔筒0.19", "t Cu/MW",
        1.025, "t Cu/MW", "＜数据来源标题＞", "＜URL＞", 100,
    ], font=BODY_FONT, fill=EXAMPLE_FILL)
    row += 1
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NCOLS)
    bl_note = ws.cell(row=row, column=1, value=(
        "高铜技术参考基准(用于统一比较减铜潜力，按“适用范围/子类别”分别设定): "
        "＜子类别1＞基准值＝＿＿＿；＜子类别2＞基准值＝＿＿＿(无子类别的产品只需一行)。"
        "高值为技术比较分母，不代表市场平均。"
    ))
    style_cell(bl_note, font=NOTE_FONT, fill=INPUT_FILL)
    ws.row_dimensions[row].height = 30
    row += 2

    # ============================================================
    # Section 4: 减量化措施清单
    # ============================================================
    row = section_header(ws, row, "4. 减量化措施清单", NCOLS)
    headers4 = ["措施ID", "减量化类别", "减量化措施", "减量原因", "作用对象(子系统/部件)",
                "适用范围/子类别", "绝对降铜量", "降铜量单位", "降低潜力(%)",
                "可叠加措施ID", "工程案例/科学依据来源", "适用条件/约束/备注", "证据等级/成熟度"]
    row = write_row(ws, row, headers4, font=HEADER_FONT, fill=HEADER_FILL)
    row = write_row(ws, row, [
        "R01", "结构优化设计", "＜措施描述＞", "＜减量机理＞", "＜子系统/部件名称＞",
        "(无子类别)", 0.1446, "t Cu/MWp_DC", 3.74,
        "R02;R05", "＜真实工程案例：项目/实施方/年份/效果/引用＞", "＜适用条件＞", "A/同行评审",
    ], font=BODY_FONT, fill=EXAMPLE_FILL)
    row = write_row(ws, row, [
        "R01", "材料替代", "＜措施描述＞", "＜减量机理＞", "塔筒内部固定主动力电缆",
        "陆上", 0.1317, "t Cu/MW", 6.9,
        "R02", "＜真实工程案例＞", "仅作用于固定塔筒段", "B-/商业产品+工程代理",
    ], font=BODY_FONT, fill=EXAMPLE_FILL)
    row = write_row(ws, row, [
        "R01", "材料替代", "(同一措施,子类别不同,数值不同→独立成行)", "＜同上＞", "塔筒内部固定主动力电缆",
        "海上", 0.1317, "t Cu/MW", 6.8,
        "R02", "＜真实工程案例＞", "海上无同口径BOM,采用陆上值作保守代理", "B-/工程代理",
    ], font=BODY_FONT, fill=EXAMPLE_FILL)
    row += 1

    # ============================================================
    # Section 5: 减量化情景定义
    # ============================================================
    row = section_header(ws, row, "5. 减量化情景定义", NCOLS)
    headers5 = ["情景代码", "情景名称", "情景定位/定义", "适用范围/子类别", "纳入措施ID",
                "满实施减量幅度Δmax(%)", "措施启动年", "目标实现年", "目标实现率(%)",
                "扩散方式", "情景依据", "备注", "参数来源/链接"]
    row = write_row(ws, row, headers5, font=HEADER_FONT, fill=HEADER_FILL)
    row = write_row(ws, row, [
        "S0", "基准情景(不减量)", "高铜技术参考配置，不额外实施减铜措施", "(无子类别)",
        "无", 0, 2025, 2025, 0, "不适用", "＜情景依据＞", "＜备注＞", "＜链接＞",
    ], font=BODY_FONT, fill=EXAMPLE_FILL)
    row = write_row(ws, row, [
        "S1", "普通减量", "＜情景定义＞", "陆上",
        "R01", 6.9, 2025, 2035, 1, "线性扩散", "＜情景依据＞", "＜备注＞", "＜链接＞",
    ], font=BODY_FONT, fill=EXAMPLE_FILL)
    row = write_row(ws, row, [
        "S1", "普通减量", "＜同一情景,子类别不同,幅度不同→独立成行＞", "海上",
        "R01", 6.8, 2025, 2035, 1, "线性扩散", "＜情景依据＞", "＜备注＞", "＜链接＞",
    ], font=BODY_FONT, fill=EXAMPLE_FILL)
    sec5_start_row = row - 3  # 记录本节数据起始行(不含表头)
    row += 1

    # ============================================================
    # Section 6: 单位铜强度变化路径（基准年–2035）
    # ============================================================
    row = section_header(ws, row, "6. 单位铜强度变化路径（基准年–2035）", NCOLS)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NCOLS)
    calc_note = ws.cell(row=row, column=1, value=(
        "本节数据由后端程序批量计算后写入，不在Excel内用公式呈现计算过程"
        "(计算逻辑由quant_agent+integration_node在代码中实现，Excel仅作结果展示)。"
        "本节按“年份 × 子类别 × 情景代码”长表形式逐行列示，而非按子类别/情景横向展开列——"
        "这样无论某产品有几个子类别、几套情景，都不需要新增列，只需要新增行。"
    ))
    style_cell(calc_note, font=NOTE_FONT, fill=FIXED_FILL)
    ws.row_dimensions[row].height = 32
    row += 1

    headers6 = ["年份", "适用范围/子类别", "情景代码", "情景实现率(%)",
                "单位铜强度", "强度单位", "较基准ΔCu", "备注"]
    row = write_row(ws, row, headers6, font=HEADER_FONT, fill=HEADER_FILL)

    demo_rows = [
        (2025, "陆上", "S1", 0, 1.9, "t Cu/MW", 0, "基准年"),
        (2026, "陆上", "S1", 10, 1.887, "t Cu/MW", -0.013, ""),
        (2027, "陆上", "S1", 20, 1.874, "t Cu/MW", -0.026, ""),
    ]
    for vals in demo_rows:
        row = write_row(ws, row, list(vals), font=BODY_FONT, fill=EXAMPLE_FILL)

    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NCOLS)
    ext_note = ws.cell(row=row, column=1, value=
        "↑以上3行为格式示例(橙色)，实际交付时由程序为每个年份×子类别×情景组合写入一行，"
        "延展至2035年，并删除本行提示文字。")
    style_cell(ext_note, font=NOTE_FONT, fill=FIXED_FILL)
    row += 1

    ws.freeze_panes = "A4"
    return ws


def build_readme_sheet(wb):
    ws = wb.create_sheet("使用说明", 0)
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 90

    rows = [
        ("《铜产品减量化模型》通用输出样式模板", None, TITLE_FONT, TITLE_FILL),
        ("", None, None, None),
        ("与原手工版的三点结构性差异", None, SECTION_FONT, SECTION_FILL),
        ("1. 标题统一",
         "6个小节标题在7种产品间完全一致，不再嵌入产品专属的单位/基准值(原PV表Section5标题含"
         "“3.8611 t Cu/MWp_DC”，Wind表Section7标题含“R01–R02”)。产品专属信息放进表格内容，不放进标题。",
         HEADER_FONT, HEADER_FILL),
        ("2. 子类别用行不用列",
         "原Wind表用“列复制”表达陆上/海上(如“单位铜消耗绝对降低量(陆上)”/“(海上)”两列)，导致列数"
         "随子类别数量膨胀，7种产品无法共用列结构。新模板统一新增“适用范围/子类别”列，子类别差异"
         "体现为不同的行，列头永远相同。没有子类别的产品(如光伏站)该列填“(无子类别)”即可。",
         HEADER_FONT, HEADER_FILL),
        ("3. 分类维度显式声明",
         "Section2新增“分类维度名称”“分类维度类别”两列，强制先声明“这组型号是按哪个角度划分的”。"
         "原Wind表Section2列头写“产品种类/技术路线”，但实际填的“陆上/海上”是应用场景而非技术路线——"
         "这正是同轴混淆的真实案例，模板用反面示例行(红字标注)提醒这一类错误。",
         HEADER_FONT, HEADER_FILL),
        ("", None, None, None),
        ("为什么移除了原“7. 减量化计算参数”", None, SECTION_FONT, SECTION_FILL),
        ("按你的要求移除",
         "原表第7部分(P01–P20等)是支撑第4/6部分计算的中间参数。你已明确后续不再用Excel做计算，"
         "改为接入程序(LangGraph pipeline)完成计算，因此本模板不含计算参数，也不含任何计算公式——"
         "所有数值(包括Section6的强度路径)均由quant_agent+integration_node在代码中算出后，"
         "直接把结果值写入Excel，Excel在这里只承担“结果呈现”的角色，不承担“计算”的角色。",
         BODY_FONT, None),
        ("", None, None, None),
        ("与LangGraph图/schema的字段对应关系", None, SECTION_FONT, SECTION_FILL),
        ("Section 1", "对应 ProductResearchOutput 的顶层元信息 + 各schema的functional_unit相关字段", BODY_FONT, None),
        ("Section 2", "对应 ClassificationDimension + DimensionValue（“分类维度名称”=dimension_name，"
                       "“分类维度类别”=dimension_category，“型号/取值名称”=value_name）", BODY_FONT, None),
        ("Section 3", "对应 FunctionalSubsystem + CopperComponent（基准强度部分对应baseline_unit_intensity）", BODY_FONT, None),
        ("Section 4", "对应 CopperReductionMeasure + EngineeringCase（新增applicable_scope字段"
                       "承接“适用范围/子类别”列）", BODY_FONT, None),
        ("Section 5", "对应新增的 Scenario 类（scenario_id/scenario_name/applicable_scope/"
                       "included_measure_ids/full_implementation_reduction_pct/measure_start_year/"
                       "target_achievement_year/target_achievement_rate_pct/diffusion_method）", BODY_FONT, None),
        ("Section 6", "对应 IntensityTrajectoryPoint（已改为scenario_id外键关联Scenario，"
                       "不再用粗粒度枚举），数值由代码计算后直接写入，不在Excel中用公式呈现", BODY_FONT, None),
        ("", None, None, None),
        ("颜色约定", None, SECTION_FONT, SECTION_FILL),
        ("黄色", "需要调研/填写的数据", BODY_FONT, INPUT_FILL),
        ("灰色", "固定结构或说明文字", BODY_FONT, FIXED_FILL),
        ("橙色", "示例数据，仅演示格式，正式使用前请删除或替换为真实调研结果"
                 "(Section6的最终交付版本中，此列数据由程序计算写入，非人工示例)", BODY_FONT, EXAMPLE_FILL),
    ]
    r = 1
    for label, desc, font, fill in rows:
        if label == "" and desc is None:
            r += 1
            continue
        lc = ws.cell(row=r, column=1, value=label)
        style_cell(lc, font=font or BODY_FONT, fill=fill)
        if desc:
            dc = ws.cell(row=r, column=2, value=desc)
            style_cell(dc, font=BODY_FONT, fill=fill)
            ws.row_dimensions[r].height = max(15, 14 * (len(desc) // 55 + 1))
        else:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
        r += 1
    return ws


if __name__ == "__main__":
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    build_readme_sheet(wb)
    build_template_sheet(wb, "Template")
    out_path = "/home/claude/copper_template/铜产品减量化模型_通用输出模板.xlsx"
    wb.save(out_path)
    print(f"saved to {out_path}")
