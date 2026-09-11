import { canvasImage } from 'qoder/canvas';
import { Divider, Grid, H1, H2, H3, Stack, Stat, Table, Text, Row, Tag } from 'qoder/canvas';

const shot1 = canvasImage('./uicheck_step1_region_datalist.png');
const shot2 = canvasImage('./uicheck_step2_region_baseline.png');
const shot5 = canvasImage('./uicheck_step5_research_region_tab.png');

export default function SpecCompletionReport() {
  return (
    <Stack gap={20}>
      <H1>铜减量化模型：区域区分度 × 确定性计算 × 模板填数导出 — Spec 完成报告</H1>
      <Text tone="secondary">
        目标 Spec：铜模型国家区分度补充_task-514.md · 全部条目已实现并验证 · 2026-09-10
      </Text>

      <Grid columns={4} gap={16}>
        <Stat value="32 / 32" label="单元测试全绿" tone="positive" />
        <Stat value="3 / 3" label="分期（P1/P2/P3）完成" tone="positive" />
        <Stat value="12" label="新增/重构文件" />
        <Stat value="44" label="图B路径点（确定性计算）" />
      </Grid>

      <Divider />

      <H2>一、完成摘要</H2>
      <Stack gap={8}>
        <Text>· <b>区域区分度成为一等公民</b>：GCAM 32 区域注册表驱动双语区域化检索；五维证据充分度评分（区域特异性40/权威25/多样性15/时效10/覆盖10）决定证据口径；证据不足区域自动全球主流兜底并逐实体标注 region_basis。</Text>
        <Text>· <b>计算全部确定性化</b>：新增 calculate.py 承担基线强度、Δmax、逐年路径、绝对降铜量；LLM 只产出带引用的参数（BaselineParams/ScenarioParams），排序违规以参数级 objection 打回。</Text>
        <Text>· <b>减量前单位铜强度归位</b>：图B 新增 stage 0 baseline_quantification，模板 Section 3 的强度与构成权重由代码算出，消灭"0 kg 硬编码兜底"。</Text>
        <Text>· <b>导出即模板填数</b>：运行时加载通用输出模板，删除示例行、按模板列序逐字填数；reduction run 补齐 Section 2/3，修复缺漏飘移。</Text>
      </Stack>

      <H2>二、关键步骤</H2>
      <Table
        headers={['阶段', '内容', '关键证据']}
        rows={[
          ['P1 计算确定化', 'calculate.py + 图B重写（stage 0 基线 + calculation_node）+ schema 参数载体', 'test_calculate.py 9项、test_graphb_nodes.py 5项'],
          ['P2 模板填数导出', '两遍法 ops 序列重写 export_excel.py，模板为布局唯一事实源', 'test_export_template.py 4项（列头逐字一致/示例行删除/行数校验）'],
          ['P3 区域化证据链', 'regions.py 注册表 + evidence_scoring.py 评分 + 区域化检索 + region_evidence_gate + 前端', 'test_region_pipeline.py 9项'],
          ['完成审计补充', '旧run JSON兼容测试、DemoLLM新契约冒烟、critic覆盖度接线、region_basis_unsupported审计、兜底切全球主流值', 'test_compat_and_smoke.py 2项，累计32项全绿'],
        ]}
      />

      <H2>三、变更文件</H2>
      <Table
        headers={['文件', '类型', '说明']}
        rows={[
          ['calculate.py', '新增', '确定性计算层（基线/Δmax/扩散曲线/路径/排序预检），COMBINE_MODE 可切换组合规则'],
          ['regions.py', '新增', 'GCAM 32 区域注册表：中文名/成员国/检索别名/查询语言；country_to_region 归区'],
          ['evidence_scoring.py', '新增', '来源三层白名单、区域命中、五维评分三档、全球主流兜底标注'],
          ['schemas.py', '修改', 'region_id/region_basis/regional_presence/market_share；BaselineParams/ScenarioParams/CalculationParams/ModelBaselineIntensity/RegionEvidenceAssessment'],
          ['reduction_graph.py', '重写', 'stage 0 基线阶段 + 确定性 calculation_node + integration 缺产出即报错'],
          ['research_graph.py', '修改', 'region_evidence_gate 节点（人工审核前评分与兜底标注）'],
          ['webapp/export_excel.py', '重写', '模板填数导出（两遍法，规避合并区错位）'],
          ['webapp/retrieval.py', '修改', '双语区域查询模板、region_hit/source_tier 逐条标注、区域命中优先'],
          ['webapp/runner.py · demo_llm.py · server.py', '修改', '区域传递、新节点 PIPELINES、Demo 新契约、GET /api/regions'],
          ['webapp/static/*', '修改', '区域 datalist 下拉、区域证据卡/基线卡、新样式'],
          ['tests/*', '新增5个', 'test_calculate / test_graphb_nodes / test_export_template / test_region_pipeline / test_compat_and_smoke'],
        ]}
      />

      <H2>四、验证证据</H2>
      <H3>单元/节点级测试（32 项全绿）</H3>
      <Row gap={12}>
        <Tag tone="positive">test_calculate 9 通过</Tag>
        <Tag tone="positive">test_graphb_nodes 5 通过</Tag>
        <Tag tone="positive">test_export_template 4 通过</Tag>
      </Row>
      <Row gap={12}>
        <Tag tone="positive">test_region_pipeline 9 通过</Tag>
        <Tag tone="positive">test_compat_and_smoke 2 通过</Tag>
        <Tag tone="positive">回归（同轴一致性等）3 通过</Tag>
      </Row>
      <Text tone="secondary">覆盖：铝绕组份额40%→基线加权下降、构成权重和=100、kg/台→t/MWp 归一化换算、同部件连乘/跨部件加权、linear/s_curve/step 曲线性质、排序违规检出与参数级打回、模板列头逐字一致、Section 6 行数=11×4、德国→EU-15 归区、评分三档、global_fallback 兜底标注、旧 run JSON 反序列化兼容。</Text>

      <H3>浏览器实测（http://127.0.0.1:8010，注入合成完成 run，未发起任何图运行）</H3>
      <Grid columns={2} gap={16}>
        <Stack gap={6}>
          <Text size="small">① GCAM 区域下拉（/api/regions 驱动，32 区域，输入"欧"联想）</Text>
          <img src={shot1} alt="区域下拉验证" style={{ width: '100%', borderRadius: 8, border: '1px solid rgba(128,128,128,0.35)' }} />
        </Stack>
        <Stack gap={6}>
          <Text size="small">② 图B run「区域与基线」tab：证据充足 78.5 分徽章 + 基线 1.02 kg Cu/台（确定性计算）</Text>
          <img src={shot2} alt="图B区域与基线卡" style={{ width: '100%', borderRadius: 8, border: '1px solid rgba(128,128,128,0.35)' }} />
        </Stack>
      </Grid>
      <Stack gap={6}>
        <Text size="small">③ 调研 run 审核预览同款区域证据卡（口径 region_specific、区域命中 4/5、五项分项得分）</Text>
        <img src={shot5} alt="调研run区域与基线tab" style={{ width: '100%', maxWidth: 720, borderRadius: 8, border: '1px solid rgba(128,128,128,0.35)' }} />
      </Stack>
      <Text tone="secondary">另验证：导出 Excel 返回 200（合法 xlsx，11KB，无报错弹窗）；强度路径 tab 渲染 S0-S3 × 11 年共 44 行。</Text>

      <H2>五、结论与边界</H2>
      <Stack gap={8}>
        <Text>· Spec 全部条目已实现：P1 计算确定化、P2 模板填数导出、P3 区域化证据链，以及完成审计补充的 6 项缺口（旧JSON兼容测试、region_basis_unsupported 审计、兜底切全球主流值、critic 覆盖度接线、loader 权威读取、归一化/Demo 冒烟用例）。</Text>
        <Text>· 实施偏差（已在代码注释说明）：gate 产出的 region_basis_unsupported 采用 warning 级审计标记（确定性兜底标注已消除 blocking 风险，避免人工审核死锁）；区域下拉用 datalist 组合输入实现（等效 select 且保留自定义兜底）。</Text>
        <Text>· 按 Spec 测试计划约定，端到端在线运行（含 Tavily 区域检索）须先经用户批准；本轮已完成其要求的单元验证与 demo 冒烟。</Text>
      </Stack>
    </Stack>
  );
}
