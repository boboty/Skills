# 进销存台账 V1 (inventory-ledger-skill)

通用进销存技能, 供 **Claude Code / Codex / DSH** 等智能体调用。
自然语言 + 业务文件(CVS/XLSX)维护采购、销售、库存、调拨、月末成本;
**Excel 单文件持久化, 程序确定性计算, 全程可追溯**。

- AI 职责: 理解业务 → 生成结构化导入文件 → 预览/确认 → 汇报数字。
- 程序职责: 校验、月末一次加权平均成本、结账保护、幂等防重、审计。
- 明确不做: FIFO、多公司、多币种、退货、增值税/税务、凭证、ERP、Web/数据库(V2 候选)。

## 目录结构

```
inventory-ledger-skill/
├── SKILL.md                        # 技能入口(给智能体读)
├── references/
│   ├── business-rules.md           # 冻结业务规则 + 精度定义 + 异常清单 E01–E18
│   ├── workbook-schema.md          # 13 张工作表字段字典 + 原 Excel 32 列映射 + 口径脚注
│   └── workflows.md                # 操作工作流(导入/查询/结账/更正) + CLI 参考
├── scripts/
│   ├── cli.py                      # 命令行入口
│   └── ledgerlib/                  # 核心库(store/engine/biz/importer/queries/schema/decimal_utils)
├── tests/                          # pytest 自动化测试(AC1–AC8 + 单元)
├── examples/
│   ├── 进销存台账模板.xlsx          # 空台账模板(由 init 生成)
│   ├── import_templates/           # 各导入类型 CSV 模板
│   └── demo/                       # 模拟日常业务端到端演示(数据+脚本+输出)
└── requirements.txt
```

## 安装

1. 克隆/复制本目录到智能体的 skills 目录:
   - **Claude Code**: `~/.claude/skills/inventory-ledger-skill/`
   - **Codex**: `~/.codex/skills/`
   - **DSH**: 按 DSH 技能装载方式指向本目录
   (或直接在任何工作区使用绝对路径调用 `scripts/cli.py`。)
2. 依赖: `python3`(≥3.9) + `openpyxl`(≥3.1); 测试另需 `pytest`(≥8):
   ```bash
   python3 -m pip install -r requirements.txt
   ```

## 快速开始

```bash
# 1. 初始化台账(公司名/起始期间问用户, 不猜)
python3 scripts/cli.py init --path 进销存台账.xlsx --company "XX商贸" --start-period 2026-01

# 2. 建档
python3 scripts/cli.py sku add --path 进销存台账.xlsx --sku SKU-001 --name 示例商品 --unit 件
python3 scripts/cli.py wh  add --path 进销存台账.xlsx --wh W01 --name 主仓

# 3. 期初: 先预览后提交
python3 scripts/cli.py import --path 进销存台账.xlsx --file 期初.csv --type opening --preview
python3 scripts/cli.py import --path 进销存台账.xlsx --file 期初.csv --type opening --commit --confirm 用户确认

# 4. 日常采购/销售/调拨/调整/费用: 一律 preview -> commit
python3 scripts/cli.py import --path 进销存台账.xlsx --file 采购.csv --type purchase --preview
python3 scripts/cli.py import --path 进销存台账.xlsx --file 采购.csv --type purchase --commit --confirm 用户确认

# 5. 月末: 待确认成本 -> 费用分摊 -> 试结账 -> 正式结账 -> 开启下月
python3 scripts/cli.py query pending   --path 进销存台账.xlsx
python3 scripts/cli.py fee allocate   --path 进销存台账.xlsx --fee-id FEE-000001 --preview
python3 scripts/cli.py close dry      --path 进销存台账.xlsx --period 2026-01
python3 scripts/cli.py close final    --path 进销存台账.xlsx --period 2026-01 --confirm 用户确认
python3 scripts/cli.py period open    --path 进销存台账.xlsx --period 2026-02
```

完整操作工作流与错误处理见 `references/workflows.md`。

## 测试

```bash
python3 -m pytest tests -q        # 期望 57 passed
```
测试覆盖: AC1 多仓统一成本(10.80/140/40/180/756/1944)与 AC2 附加费用(11.20/784/2016)
精确数字、AC3 先销后购统一加权、AC4 调拨双边一致防重、AC5 成本更正重算、
AC6 结账保护/受控重开/级联重算、AC7 导入幂等/单据冲突、AC8 异常边界(E01–E18)、
Decimal 尾差最大余数法确定性、V1 定向修复回归(同单多费累计/跨期费用/更正继承/
导入原子与来源行号/成本内部全精度守恒)、数据完整性回归(批次内防重/费用登记前
关联校验/统一批次号关联 业务行-导入登记-审计)。

## 冻结口径摘要

- 成本按**公司+SKU 统一池**, 数量按 SKU+仓库; 单价 4 位 / 金额 2 位,
  ROUND_HALF_UP; 行级尾差最大余数法(行序=记录ID升序)。
- 月末加权 = (月初金额 + 计价入库金额 + 已确认成本调整) / (月初数量 + 计价入库数量);
  期末金额 = 池 − 出库成本(恒等守恒)。
- 业务流水是事实只增不删(更正=原行`已更正`+关联新行); 结存/成本是结果可重算。
- 结账硬保护, 重开须授权+原因并级联, 历史行保留版本递增。
- 异常明确报错不补全: 成本缺失→`待确认`并在结账前阻止(E04/E18)。

## 已知限制(V1, 均不实现, 列为 V2 候选)

1. **退货**不实现: 负数量或含"退货"字样的输入被拒绝(E11)。
2. FIFO/移动加权: 只做月末一次加权平均。
3. 单公司、单币种(人民币); 不支持多公司合并、外币。
4. 每 SKU 一个基本单位, 不做单位换算(箱/件/个互转)。
5. 不加增值税/税费判断, 不生成会计凭证, 不接 ERP。
6. 无数据库/Web 界面, 单 Excel 文件即台账(适合小规模, 备份策略见 `store.py`/`backups/`)。
7. 出库成本按 公司+SKU 统一池, 分仓成本差异不体现(口径冻结如此)。
8. 调拨不改公司总数量与总成本(仅分仓变化)。
9. 销售明细行成本在**结账时**写回; 未结账期间的查询为实时计算值。

## 验收样例(AC, 对应 tests/)

| 编号 | 场景 | 期望结果(必须精确) |
|---|---|---|
| AC1 | 期初两仓+采购+两仓销售+调拨 | 加权 10.80; A 末 140 / B 末 40 / 公司 180; 出库成本 756.00; 期末 1,944.00; 756+1944=2700 |
| AC2 | 同 AC1 + 附加费用 100 | 池 2,800; 加权 11.20; 出库成本 784.00; 期末 2,016.00 |
| AC3 | 先销售后采购 | 用全月统一加权, 不得用销售时点暂估值 |
| AC4 | 重复调拨 | 不重复入账; 双边一致; 公司净零 |
| AC5 | 采购成本更正 | 原行保留`已更正`+更正行关联; 出库成本/期末金额重算 |
| AC6 | 结账保护 | 已结账拒写; 授权重开+级联重开+依序重结账; 版本递增 |
| AC7 | 导入幂等 | 同文件重复导入拒绝; 同单据行冲突阻止 |
| AC8 | 异常边界 | 档案缺失/负库存/成本缺失/单位不符/零库存有金额/尾差/月界/部分异常/退货等明确报错 |

## 交付物清单

1. 可安装技能: `SKILL.md` + `references/` + `scripts/` + `tests/` + `examples/`
2. 源码: `scripts/ledgerlib/` + `scripts/cli.py`
3. Excel 模板: `examples/进销存台账模板.xlsx`(或 `init` 随时生成)
4. 测试数据: `examples/demo/data/` + `examples/import_templates/`
5. 自动化测试及结果: `tests/` + `tests/pytest-run.txt`
6. 使用说明: 本文件 + `references/workflows.md`
7. 已知限制: 上节
8. 本地提交号与工作区状态: 见交付报告(最终消息)