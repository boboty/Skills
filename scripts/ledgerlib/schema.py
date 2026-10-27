"""台账工作簿表结构定义(字段字典的代码实现)。

设计原则(对应 references/business-rules.md 与任务书):
- 九类业务工作表 + 4 张实现必需的控制表(元数据/期间状态/导入记录/附加费用)。
- 业务流水为原始事实; 月末结存为可重算结果, 不做第二套事实。
- 附加费用独立成表以支撑"按规则分摊 + 可审计 + 试结账核对总额"。
- 原参考 Excel(库存进销存台账.xlsx)平铺 32 列仅为字段来源:
  - FIFO 字段不实现; 报关字段仅作追溯标识; 出库类型"库存处置"归入库存调整。

每张表的列顺序即 Excel 列顺序。行首行固定为表头。
"""
from __future__ import annotations


# ---------------------------------------------------------------- 元数据键
META_COMPANY = "公司名称"
META_CURRENCY = "币种"
META_COST_METHOD = "成本方法"
META_PRICE_PREC = "单价精度"
META_MONEY_PREC = "金额精度"
META_QTY_PREC = "数量精度"
META_ROUND_MODE = "舍入模式"
META_START_PERIOD = "起始期间"
META_VERSION = "台账版本"
META_CREATED_AT = "创建时间"
META_WRITES = "写入次数"
META_LEDGER_ID = "台账ID"
# ---------------------------------------------------------------- 工作表名
SHEET_META = "元数据"
SHEET_PERIODS = "期间状态"
SHEET_SKU = "商品档案"
SHEET_WH = "仓库档案"
SHEET_OPENING = "期初库存"
SHEET_IN = "入库流水"
SHEET_OUT = "出库流水"
SHEET_TRANSFER = "调拨流水"
SHEET_ADJUST = "调整流水"
SHEET_FEE = "附加费用"
SHEET_MONTHLY = "月末结存"
SHEET_AUDIT = "操作记录"
SHEET_IMPORTS = "导入记录"

ALL_SHEETS = [
    SHEET_META, SHEET_PERIODS, SHEET_SKU, SHEET_WH, SHEET_OPENING,
    SHEET_IN, SHEET_OUT, SHEET_TRANSFER, SHEET_ADJUST, SHEET_FEE,
    SHEET_MONTHLY, SHEET_AUDIT, SHEET_IMPORTS,
]

# ---------------------------------------------------------------- 公共常量
PERIOD_FMT = "YYYY-MM"
DATE_FMT = "YYYY-MM-DD"
DATETIME_FMT = "YYYY-MM-DD HH:MM:SS"

# 精度参数(与 business-rules.md 下篇 §2 一致)
PREC_QTY = "0.000001"     # 数量 6 位
PREC_PRICE = "0.0001"     # 单价 4 位
PREC_MONEY = "0.01"       # 金额 2 位
ROUND_MODE = "ROUND_HALF_UP"

# 状态枚举
RECORD_OK = "正常"
RECORD_VOID = "作废"          # 原行被更正取代
RECORD_REPLACED = "已更正"     # 原行状态(仍保留原始数据)
RECORD_PENDING = "待确认"      # 成本待确认

PERIOD_OPEN = "未结账"
PERIOD_DRY = "试结账"
PERIOD_CLOSED = "已结账"

COST_CONFIRMED = "已确认"
COST_PENDING = "待确认"

ADJ_GAIN = "盘盈"
ADJ_LOSS = "盘亏"
ADJ_SCRAP = "报废"
ADJ_FIX = "受控更正"

FEE_RULE_QTY = "按数量比例"
FEE_RULE_AMOUNT = "按金额比例"
FEE_RULE_EXPLICIT = "逐明细指定"

ROW_COMPANY = "公司"
ROW_WAREHOUSE = "仓库"

MONTHLY_TENTATIVE = "暂估"
MONTHLY_FINAL = "已结账"
MONTHLY_HISTORY = "历史"

IMPORT_OK = "成功"
IMPORT_REJECTED = "拒绝"
IMPORT_REVIEW = "待确认"

CURRENCY = "CNY"
COST_METHOD = "月末一次加权平均"

# ---------------------------------------------------------------- 表结构
# 每项: (字段名, 列标题)。列标题即 Excel 表头; 字段名用于代码内引用。
COLUMNS = {
    SHEET_META: [
        ("key", "键"),
        ("value", "值"),
        ("remark", "备注"),
    ],
    SHEET_PERIODS: [
        ("period", "期间"),
        ("status", "状态"),
        ("version", "核算版本"),
        ("updated_at", "更新时间"),
        ("operator", "操作人"),
        ("remark", "备注"),
    ],
    SHEET_SKU: [
        ("sku", "SKU"),
        ("name", "商品名称"),
        ("unit", "基本单位"),
        ("status", "状态"),
        ("created_at", "建档时间"),
        ("remark", "备注"),
    ],
    SHEET_WH: [
        ("wh", "仓库代码"),
        ("name", "仓库名称"),
        ("status", "状态"),
        ("created_at", "建档时间"),
        ("remark", "备注"),
    ],
    SHEET_OPENING: [
        ("opening_id", "期初ID"),
        ("period", "起始期间"),
        ("sku", "SKU"),
        ("wh", "仓库"),
        ("qty", "数量"),
        ("price", "单价"),
        ("amount", "金额"),
        ("status", "状态"),
        ("src_file", "来源文件"),
        ("src_sheet", "来源工作表"),
        ("src_row", "来源行号"),
        ("file_hash", "文件摘要"),
        ("batch", "导入批次"),
        ("created_at", "录入时间"),
        ("operator", "操作人"),
        ("confirmed_by", "用户确认"),
        ("remark", "备注"),
    ],
    SHEET_IN: [
        ("in_id", "入库ID"),
        ("period", "期间"),
        ("biz_date", "业务日期"),
        ("doc_no", "单号"),
        ("line_no", "明细行号"),
        ("sku", "SKU"),
        ("wh", "仓库"),
        ("qty", "数量"),
        ("price", "确认单价"),
        ("amount", "确认金额"),
        ("fee", "附加费用分摊额"),
        ("cost_status", "成本状态"),
        ("batch_no", "批次号"),
        ("customs_no", "报关单号"),
        ("customs_type", "报关类型"),
        ("in_code", "入库码"),
        ("status", "记录状态"),
        ("correct_of", "更正关联ID"),
        ("src_file", "来源文件"),
        ("src_sheet", "来源工作表"),
        ("src_row", "来源行号"),
        ("file_hash", "文件摘要"),
        ("batch", "导入批次"),
        ("created_at", "录入时间"),
        ("operator", "操作人"),
        ("confirmed_by", "用户确认"),
        ("remark", "备注"),
    ],
    SHEET_OUT: [
        ("out_id", "出库ID"),
        ("period", "期间"),
        ("biz_date", "业务日期"),
        ("doc_no", "单号"),
        ("line_no", "明细行号"),
        ("sku", "SKU"),
        ("wh", "仓库"),
        ("channel", "渠道"),
        ("order_no", "订单号"),
        ("qty", "数量"),
        ("sale_price", "销售单价"),
        ("sale_amount", "销售金额"),
        ("price_adj", "销售价调整"),
        ("price_adj_type", "销售价调整类型"),
        ("actual_price", "实际销售价"),
        ("logistics_no", "物流单号"),
        ("out_cost", "出库成本"),
        ("status", "记录状态"),
        ("correct_of", "更正关联ID"),
        ("src_file", "来源文件"),
        ("src_sheet", "来源工作表"),
        ("src_row", "来源行号"),
        ("file_hash", "文件摘要"),
        ("batch", "导入批次"),
        ("created_at", "录入时间"),
        ("operator", "操作人"),
        ("confirmed_by", "用户确认"),
        ("remark", "备注"),
    ],
    SHEET_TRANSFER: [
        ("transfer_id", "调拨ID"),
        ("period", "期间"),
        ("biz_date", "业务日期"),
        ("doc_no", "调拨单号"),
        ("line_no", "明细行号"),
        ("sku", "SKU"),
        ("from_wh", "调出仓库"),
        ("to_wh", "调入仓库"),
        ("qty", "数量"),
        ("status", "记录状态"),
        ("correct_of", "更正关联ID"),
        ("src_file", "来源文件"),
        ("src_sheet", "来源工作表"),
        ("src_row", "来源行号"),
        ("file_hash", "文件摘要"),
        ("batch", "导入批次"),
        ("created_at", "录入时间"),
        ("operator", "操作人"),
        ("confirmed_by", "用户确认"),
        ("remark", "备注"),
    ],
    SHEET_ADJUST: [
        ("adj_id", "调整ID"),
        ("period", "期间"),
        ("biz_date", "业务日期"),
        ("adj_type", "调整类型"),
        ("doc_no", "单号"),
        ("line_no", "明细行号"),
        ("sku", "SKU"),
        ("wh", "仓库"),
        ("qty", "数量"),
        ("basis_type", "计价依据类型"),
        ("basis_price", "计价单价"),
        ("basis_amount", "计价金额"),
        ("status", "记录状态"),
        ("correct_of", "更正关联ID"),
        ("src_file", "来源文件"),
        ("src_sheet", "来源工作表"),
        ("src_row", "来源行号"),
        ("file_hash", "文件摘要"),
        ("batch", "导入批次"),
        ("created_at", "录入时间"),
        ("operator", "操作人"),
        ("confirmed_by", "用户确认"),
        ("remark", "备注"),
    ],
    SHEET_FEE: [
        ("fee_id", "费用ID"),
        ("period", "期间"),
        ("doc_no", "费用单号"),
        ("in_doc_no", "关联采购单号"),
        ("rule", "分摊规则"),
        ("total", "费用总额"),
        ("status", "分摊状态"),
        ("src_file", "来源文件"),
        ("src_sheet", "来源工作表"),
        ("src_row", "来源行号"),
        ("file_hash", "文件摘要"),
        ("batch", "导入批次"),
        ("created_at", "录入时间"),
        ("operator", "操作人"),
        ("confirmed_by", "用户确认"),
        ("remark", "备注"),
    ],
    SHEET_MONTHLY: [
        ("period", "期间"),
        ("sku", "SKU"),
        ("wh", "仓库"),
        ("row_type", "行类型"),
        ("open_qty", "期初数量"),
        ("in_qty", "计价入库数量"),
        ("out_qty", "出库数量"),
        ("transfer_net", "调拨净"),
        ("adj_qty", "调整数量"),
        ("end_qty", "期末数量"),
        ("open_amount", "期初金额"),
        ("in_amount", "计价入库金额"),
        ("fee_amount", "附加费用分摊额"),
        ("out_cost", "出库成本"),
        ("end_amount", "期末金额"),
        ("avg_price", "加权单价"),
        ("status", "状态"),
        ("version", "核算版本"),
        ("updated_at", "更新时间"),
    ],
    SHEET_AUDIT: [
        ("op_id", "操作ID"),
        ("created_at", "时间"),
        ("op_type", "操作类型"),
        ("operator", "操作人"),
        ("summary", "指令摘要"),
        ("detail", "变更摘要"),
        ("ref_key", "关联键"),
        ("result", "结果"),
    ],
    SHEET_IMPORTS: [
        ("imp_id", "导入ID"),
        ("batch", "批次号"),
        ("file_name", "文件名"),
        ("file_hash", "文件摘要"),
        ("file_size", "文件大小"),
        ("imp_type", "导入类型"),
        ("ok_count", "成功条数"),
        ("reject_count", "拒绝条数"),
        ("pending_count", "待确认条数"),
        ("created_at", "导入时间"),
        ("operator", "操作人"),
        ("status", "状态"),
    ],
}

# 需要以 Decimal 数值(而非字符串)存储的列 —— 金额/数量/单价
DECIMAL_COLS = {
    col for cols in COLUMNS.values() for _, col in cols
    if col in ("数量", "单价", "金额", "确认单价", "确认金额", "销售单价",
               "销售金额", "销售价调整", "实际销售价", "附加费用分摊额",
               "费用总额", "出库成本", "计价单价", "计价金额", "期初数量",
               "计价入库数量", "出库数量", "调拨净", "调整数量", "期末数量",
               "期初金额", "计价入库金额", "期末金额", "加权单价")
}


def fields(sheet: str) -> list:
    """返回指定工作表的字段名列表(顺序=列顺序)。"""
    return [f for f, _ in COLUMNS[sheet]]


def headers(sheet: str) -> list:
    """返回指定工作表的表头列表。"""
    return [h for _, h in COLUMNS[sheet]]


def field_index(sheet: str, field: str) -> int:
    return fields(sheet).index(field)