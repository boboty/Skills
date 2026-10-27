"""文件导入: .xlsx / .csv → 结构化候选行 → 预览分类 → 确认提交。

原则(任务书 §8 与 business-rules.md):
- 字段映射显式可见: 表头必须使用本脚本支持的规范列名, 未知列整批拒绝并列出;
- 先预览后提交: preview 不写盘, commit 才写入;
- 幂等: 同一文件摘要(sha256)+导入类型 已导入过 -> 整批拒绝; 来源行指纹/单据指纹
  重复 -> 行级拒绝(E12);
- 部分异常不得悄悄丢弃: 预览/提交结果均列出 成功/拒绝/待确认 数量与原因。
"""
from __future__ import annotations

import csv
import os
from decimal import Decimal

import openpyxl

from . import schema as S
from . import biz
from .store import Store, LedgerError, sha256_of_file, is_date_valid, period_of

# ---------------------------------------------------------------- 列映射定义
# 规范列名(表头) -> 内部字段; 每个类型独立。未知列 -> 拒绝。
COLUMNS = {
    "purchase": {
        "业务日期": "业务日期", "单号": "单号", "明细行号": "明细行号",
        "SKU": "SKU", "仓库": "仓库", "数量": "数量",
        "确认单价": "确认单价", "确认金额": "确认金额", "成本状态": "成本状态",
        "批次号": "批次号", "报关单号": "报关单号", "报关类型": "报关类型",
        "入库码": "入库码", "单位": "单位", "备注": "备注",
    },
    "sale": {
        "业务日期": "业务日期", "单号": "单号", "明细行号": "明细行号",
        "SKU": "SKU", "仓库": "仓库", "数量": "数量",
        "渠道": "渠道", "订单号": "订单号",
        "销售单价": "销售单价", "销售金额": "销售金额",
        "销售价调整": "销售价调整", "销售价调整类型": "销售价调整类型",
        "物流单号": "物流单号", "单位": "单位", "备注": "备注",
    },
    "transfer": {
        "业务日期": "业务日期", "调拨单号": "调拨单号", "明细行号": "明细行号",
        "SKU": "SKU", "调出仓库": "调出仓库", "调入仓库": "调入仓库",
        "数量": "数量", "备注": "备注",
    },
    "adjust": {
        "业务日期": "业务日期", "调整类型": "调整类型", "单号": "单号",
        "明细行号": "明细行号", "SKU": "SKU", "仓库": "仓库", "数量": "数量",
        "计价依据类型": "计价依据类型", "计价单价": "计价单价",
        "计价金额": "计价金额", "备注": "备注",
    },
    "fee": {
        "业务日期": "业务日期", "费用单号": "费用单号",
        "关联采购单号": "关联采购单号", "费用总额": "费用总额",
        "分摊规则": "分摊规则", "备注": "备注",
    },
    "opening": {
        "SKU": "SKU", "仓库": "仓库", "数量": "数量", "单价": "单价",
        "金额": "金额", "备注": "备注",
    },
}

REQUIRED = {
    "purchase": ["业务日期", "单号", "SKU", "仓库", "数量"],
    "sale": ["业务日期", "单号", "SKU", "仓库", "数量"],
    "transfer": ["业务日期", "调拨单号", "SKU", "调出仓库", "调入仓库", "数量"],
    "adjust": ["业务日期", "调整类型", "单号", "SKU", "仓库", "数量"],
    "fee": ["业务日期", "费用单号", "关联采购单号", "费用总额", "分摊规则"],
    "opening": ["SKU", "仓库", "数量", "单价"],
}

SHEET_OF_TYPE = {
    "purchase": S.SHEET_IN, "sale": S.SHEET_OUT, "transfer": S.SHEET_TRANSFER,
    "adjust": S.SHEET_ADJUST, "fee": S.SHEET_FEE, "opening": S.SHEET_OPENING,
}

ID_FIELD_OF_SHEET = {
    S.SHEET_IN: "入库ID", S.SHEET_OUT: "出库ID", S.SHEET_TRANSFER: "调拨ID",
    S.SHEET_ADJUST: "调整ID", S.SHEET_FEE: "费用ID", S.SHEET_OPENING: "期初ID",
}


def parse_file(path: str) -> tuple[list[str], list[dict], str, int]:
    """解析文件 -> (表头, 行dict列表, 文件名, 大小)。"""
    if not os.path.exists(path):
        raise LedgerError("E16", f"文件不存在: {path}")
    name = os.path.basename(path)
    size = os.path.getsize(path)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            rows = list(reader)
        if not rows:
            raise LedgerError("E09", "CSV 为空文件")
        header = [str(c).strip() for c in rows[0]]
        data = []
        for i, r in enumerate(rows[1:], start=2):
            if all(str(c).strip() == "" for c in r):
                continue
            d = {}
            for idx, h in enumerate(header):
                if idx < len(r):
                    d[h] = str(r[idx]).strip()
            data.append((i, d))
        return header, data, name, size
    if ext in (".xlsx", ".xlsm"):
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        try:
            header = [str(c).strip() if c is not None else "" for c in next(it)]
        except StopIteration:
            raise LedgerError("E09", "Excel 为空文件") from None
        data = []
        for i, r in enumerate(it, start=2):
            if all(c is None or str(c).strip() == "" for c in r):
                continue
            d = {}
            for idx, h in enumerate(header):
                if idx < len(r) and r[idx] is not None:
                    d[h] = str(r[idx]).strip()
            data.append((i, d))
        wb.close()
        return header, data, name, size
    raise LedgerError("E09", f"不支持的文件类型: {ext} (仅 .xlsx/.csv)")


def map_columns(header: list[str], imp_type: str) -> dict:
    """返回 文件列 -> 内部字段 映射; 未知列/缺失必填列抛 E09。"""
    known = COLUMNS[imp_type]
    mapping: dict[str, str] = {}
    unknown = []
    for h in header:
        if h == "":
            continue
        if h in known:
            mapping[h] = known[h]
        else:
            unknown.append(h)
    if unknown:
        raise LedgerError("E09",
                          f"存在无法映射的列(不允许静默忽略): {unknown}; "
                          f"支持列: {list(known)}")
    missing = [c for c in REQUIRED[imp_type] if c not in [h for h in header]]
    if missing:
        raise LedgerError("E09", f"缺少必填列: {missing}")
    return mapping


class Preview:
    def __init__(self, file_hash: str, file_name: str, file_size: int, imp_type: str):
        self.file_hash = file_hash
        self.file_name = file_name
        self.file_size = file_size
        self.imp_type = imp_type
        self.mapping: dict[str, str] = {}
        self.ok: list[dict] = []
        self.rejected: list[tuple[int, str, str]] = []  # (文件行, code, reason)
        self.pending: list[dict] = []
        self.imported_before = False
        self.prior_batch = ""

    @property
    def summary(self) -> dict:
        return {
            "type": self.imp_type, "file": self.file_name, "file_hash": self.file_hash[:16],
            "mapping": self.mapping, "ok_count": len(self.ok),
            "reject_count": len(self.rejected), "pending_count": len(self.pending),
            "imported_before": self.imported_before, "prior_batch": self.prior_batch,
            "rejected": [{"row": r, "code": c, "reason": m} for r, c, m in self.rejected],
        }


def preview(store: Store, path: str, imp_type: str) -> Preview:
    if imp_type not in COLUMNS:
        raise LedgerError("E09", f"未知导入类型: {imp_type} (可选: {list(COLUMNS)})")
    header, data_rows, name, size = parse_file(path)
    mapping = map_columns(header, imp_type)
    hash_ = sha256_of_file(path)

    pr = Preview(hash_, name, size, imp_type)
    pr.mapping = mapping
    prior = store.find_import(hash_, imp_type)
    if prior:
        pr.imported_before = True
        pr.prior_batch = prior.get("批次号", "")
    src_sheet = "Sheet1" if name.lower().endswith((".xlsx", ".xlsm")) else "CSV"

    # 行指纹预检: 来源文件+工作表+行号 已在目标表 -> E12
    target_sheet = SHEET_OF_TYPE[imp_type]
    id_field = ID_FIELD_OF_SHEET[target_sheet]
    existing_src = {
        (r.get("文件摘要"), r.get("来源工作表"), str(r.get("来源行号") or ""))
        for r in store.read_all(target_sheet) if r.get("文件摘要")
    }

    for file_row, raw in data_rows:
        row = {mapping[k]: v for k, v in raw.items() if k in mapping}
        src = {"file": name, "sheet": src_sheet, "row": str(file_row), "hash": hash_}
        if (hash_, src_sheet, str(file_row)) in existing_src:
            pr.rejected.append((file_row, "E12",
                                f"来源行已入账(文件 {name} 行 {file_row})"))
            continue
        try:
            if imp_type == "purchase":
                norm = biz.normalize_purchase(store, row, file_row, src)
            elif imp_type == "sale":
                norm = biz.normalize_sale(store, row, file_row, src)
            elif imp_type == "transfer":
                norm = biz.normalize_transfer(store, row, file_row, src)
            elif imp_type == "adjust":
                norm = biz.normalize_adjust(store, row, file_row, src)
            elif imp_type == "fee":
                # 与 add_fee 相同的校验逻辑(复用行规范化)
                norm = _normalize_fee(store, row, file_row, src)
            elif imp_type == "opening":
                norm = _normalize_opening(store, row, file_row, src)
            else:  # pragma: no cover
                raise LedgerError("E09", f"未知类型 {imp_type}")
        except LedgerError as ex:
            pr.rejected.append((file_row, ex.code, ex.message))
            continue
        if imp_type == "purchase" and norm.get("成本状态") == S.COST_PENDING:
            pr.pending.append(norm)
        else:
            pr.ok.append(norm)

    # Fix1: 同批次内部(尚未落库)单据指纹互查, 重复行移入拒绝(E12);
    # 保证"预览已识别"与"提交整批拒绝"行为一致。
    if imp_type in ("purchase", "sale"):
        keep_ok, keep_pend = [], []
        seen: dict[tuple, str] = {}
        for row in pr.ok + pr.pending:
            key = (str(row.get("期间") or ""), str(row.get("单号") or ""),
                   str(row.get("明细行号") or ""), str(row.get("SKU") or ""),
                   str(row.get("仓库") or ""))
            first = seen.get(key)
            if first is not None:
                pr.rejected.append((int(row.get("来源行号") or 0), "E12",
                                    f"同批次重复: 与导入行 {first} 单据指纹相同"
                                    f" (单号 {key[1]} 行 {key[2]} SKU {key[3]} @ {key[4]})"))
            else:
                seen[key] = str(row.get("来源行号") or "")
                (keep_pend if row.get("成本状态") == S.COST_PENDING else keep_ok).append(row)
        pr.ok, pr.pending = keep_ok, keep_pend
    return pr


def _normalize_fee(store: Store, r: dict, line: int, src: dict) -> dict:
    biz_date = str(r.get("业务日期") or "").strip()
    if not biz_date or not is_date_valid(biz_date):
        raise LedgerError("E08", f"业务日期必须为 YYYY-MM-DD: 行{line}")
    period = period_of(biz_date)
    store.ensure_period_open(period)
    rule = str(r.get("分摊规则") or "").strip()
    if rule not in (S.FEE_RULE_QTY, S.FEE_RULE_AMOUNT, S.FEE_RULE_EXPLICIT):
        raise LedgerError("E08", f"分摊规则必须为 按数量比例/按金额比例/逐明细指定: 行{line}")
    total = agreement_money(r.get("费用总额"))
    if total <= 0:
        raise LedgerError("E08", f"费用总额必须 > 0: 行{line}")
    doc = str(r.get("费用单号") or "").strip()
    in_doc = str(r.get("关联采购单号") or "").strip()
    if not doc or not in_doc:
        raise LedgerError("E08", f"费用单号/关联采购单号不能为空: 行{line}")
    for e in store.read_all(S.SHEET_FEE):
        if e.get("费用单号") == doc:
            raise LedgerError("E12", f"费用单 {doc} 已存在({e.get('费用ID')})")
    related = [x for x in store.read_all(S.SHEET_IN)
               if x.get("单号") == in_doc and x.get("记录状态") == "正常"
               and x.get("期间") == period]
    if not related:
        raise LedgerError("E16",
                          f"关联采购单 {in_doc} 在期间 {period} 无有效入库行"
                          f" (费用单须与采购同月且采购已入账)")
    return {"业务日期": biz_date, "费用单号": doc, "关联采购单号": in_doc,
            "分摊规则": rule, "费用总额": total, "备注": r.get("备注", ""),
            "来源文件": src["file"], "来源工作表": src["sheet"],
            "来源行号": src["row"], "文件摘要": src["hash"]}


def _normalize_opening(store: Store, r: dict, line: int, src: dict) -> dict:
    from .decimal_utils import money, qty, price
    from .decimal_utils import Decimal as D
    store.ensure_period_open(store.meta(S.META_START_PERIOD))
    sku, wh = r.get("SKU"), r.get("仓库")
    biz._require_sku(store, sku)
    biz._require_wh(store, wh)
    q = qty(r.get("数量"))
    if q <= 0:
        raise LedgerError("E11", f"期初数量必须 > 0: 行{line}")
    p = price(r.get("单价"))
    calc = money(q * p)
    given = r.get("金额")
    if given not in (None, ""):
        g = money(given)
        if abs(g - calc) > D("0.01"):
            raise LedgerError("E07", f"期初金额 {g} 与 数量×单价 {calc} 不符(行{line})")
    return {"SKU": sku, "仓库": wh, "数量": q, "单价": p, "金额": calc,
            "备注": r.get("备注", ""), "来源文件": src["file"],
            "来源工作表": src["sheet"], "来源行号": src["row"], "文件摘要": src["hash"]}


def agreement_money(v) -> Decimal:
    from .decimal_utils import money as _m
    return _m(v)


def commit(store: Store, path: str, imp_type: str, operator: str,
           confirmed_by: str, allow_partial: bool = False) -> dict:
    """正式提交导入。默认整批事务; allow_partial 时成功行照常写入、拒绝行报告。"""
    pr = preview(store, path, imp_type)
    if pr.imported_before:
        raise LedgerError("E12",
                          f"文件已导入过(批次 {pr.prior_batch}, 类型 {imp_type}), 拒绝重复入账")
    if pr.rejected and not allow_partial:
        # 无通过行或存在拒绝行 -> 按整体事务处理: 有拒绝行则须人工裁决
        raise LedgerError("E09",
                          f"{len(pr.rejected)} 行被拒绝, 未写入; 请修正后重新提交: "
                          + "; ".join(f"行{r} [{c}] {m}" for r, c, m in pr.rejected[:10]))
    src_base = {"file": pr.file_name, "sheet": "Sheet1" if pr.file_name.lower().endswith((".xlsx", ".xlsm")) else "CSV", "hash": pr.file_hash}
    with store.batch():  # 修复4b: 业务写入+导入登记+审计 单次原子写盘
        # (batch 内 store.save() 挂起, 任一环节失败整批不落盘)
        # Fix3: 批次号唯一确定, 业务行/导入登记/审计引用同一 ID
        batch = store.next_batch_id()
        n_ok = n_pending = 0
        if imp_type == "purchase":
            rows = [dict(x) for x in pr.ok + pr.pending]
            for r in rows:
                r["操作人"] = operator
                r["用户确认"] = confirmed_by
                r["导入批次"] = batch
            res = biz.add_purchases(store, rows, operator, confirmed_by, src_base, batch)
            n_ok = len(res["ids"])
            n_pending = len(pr.pending)
        elif imp_type == "sale":
            rows = [dict(x) for x in pr.ok]
            for r in rows:
                r["操作人"] = operator
                r["用户确认"] = confirmed_by
                r["导入批次"] = batch
            res = biz.add_sales(store, rows, operator, confirmed_by, src_base, batch)
            n_ok = len(res["ids"])
        elif imp_type == "transfer":
            rows = [dict(x) for x in pr.ok]
            for r in rows:
                r["操作人"] = operator
                r["用户确认"] = confirmed_by
                r["导入批次"] = batch
            res = biz.add_transfers(store, rows, operator, confirmed_by, src_base, batch)
            n_ok = len(res["ids"])
        elif imp_type == "adjust":
            rows = [dict(x) for x in pr.ok]
            for r in rows:
                r["操作人"] = operator
                r["用户确认"] = confirmed_by
                r["导入批次"] = batch
            res = biz.add_adjustments(store, rows, operator, confirmed_by, src_base, batch)
            n_ok = len(res["ids"])
        elif imp_type == "fee":
            fee_rows = [{"业务日期": x["业务日期"], "费用单号": x["费用单号"],
                         "关联采购单号": x["关联采购单号"], "费用总额": x["费用总额"],
                         "分摊规则": x["分摊规则"], "备注": x.get("备注", ""),
                         "来源行号": x.get("来源行号", "")}
                        for x in pr.ok]
            res = biz.add_fee(store, fee_rows, operator, confirmed_by, src_base, batch)
            n_ok = res["posted"]
        elif imp_type == "opening":
            entries = [{"SKU": x["SKU"], "仓库": x["仓库"], "数量": x["数量"],
                        "单价": x["单价"], "金额": x["金额"], "备注": x.get("备注", ""),
                        "来源行号": x.get("来源行号", "")}
                       for x in pr.ok]
            res = None
            biz.set_opening(store, entries, operator, confirmed_by,
                            src=src_base, batch=batch)
            n_ok = len(entries)
        else:  # pragma: no cover
            raise LedgerError("E09", f"未知类型 {imp_type}")

    # 修复4b: 业务写入 + 导入登记 + 审计 一次原子提交(batch 内 save 挂起,
        batch_reg = store.register_import(pr.file_name, pr.file_hash, pr.file_size,
                                          imp_type, n_ok, len(pr.rejected), n_pending,
                                          operator, batch)
        store.audit("import", operator,
                    f"导入 {imp_type} 文件 {pr.file_name} => 成功 {n_ok}, "
                    f"拒绝 {len(pr.rejected)}, 待确认 {n_pending}",
                    f"用户确认: {confirmed_by}; 批次: {batch_reg}", batch_reg, "成功")
    return {"batch": batch_reg, "ok": n_ok, "rejected": len(pr.rejected),
            "pending": n_pending,
            "reject_detail": [{"row": r, "code": c, "reason": m}
                              for r, c, m in pr.rejected]}