# 进销存台账 V1 —— 智能体操作工作流

> 本文件给智能体(Claude Code / Codex / DSH 等)操作台账的**标准步骤**。
> 原则: **AI 理解业务, 程序确定性计算** —— 智能体负责把业务语言解释成确定性的
> 结构化指令/文件, 所有数值计算与校验由程序完成; 模糊处先问, 异常不擅自补全。

## 0. 总览: 一次业务会话的骨架

```
1. 打开台账            -> 检查文件存在, 读取元数据(公司/起始期间/最新期间)
2. 理解业务诉求
3. 生成导入文件(CSV)   -> 用真实数据, 列名与本库表头一致(见 workbook-schema.md)
4. 预览(preview)       -> 程序校验每一行, 展示 成功/待确认/拒绝 与原因
5. 人工确认后提交      -> 整体事务, 幂等
6. 查询验证            -> query stock / monthly, 把数字呈现给业务人员
7. 需要时执行更正/结账 -> 受控流程
```

**铁律**
- 所有写操作必须向用户展示预览/方案并获得明确确认(`--confirm`)。
- 预览发现拒绝行时, 不得擅自改数据重试; 向用户报告原因, 由用户决定。
- 已结账期间任何写入都会被程序拒绝; 重开需用户授权+原因。
- 未给出的数据不得杜撰: 采购成本未确认→待确认状态, 结账前必须确认, 否则结账被阻止并报 E04/E18。

## 1. 初始化

```bash
# 建台账(公司, 起始期间) — 仅一次
python3 scripts/cli.py init --path 进销存台账.xlsx \
    --company "XX商贸有限公司" --start-period 2026-01 --operator 助手名

# 商品档案 / 仓库档案
python3 scripts/cli.py sku add --path 台账.xlsx --sku SKU-001 --name "商品名" --unit 件
python3 scripts/cli.py wh add  --path 台账.xlsx --wh W01 --name "一号仓"
python3 scripts/cli.py sku list / wh list   # 查看现有档案
```

注意: 每个 SKU 一个基本单位; 新增 SKU/仓库后才可导入其流水。

## 2. 文件导入: 预览 → 确认 → 提交

### 2.1 生成导入 CSV

列名 = 表头名(workbook-schema.md), 类型分别支持:

| --type | 工作表 | 最少列 |
|---|---|---|
| opening | 期初库存 | SKU, 仓库, 数量, 单价(或金额) |
| purchase | 入库流水 | 业务日期, 单号, 明细行号, SKU, 仓库, 数量, 确认单价(或确认金额) |
| sale | 出库流水 | 业务日期, 单号, 明细行号, SKU, 仓库, 数量, 渠道, 订单号, 销售单价 |
| transfer | 调拨流水 | 业务日期, 调拨单号, 明细行号, SKU, 调出仓库, 调入仓库, 数量 |
| adjust | 调整流水 | 业务日期, 调整类型, 单号, 明细行号, SKU, 仓库, 数量[, 计价依据类型, 计价单价] |
| fee | 附加费用 | 业务日期, 费用单号, 关联采购单号, 费用总额, 分摊规则 |

规则:
- 可带更多追溯列(批次号/报关单号/物流单号/备注/单价+金额等, 见字段字典)。
- CSV 用 UTF-8(可带 BOM); 也支持 .xlsx(取第一个工作表)。
- 单元格留空 = 未提供; 程序按规则决定待确认(E04)或拒绝。
- 数量必须>0; **退货(V1 不支持)** 输入会被拒绝——负数量或含"退货"字样都拒(E11), 报告给用户按 V2 排队。

### 2.2 预览(不写盘)

```bash
python3 scripts/cli.py import --path 台账.xlsx --file 采购_2026-01.csv \
    --type purchase --preview
```
输出 JSON: `ok_count / pending_count / reject_count / rejected[{row,code,reason}] / imported_before`。

- 销售预览与提交共用同一库存可用量校验: 单行超量或同批多次出库累计超出仓库可用量,
  预览即报告 E03(与提交阶段的判定一致)。
- `imported_before: true` -> 该文件已导入过, 不能再次提交(幂等, E12)。
- `rejected` 非空 -> 逐行向用户说明原因, 等待裁决(改文件重导 / 部分写入需明确授权 `--allow-partial`)。

### 2.3 提交(整体事务)

```bash
python3 scripts/cli.py import --path 台账.xlsx --file 采购_2026-01.csv \
    --type purchase --commit --confirm "用户已确认" --operator 助手名
```
- 默认: 有任何拒绝行则**整个批次不写入**(E09); 用户明确要求"只写合法行"时加 `--allow-partial`, 输出会报告 成功/待确认/拒绝 数量。
- 提交成功 -> 返回 `batch`(如 B20261027-000001), 供审计追溯。

## 3. 查询

```bash
python3 scripts/cli.py query stock   --path 台账.xlsx                # 最新期间分仓+公司结存
python3 scripts/cli.py query stock   --path 台账.xlsx --period 2026-01 --sku SKU-001 --wh W01
python3 scripts/cli.py query monthly --path 台账.xlsx --period 2026-01 # 月度成本计算明细
python3 scripts/cli.py query flows   --path 台账.xlsx --period 2026-01 --kind purchase
python3 scripts/cli.py query pending --path 台账.xlsx                # 待确认成本(结账前必查)
python3 scripts/cli.py query exceptions --path 台账.xlsx --period 2026-01  # 本月异常清单
python3 scripts/cli.py query status  --path 台账.xlsx                # 期间状态/版本
```
- 未结账期间: 状态显示 `暂估`, 数字为实时计算; 结账后: 显示物化结存与期间 `已结账`。
- 注意 stock 的"公司期末金额"是加权成本口径(池−出库成本), 不是按售价。

## 4. 采购成本确认与更正

```bash
# 待确认成本: 用户给出依据后补确认(可同时补单价/金额)
python3 scripts/cli.py cost confirm --path 台账.xlsx --ids IN-000005 \
    --basis "采购合同XS-2026-011 含税价" --price 12.50 --confirm "用户确认"
# 采购成本更正(供应商发票更正等): 原行保留"已更正", 生成更正行
python3 scripts/cli.py cost correct --path 台账.xlsx --id IN-000005 \
    --new-price 13.00 --basis "发票更正单 FP-20260115" --confirm "用户确认"
```
更正后查询即重算; 若期间已结账, 先走 §6 受控重开。

## 5. 附加费用分摊

```bash
# 预览分摊方案(不落盘)
python3 scripts/cli.py fee allocate --path 台账.xlsx --fee-id FEE-000001 --preview
# 按规则分摊(数量/金额比例)或逐明细指定后执行
python3 scripts/cli.py fee allocate --path 台账.xlsx --fee-id FEE-000001 --confirm "用户确认"
python3 scripts/cli.py fee allocate --path 台账.xlsx --fee-id FEE-000001 \
    --explicit "IN-000001:9.99,IN-000002:20.01" --confirm "用户确认"
```
- 分摊合计必须=费用总额, 否则 E07 拒绝; 结账时未分摊费用单会阻止结账(E04)。
- 同一采购单可挂**多张费用单**: 行内"附加费用分摊额"逐单**累计写入**(不覆盖), 结账按期间汇总核对 Σ费用总额 == Σ行分摊额(多费累计, V1 定向修复 1)。
- 费用单须与关联采购**同月**: **登记阶段即校验**——关联采购单须在同月存在有效入库行, 否则拒绝登记(E16), 不会产生"已登记、无法分摊、无法结账"的悬挂记录; 分摊阶段另有同款防御(V1 定向修复 2 + 数据完整性 Fix2)。

## 6. 结账与重开

```bash
# 试结账(只计算, 不落盘): 查看加权单价/出库成本/期末金额与全部异常
python3 scripts/cli.py close dry --path 台账.xlsx --period 2026-01
# 正式结账: 落盘(写回出库成本+物化结存+期间状态), 需用户明确确认
python3 scripts/cli.py close final --path 台账.xlsx --period 2026-01 --confirm "用户确认"
```
- 顺序: 上一期间必须已结账(E13); 结账前提是本月无任何异常(E04/E06/E07/E14…), 否则 E18 拒绝并给出清单。
- 新月份: 先 `period open` 开启(严格逐月):
  ```bash
  python3 scripts/cli.py period open --path 台账.xlsx --period 2026-02
  ```
- 重开(用户授权+原因, 级联重开后续已结账期间):
  ```bash
  python3 scripts/cli.py reopen --path 台账.xlsx --period 2026-01 \
      --auth "财务经理授权 AUTH-016" --reason "1月采购单价录入错误, 按发票更正"
  # 更正完成后, 按 §6 从 2026-01 起依序重新结账
  ```

## 7. 月末常规循环(给智能体的检查单)

1. `query pending` —— 全部采购成本已确认? 否则向用户催依据。
2. `query exceptions --period 当月` —— 无异常? 有则逐条向用户报告。
3. `fee allocate --preview` 每个未分摊费用单 -> 向用户展示方案 -> 执行。
4. `close dry` 给用户展示: 加权单价、出库成本、期末金额、对账差异。
5. 用户确认后 `close final`。
6. `period open 下月` 并继续。

## 8. 常见异常码 → 动作

| 码 | 含义 | 动作 |
|---|---|---|
| E01/E02 | SKU/仓库不存在 | 提示建档或修正文件 |
| E03 | 负库存 | 提示: 检查单据顺序/漏录入库/手工数据 |
| E04 | 成本缺失(待确认/盘盈无依据/费用未分摊) | 向用户索要依据 |
| E05 | 计量单位("单位"列 ≠ 档案) | V1 单单位, 提示删除单位列或改档案 |
| E06/E07 | 零库存带金额/分摊不平/数据不一致 | 展示异常数字, 提示检查费用分摊与输入数据; 精度机制下正常核算不再触发 |
| E08 | 期间未登记/日期错位 | 先 period open, 或修正业务日期 |
| E09 | 批次整体拒绝/未知列 | 看明细原因, 修正后重导 |
| E11 | 退货语义 | 报告: V1 不支持, 已列入 V2 |
| E12 | 重复导入/单据冲突 | 提示: 该文件或单据行已入账, 勿重复提交 |
| E13 | 结账顺序 | 先结上一期间 |
| E14 | 成本池零分母 | 本月出库但池内无已确认成本, 补确认 |
| E15 | 已结账期间写入/重开未授权 | 走受控重开 |
| E16 | 数据不一致(公司≠Σ仓库/接班不一致/关联采购单无同月有效行) | 立即报告, 不自行修复 |
| E17 | 只读打开 | 写操作须以写模式打开(内部错误则不出现) |
| E18 | 试结账未通过 | 按异常清单逐项处理 |

## 9. CLI 全量参考

```
init --company --start-period
sku add|list      (--sku --name --unit [--remark])
wh  add|list      (--wh --name [--remark])
import --file --type purchase|sale|transfer|adjust|fee|opening (--preview|--commit) [--allow-partial]
fee allocate --fee-id [--explicit in:amt,...] [--preview]
cost confirm --ids --basis [--price] [--amount]
cost correct --id [--new-price|--new-amount] --basis
period open --period
close dry|final --period
reopen --period --auth --reason
query stock|flows|monthly|exceptions|pending|status [...]
全局: --path(默认 进销存台账.xlsx) --operator(默认 cli) --json(默认输出 JSON)
写操作一律需 --confirm 用户确认(open/reopen 用 --auth 授权+--reason)。
```