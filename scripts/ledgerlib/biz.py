"""业务操作层: 全部写操作的确定性校验与落库。

每个写入入口遵循同一纪律:
1. 校验期间未结账 (E15) 与业务日期/期间推导一致 (E08);
2. 校验 SKU/仓库档案存在 (E01/E02);
3. 数量 > 0, 拒绝退货语义 (E11);
4. 业务单据指纹 (期间+单号+明细行号+SKU+仓库) 与来源行指纹 (文件摘要+表+行) 防重 (E12);
5. 出库/调出/损耗实时校验仓库负库存 (E03);
6. 部分异常行拒绝并返回原因, 不静默丢弃; 成功后写审计与元数据。
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from . import schema as S
from .decimal_utils import money, price, qty as norm_qty
from .engine import compute_month
from .store import Store, LedgerError, is_date_valid, period_of, now_str

RETURN_KEYWORDS = ("退货", "return", "退款")
UNIT_FIELD_ALIASES = ("单位", "基本单位", "计量单位")


# ---------------------------------------------------------------- 基础档案
def add_sku(store: Store, sku: str, name: str, unit: str,
            operator: str, remark: str = "") -> str:
    if not sku or not name or not unit:
        raise LedgerError("E08", "SKU/名称/基本单位 均不能为空")
    if any(r.get("SKU") == sku for r in store.read_all(S.SHEET_SKU)):
        raise LedgerError("E01", f"SKU 已存在: {sku}")
    store.append_row(S.SHEET_SKU, {
        "SKU": sku, "商品名称": name, "基本单位": unit, "状态": "启用",
        "建档时间": now_str(), "备注": remark,
    })
    store.audit("sku", operator, f"新增商品 {sku} ({name}, 单位 {unit})", remark, sku, "成功")
    store.save()
    return sku


def add_warehouse(store: Store, wh: str, name: str,
                  operator: str, remark: str = "") -> str:
    if not wh or not name:
        raise LedgerError("E08", "仓库代码/名称 不能为空")
    if any(r.get("仓库代码") == wh for r in store.read_all(S.SHEET_WH)):
        raise LedgerError("E02", f"仓库已存在: {wh}")
    store.append_row(S.SHEET_WH, {
        "仓库代码": wh, "仓库名称": name, "状态": "启用",
        "建档时间": now_str(), "备注": remark,
    })
    store.audit("wh", operator, f"新增仓库 {wh} ({name})", remark, wh, "成功")
    store.save()
    return wh


# ---------------------------------------------------------------- 期初
def set_opening(store: Store, entries: list[dict], operator: str,
                confirmed_by: str, period: str | None = None,
                src: dict | None = None, batch: str = "") -> dict:
    """设置期初库存。可重设(作废旧行+写入新行), 前提: 起始期间未结账且无业务流水。"""
    start = store.meta(S.META_START_PERIOD)
    if period is not None and period != start:
        raise LedgerError("E08", f"期初只能设置在起始期间 {start}")
    store.ensure_period_open(start)
    # 重设保护: 起始期间已有业务流水则拒绝(改用受控更正)
    has_flow = any(r.get("期间") == start
                   for s in (S.SHEET_IN, S.SHEET_OUT, S.SHEET_TRANSFER, S.SHEET_ADJUST)
                   for r in store.read_all(s))
    if has_flow:
        raise LedgerError("E08", "起始期间已存在业务流水, 期初禁止重设; 如需修正请走受控更正")
    if not entries:
        raise LedgerError("E08", "期初明细为空")
    posted = []
    rejected = []
    for i, e in enumerate(entries):
        sku, wh = e.get("sku") or e.get("SKU"), e.get("wh") or e.get("仓库")
        try:
            _require_sku(store, sku)
            _require_wh(store, wh)
            q = norm_qty(e.get("qty", e.get("数量")))
            if q <= 0:
                raise LedgerError("E11", f"期初数量必须 > 0: {q}")
            p = price(e.get("price", e.get("单价")))
            calc = money(q * p)
            given = e.get("amount", e.get("金额"))
            if given not in (None, ""):
                g = money(given)
                if abs(g - calc) > Decimal("0.01"):
                    raise LedgerError("E07",
                                      f"期初金额 {g} 与 数量×单价 {calc} 不符(差 > 0.01)")
            store.append_row(S.SHEET_OPENING, {
                "期初ID": store.next_id(S.SHEET_OPENING, "OPN-"),
                "起始期间": start, "SKU": sku, "仓库": wh, "数量": q, "单价": p,
                "金额": calc, "状态": S.RECORD_OK,
                "来源文件": _src(src, "file"), "来源工作表": _src(src, "sheet"),
                "来源行号": _src(src, "row"), "文件摘要": _src(src, "hash"),
                "导入批次": batch, "录入时间": now_str(), "操作人": operator,
                "用户确认": confirmed_by, "备注": e.get("remark", ""),
            })
            posted.append(sku)
        except LedgerError as ex:
            rejected.append((i + 1, ex.code, ex.message))
    if rejected:
        raise LedgerError("E09", f"期初 {len(rejected)} 行被拒绝: "
                                 + "; ".join(f"行{r}: [{c}] {m}" for r, c, m in rejected))
    store.audit("opening", operator, f"设置期初库存 {start} 共 {len(posted)} 条",
                f"用户确认: {confirmed_by}", f"P{start}", "成功")
    store.save()
    return {"posted": len(posted), "rejected": rejected}


# ---------------------------------------------------------------- 采购入库
def normalize_purchase(store: Store, r: dict, line: int,
                       src: dict | None = None, batch: str = "") -> dict:
    """将导入行规范化为入库流水行; 抛 LedgerError 表示拒绝。"""
    sku = r.get("SKU") or r.get("sku")
    wh = r.get("仓库") or r.get("wh")
    _require_sku(store, sku)
    _require_wh(store, wh)
    _check_return(r, f"入库行{line}")
    _check_unit(store, sku, r, line)
    biz_date = _require_date(r.get("业务日期"), line)
    period = period_of(biz_date)
    store.ensure_period_open(period)
    q = norm_qty(r.get("数量"))
    if q <= 0:
        raise LedgerError("E11", f"入库数量必须 > 0: 行{line}")
    doc = str(r.get("单号") or "").strip()
    lineno = str(r.get("明细行号") or "1").strip()
    if not doc:
        raise LedgerError("E08", f"入库单号不能为空: 行{line}")
    _check_doc_fingerprint(store, S.SHEET_IN, period, doc, lineno, sku, wh)
    price_v, amount_v = r.get("确认单价"), r.get("确认金额")
    cost_status = str(r.get("成本状态") or S.COST_CONFIRMED)
    if price_v in (None, "") and amount_v in (None, ""):
        cost_status = S.COST_PENDING
    p, amt = _resolve_price_amount(price_v, amount_v, q, f"入库行{line}")
    if cost_status == S.COST_CONFIRMED and (p is None or p < 0):
        raise LedgerError("E04", f"明确要求已确认但缺成本依据(单号 {doc}): 行{line}")
    if cost_status not in (S.COST_CONFIRMED, S.COST_PENDING):
        raise LedgerError("E08", f"成本状态只能为 {S.COST_CONFIRMED}/{S.COST_PENDING}: 行{line}")
    return {
        "期间": period, "业务日期": biz_date, "单号": doc, "明细行号": lineno,
        "SKU": sku, "仓库": wh, "数量": q, "确认单价": p if p is not None else "",
        "确认金额": amt if p is not None else "",
        "附加费用分摊额": Decimal("0"), "成本状态": cost_status,
        "批次号": r.get("批次号", ""), "报关单号": r.get("报关单号", ""),
        "报关类型": r.get("报关类型", ""), "入库码": r.get("入库码", ""),
        "记录状态": S.RECORD_OK, "更正关联ID": "",
        "来源文件": _src(src, "file"), "来源工作表": _src(src, "sheet"),
        "来源行号": _src(src, "row"), "文件摘要": _src(src, "hash"),
        "导入批次": batch, "录入时间": now_str(), "操作人": r.get("_operator", ""),
        "用户确认": r.get("_confirmed_by", ""), "备注": r.get("备注", ""),
    }


def add_purchases(store: Store, rows: list[dict], operator: str,
                  confirmed_by: str, src: dict | None = None,
                  batch: str = "") -> dict:
    """采购入库(整体事务): 任一行拒绝则整批不落库(调用方应先用预览过滤)。"""
    normalized, rejected = [], []
    for i, r in enumerate(rows, start=1):
        r = dict(r)
        r["_operator"], r["_confirmed_by"] = operator, confirmed_by
        try:
            normalized.append(normalize_purchase(store, r, i, src, batch))
        except LedgerError as ex:
            rejected.append((i, ex.code, ex.message))
    if rejected:
        _raise_partial("采购入库", [], rejected)
    ids = []
    for row in normalized:
        rid = store.next_id(S.SHEET_IN, "IN-")
        row["入库ID"] = rid
        store.append_row(S.SHEET_IN, row)
        ids.append(rid)
    store.audit("purchase", operator, f"采购入库 {len(ids)} 条",
                f"用户确认: {confirmed_by}; 首单: {ids[0] if ids else '-'}", "", "成功")
    store.save()
    return {"posted": len(ids), "ids": ids, "rejected": []}


def confirm_cost(store: Store, in_ids: list[str], operator: str,
                 confirmed_by: str, basis: str) -> dict:
    """确认待确认采购成本的依据(单价/金额必须已有)。"""
    if not basis:
        raise LedgerError("E08", "成本确认必须提供确认依据(basis)")
    done = []
    for rid in in_ids:
        r = _by_id(store, S.SHEET_IN, "入库ID", rid)
        if r is None:
            raise LedgerError("E16", f"入库行不存在: {rid}")
        if r.get("成本状态") == S.COST_CONFIRMED:
            done.append(rid)
            continue
        store.ensure_period_open(r.get("期间"))
        if r.get("确认单价") in (None, "") and r.get("确认金额") in (None, ""):
            raise LedgerError("E04", f"入库行 {rid} 尚无单价/金额, 不能确认成本")
        store.update_cell(S.SHEET_IN, r["_row"], "成本状态", S.COST_CONFIRMED)
        done.append(rid)
    store.audit("confirm", operator, f"确认采购成本 {len(done)} 行", f"依据: {basis}",
                ",".join(done), "成功")
    store.save()
    return {"confirmed": len(done), "ids": done}


def correct_purchase(store: Store, in_id: str, operator: str, confirmed_by: str,
                     basis: str, new_price=None, new_amount=None) -> dict:
    """采购成本更正: 原行保留(状态=已更正), 写入更正行(更正关联ID=原行)。"""
    r = _by_id(store, S.SHEET_IN, "入库ID", in_id)
    if r is None:
        raise LedgerError("E16", f"入库行不存在: {in_id}")
    if r.get("成本状态") != S.COST_CONFIRMED:
        raise LedgerError("E08", f"入库行 {in_id} 尚未确认成本, 应先确认(或直接更正单价/金额)")
    if new_price in (None, "") and new_amount in (None, ""):
        raise LedgerError("E08", "更正必须提供新单价或新金额")
    if not basis:
        raise LedgerError("E08", "成本更正必须提供更正依据(basis)")
    store.ensure_period_open(r.get("期间"))
    p = price(new_price) if new_price not in (None, "") else price(r.get("确认单价"))
    amt = money(new_amount) if new_amount not in (None, "") else money(qty_num(r.get("数量")) * p)
    if new_amount not in (None, "") and abs(money(qty_num(r.get("数量")) * p) - amt) > Decimal("0.01"):
        raise LedgerError("E07", f"更正金额 {amt} 与 数量×单价 {money(qty_num(r.get('数量')) * p)} 不符")
    # 原行作废保留
    store.update_cell(S.SHEET_IN, r["_row"], "记录状态", S.RECORD_REPLACED)
    old_id = r.get("入库ID")
    new_id = store.next_id(S.SHEET_IN, "IN-")
    store.append_row(S.SHEET_IN, {
        "入库ID": new_id, "期间": r.get("期间"), "业务日期": r.get("业务日期"),
        "单号": r.get("单号"), "明细行号": r.get("明细行号"),
        "SKU": r.get("SKU"), "仓库": r.get("仓库"), "数量": r.get("数量"),
        "确认单价": p, "确认金额": amt, "附加费用分摊额": Decimal("0"),
        "成本状态": S.COST_CONFIRMED,
        "批次号": r.get("批次号", ""), "报关单号": r.get("报关单号", ""),
        "报关类型": r.get("报关类型", ""), "入库码": r.get("入库码", ""),
        "记录状态": S.RECORD_OK, "更正关联ID": old_id,
        "来源文件": "成本更正", "来源工作表": "", "来源行号": "",
        "文件摘要": "", "导入批次": "", "录入时间": now_str(),
        "操作人": operator, "用户确认": confirmed_by, "备注": basis,
    })
    store.audit("correct", operator,
                f"采购成本更正 {old_id} -> {new_id} (单价 {r.get('确认单价')} -> {p}, "
                f"金额 {r.get('确认金额')} -> {amt})",
                f"依据: {basis}; 原行保留为 {S.RECORD_REPLACED}", old_id, "成功")
    store.save()
    return {"old_id": old_id, "new_id": new_id, "price": str(p), "amount": str(amt)}


# ---------------------------------------------------------------- 销售出库
def normalize_sale(store: Store, r: dict, line: int,
                   src: dict | None = None, batch: str = "") -> dict:
    sku, wh = r.get("SKU") or r.get("sku"), r.get("仓库") or r.get("wh")
    _require_sku(store, sku)
    _require_wh(store, wh)
    _check_return(r, f"出库行{line}")
    biz_date = _require_date(r.get("业务日期"), line)
    period = period_of(biz_date)
    store.ensure_period_open(period)
    q = norm_qty(r.get("数量"))
    if q <= 0:
        raise LedgerError("E11", f"出库数量必须 > 0: 行{line}")
    doc = str(r.get("单号") or "").strip()
    lineno = str(r.get("明细行号") or "1").strip()
    if not doc:
        raise LedgerError("E08", f"出库单号不能为空: 行{line}")
    _check_doc_fingerprint(store, S.SHEET_OUT, period, doc, lineno, sku, wh)
    p, amt = _resolve_price_amount(r.get("销售单价"), r.get("销售金额"), q, f"出库行{line}")
    adj = r.get("销售价调整", "") if r.get("销售价调整") not in (None, "") else Decimal("0")
    adj = price(adj)
    actual = price((p or Decimal("0")) + adj)
    if p is None:
        actual = None
    return {
        "期间": period, "业务日期": biz_date, "单号": doc, "明细行号": lineno,
        "SKU": sku, "仓库": wh, "渠道": r.get("渠道", ""), "订单号": r.get("订单号", ""),
        "数量": q, "销售单价": p if p is not None else "",
        "销售金额": amt if p is not None else "",
        "销售价调整": adj, "销售价调整类型": r.get("销售价调整类型", ""),
        "实际销售价": actual if actual is not None else "",
        "物流单号": r.get("物流单号", ""), "出库成本": "",
        "记录状态": S.RECORD_OK, "更正关联ID": "",
        "来源文件": _src(src, "file"), "来源工作表": _src(src, "sheet"),
        "来源行号": _src(src, "row"), "文件摘要": _src(src, "hash"),
        "导入批次": batch, "录入时间": now_str(), "操作人": r.get("_operator", ""),
        "用户确认": r.get("_confirmed_by", ""), "备注": r.get("备注", ""),
    }


def add_sales(store: Store, rows: list[dict], operator: str, confirmed_by: str,
              src: dict | None = None, batch: str = "") -> dict:
    """销售出库: 行级负库存校验; 任一行失败则整体拒绝(不落库)。"""
    normalized = []
    rejected = []
    for i, r in enumerate(rows, start=1):
        r = dict(r)
        r["_operator"], r["_confirmed_by"] = operator, confirmed_by
        try:
            normalized.append(normalize_sale(store, r, i, src, batch))
        except LedgerError as ex:
            rejected.append((i, ex.code, ex.message))
    if rejected:
        _raise_partial("销售出库", [], rejected, None)
    _check_stock_available(store, normalized, "sale")
    ids = []
    for row in normalized:
        rid = store.next_id(S.SHEET_OUT, "OUT-")
        row["出库ID"] = rid
        store.append_row(S.SHEET_OUT, row)
        ids.append(rid)
    store.audit("sale", operator, f"销售出库 {len(ids)} 条",
                f"用户确认: {confirmed_by}", f"批次:{batch or '-'}", "成功")
    store.save()
    return {"posted": len(ids), "ids": ids}


# ---------------------------------------------------------------- 调拨
def normalize_transfer(store: Store, r: dict, line: int,
                       src: dict | None = None, batch: str = "") -> dict:
    sku = r.get("SKU") or r.get("sku")
    _require_sku(store, sku)
    fw, tw = r.get("调出仓库") or r.get("from_wh"), r.get("调入仓库") or r.get("to_wh")
    _require_wh(store, fw)
    _require_wh(store, tw)
    if fw == tw:
        raise LedgerError("E08", f"调出调入仓库不能相同: 行{line}")
    _check_return(r, f"调拨行{line}")
    biz_date = _require_date(r.get("业务日期"), line)
    period = period_of(biz_date)
    store.ensure_period_open(period)
    q = norm_qty(r.get("数量"))
    if q <= 0:
        raise LedgerError("E11", f"调拨数量必须 > 0: 行{line}")
    doc = str(r.get("调拨单号") or "").strip()
    lineno = str(r.get("明细行号") or "1").strip()
    if not doc:
        raise LedgerError("E08", f"调拨单号不能为空: 行{line}")
    # 调拨防重: 单据指纹(调拨单号+行号+SKU+调出+调入)
    for e in [x for x in store.read_all(S.SHEET_TRANSFER) if x.get("记录状态") == S.RECORD_OK]:
        if (e.get("期间") == period and e.get("调拨单号") == doc
                and e.get("明细行号") == lineno and e.get("SKU") == sku
                and e.get("调出仓库") == fw and e.get("调入仓库") == tw):
            raise LedgerError("E12", f"调拨单 {doc} 行 {lineno} 已存在({e.get('调拨ID')})")
    return {
        "期间": period, "业务日期": biz_date, "调拨单号": doc, "明细行号": lineno,
        "SKU": sku, "调出仓库": fw, "调入仓库": tw, "数量": q,
        "记录状态": S.RECORD_OK, "更正关联ID": "",
        "来源文件": _src(src, "file"), "来源工作表": _src(src, "sheet"),
        "来源行号": _src(src, "row"), "文件摘要": _src(src, "hash"),
        "导入批次": batch, "录入时间": now_str(), "操作人": r.get("_operator", ""),
        "用户确认": r.get("_confirmed_by", ""), "备注": r.get("备注", ""),
    }


def add_transfers(store: Store, rows: list[dict], operator: str,
                  confirmed_by: str, src: dict | None = None,
                  batch: str = "") -> dict:
    normalized, rejected = [], []
    for i, r in enumerate(rows, start=1):
        r = dict(r)
        r["_operator"], r["_confirmed_by"] = operator, confirmed_by
        try:
            normalized.append(normalize_transfer(store, r, i, src, batch))
        except LedgerError as ex:
            rejected.append((i, ex.code, ex.message))
    if rejected:
        _raise_partial("调拨", [], rejected, None)
    _check_stock_available(store, normalized, "transfer")
    ids = []
    for row in normalized:
        rid = store.next_id(S.SHEET_TRANSFER, "TR-")
        row["调拨ID"] = rid
        store.append_row(S.SHEET_TRANSFER, row)
        ids.append(rid)
    store.audit("transfer", operator, f"仓库调拨 {len(ids)} 条(双边原子写入)",
                f"用户确认: {confirmed_by}", f"批次:{batch or '-'}", "成功")
    store.save()
    return {"posted": len(ids), "ids": ids}


# ---------------------------------------------------------------- 库存调整
def normalize_adjust(store: Store, r: dict, line: int,
                     src: dict | None = None, batch: str = "") -> dict:
    sku, wh = r.get("SKU") or r.get("sku"), r.get("仓库") or r.get("wh")
    _require_sku(store, sku)
    _require_wh(store, wh)
    biz_date = _require_date(r.get("业务日期"), line)
    period = period_of(biz_date)
    store.ensure_period_open(period)
    atype = str(r.get("调整类型") or "").strip()
    if atype not in (S.ADJ_GAIN, S.ADJ_LOSS, S.ADJ_SCRAP, S.ADJ_FIX):
        raise LedgerError("E08", f"调整类型必须为 盘盈/盘亏/报废/受控更正: 行{line}")
    q = norm_qty(r.get("数量"))
    if q == 0:
        raise LedgerError("E08", f"调整数量不能为 0: 行{line}")
    doc = str(r.get("单号") or "").strip()
    if not doc:
        raise LedgerError("E08", f"调整单号不能为空: 行{line}")
    lineno = str(r.get("明细行号") or "1").strip()
    for e in [x for x in store.read_all(S.SHEET_ADJUST) if x.get("记录状态") == S.RECORD_OK]:
        if (e.get("期间") == period and e.get("单号") == doc
                and e.get("明细行号") == lineno and e.get("SKU") == sku
                and e.get("仓库") == wh):
            raise LedgerError("E12", f"调整单 {doc} 行 {lineno} 已存在({e.get('调整ID')})")
    basis_type = r.get("计价依据类型", "")
    basis_price = r.get("计价单价")
    if q > 0:  # 盘盈/正向更正: 必须有明确计价依据
        if not basis_type or basis_price in (None, ""):
            raise LedgerError("E04", f"正数量调整(盘盈/更正+)必须提供计价依据类型与单价: 行{line}")
        bp = price(basis_price)
        if bp < 0:
            raise LedgerError("E08", f"计价单价不能为负: 行{line}")
        ba = money(q * bp)
        given = r.get("计价金额")
        if given not in (None, "") and abs(money(given) - ba) > Decimal("0.01"):
            raise LedgerError("E07", f"计价金额 {money(given)} 与 数量×单价 {ba} 不符: 行{line}")
    else:
        bp, ba = Decimal("0"), Decimal("0")
    return {
        "期间": period, "业务日期": biz_date, "调整类型": atype,
        "单号": doc, "明细行号": lineno, "SKU": sku, "仓库": wh, "数量": q,
        "计价依据类型": basis_type, "计价单价": bp if q > 0 else "",
        "计价金额": ba if q > 0 else "",
        "记录状态": S.RECORD_OK, "更正关联ID": "",
        "来源文件": _src(src, "file"), "来源工作表": _src(src, "sheet"),
        "来源行号": _src(src, "row"), "文件摘要": _src(src, "hash"),
        "导入批次": batch, "录入时间": now_str(), "操作人": r.get("_operator", ""),
        "用户确认": r.get("_confirmed_by", ""), "备注": r.get("备注", ""),
    }


def add_adjustments(store: Store, rows: list[dict], operator: str,
                    confirmed_by: str, src: dict | None = None,
                    batch: str = "") -> dict:
    normalized, rejected = [], []
    for i, r in enumerate(rows, start=1):
        r = dict(r)
        r["_operator"], r["_confirmed_by"] = operator, confirmed_by
        try:
            normalized.append(normalize_adjust(store, r, i, src, batch))
        except LedgerError as ex:
            rejected.append((i, ex.code, ex.message))
    if rejected:
        _raise_partial("库存调整", [], rejected, None)
    # 负向调整实时校验
    _check_stock_available(store, normalized, "adjust")
    ids = []
    for row in normalized:
        rid = store.next_id(S.SHEET_ADJUST, "ADJ-")
        row["调整ID"] = rid
        store.append_row(S.SHEET_ADJUST, row)
        ids.append(rid)
    store.audit("adjust", operator, f"库存调整 {len(ids)} 条",
                f"用户确认: {confirmed_by}", f"批次:{batch or '-'}", "成功")
    store.save()
    return {"posted": len(ids), "ids": ids}


# ---------------------------------------------------------------- 附加费用
def add_fee(store: Store, rows: list[dict], operator: str, confirmed_by: str,
            src: dict | None = None, batch: str = "") -> dict:
    posted, rejected = [], []
    for i, r in enumerate(rows, start=1):
        try:
            biz_date = _require_date(r.get("业务日期"), i)
            period = period_of(biz_date)
            store.ensure_period_open(period)
            rule = str(r.get("分摊规则") or "").strip()
            if rule not in (S.FEE_RULE_QTY, S.FEE_RULE_AMOUNT, S.FEE_RULE_EXPLICIT):
                raise LedgerError("E08", f"分摊规则必须为 按数量比例/按金额比例/逐明细指定: 行{i}")
            total = money(r.get("费用总额"))
            if total <= 0:
                raise LedgerError("E08", f"费用总额必须 > 0: 行{i}")
            doc = str(r.get("费用单号") or "").strip()
            in_doc = str(r.get("关联采购单号") or "").strip()
            if not doc or not in_doc:
                raise LedgerError("E08", f"费用单号/关联采购单号不能为空: 行{i}")
            # 费用单号防重
            for e in store.read_all(S.SHEET_FEE):
                if e.get("费用单号") == doc:
                    raise LedgerError("E12", f"费用单 {doc} 已存在({e.get('费用ID')})")
            fid = store.next_id(S.SHEET_FEE, "FEE-")
            store.append_row(S.SHEET_FEE, {
                "费用ID": fid, "期间": period, "费用单号": doc,
                "关联采购单号": in_doc, "分摊规则": rule, "费用总额": total,
                "分摊状态": "未分摊",
                "来源文件": _src(src, "file"), "来源工作表": _src(src, "sheet"),
                "来源行号": _src(src, "row"), "文件摘要": _src(src, "hash"),
                "导入批次": batch, "录入时间": now_str(), "操作人": operator,
                "用户确认": confirmed_by, "备注": r.get("备注", ""),
            })
            posted.append(fid)
        except LedgerError as ex:
            rejected.append((i, ex.code, ex.message))
    if rejected:
        _raise_partial("附加费用", [], rejected, None)
    store.audit("fee", operator, f"登记附加费用 {len(posted)} 张(待分摊)",
                f"用户确认: {confirmed_by}", "", "成功")
    store.save()
    return {"posted": len(posted), "ids": posted}


def plan_fee_allocation(store: Store, fee_id: str,
                        explicit: list[tuple[str, Decimal]] | None = None) -> dict:
    """费用分摊方案(只读, 供预览与执行共用)。返回 {fee, rows, alloc} 或抛异常。"""
    from .decimal_utils import largest_remainder_allocate
    f = _by_id(store, S.SHEET_FEE, "费用ID", fee_id)
    if f is None:
        raise LedgerError("E16", f"费用单不存在: {fee_id}")
    rows = [r for r in store.read_all(S.SHEET_IN)
            if r.get("单号") == f.get("关联采购单号") and r.get("记录状态") == S.RECORD_OK]
    if not rows:
        raise LedgerError("E09", f"关联采购单 {f.get('关联采购单号')} 无有效入库行")
    total = money(f.get("费用总额"))
    rule = f.get("分摊规则")
    if rule == S.FEE_RULE_EXPLICIT:
        if not explicit:
            raise LedgerError("E08", f"逐明细指定需提供 (入库ID, 金额) 列表: {fee_id}")
        by_id = {r.get("入库ID"): r for r in rows}
        alloc = {}
        for rid, amt in explicit:
            if rid not in by_id:
                raise LedgerError("E16", f"逐明细指定的入库行不存在/不在关联单中: {rid}")
            alloc[rid] = money(amt)
        if sum(alloc.values(), Decimal("0")) != total:
            raise LedgerError("E07",
                              f"逐明细指定合计 {sum(alloc.values(), Decimal('0'))} != 费用总额 {total}")
    else:
        if rule == S.FEE_RULE_QTY:
            weights = [qty_num(r.get("数量")) for r in rows]
        else:
            weights = [qty_num(r.get("确认金额")) for r in rows]
        allocated = largest_remainder_allocate(weights, total)
        alloc = {r.get("入库ID"): v for r, v in zip(rows, allocated)}
    return {"fee_id": fee_id, "rule": rule, "related_doc": f.get("关联采购单号"),
            "period": f.get("期间"), "total": str(total), "fee_status": f.get("分摊状态"),
            "rows": [{"入库ID": r.get("入库ID"), "数量": str(r.get("数量")),
                      "金额": str(r.get("确认金额"))} for r in rows],
            "alloc": {k: str(v) for k, v in alloc.items()}}


def allocate_fee(store: Store, fee_id: str, operator: str, confirmed_by: str,
                 explicit: list[tuple[str, Decimal]] | None = None) -> dict:
    f = _by_id(store, S.SHEET_FEE, "费用ID", fee_id)
    if f is None:
        raise LedgerError("E16", f"费用单不存在: {fee_id}")
    store.ensure_period_open(f.get("期间"))
    if f.get("分摊状态") == "已分摊":
        raise LedgerError("E12", f"费用单 {fee_id} 已分摊, 禁止重复分摊")
    plan = plan_fee_allocation(store, fee_id, explicit)
    alloc = {k: money(v) for k, v in plan["alloc"].items()}
    for r in store.read_all(S.SHEET_IN):
        if r.get("入库ID") in alloc:
            store.update_cell(S.SHEET_IN, r["_row"], "附加费用分摊额", alloc[r.get("入库ID")])
    store.update_cell(S.SHEET_FEE, f["_row"], "分摊状态", "已分摊")
    store.audit("fee_alloc", operator,
                f"附加费用分摊 {fee_id} -> {plan['related_doc']} "
                f"共 {len(alloc)} 行, 规则 {plan['rule']}, 总额 {plan['total']}",
                f"用户确认: {confirmed_by}; 明细: {alloc}", fee_id, "成功")
    store.save()
    return {"fee_id": fee_id, "rule": plan["rule"], "total": str(plan["total"]),
            "alloc": {k: str(v) for k, v in alloc.items()}}


# ---------------------------------------------------------------- 通用校验
def _require_sku(store: Store, sku) -> None:
    if not sku:
        raise LedgerError("E01", "SKU 为空")
    if not any(r.get("SKU") == sku for r in store.read_all(S.SHEET_SKU)):
        raise LedgerError("E01", f"SKU 不存在: {sku}")


def _require_wh(store: Store, wh) -> None:
    if not wh:
        raise LedgerError("E02", "仓库为空")
    if not any(r.get("仓库代码") == wh for r in store.read_all(S.SHEET_WH)):
        raise LedgerError("E02", f"仓库不存在: {wh}")


def _check_unit(store: Store, sku: str, r: dict, line: int) -> None:
    for alias in UNIT_FIELD_ALIASES:
        u = r.get(alias)
        if u not in (None, ""):
            master = next((x.get("基本单位") for x in store.read_all(S.SHEET_SKU)
                           if x.get("SKU") == sku), None)
            if master and str(u).strip() != str(master).strip():
                raise LedgerError("E05",
                                  f"计量单位不匹配: SKU {sku} 基本单位 {master}, "
                                  f"提供 {u} (行{line})")
            return


def _check_return(r: dict, label: str) -> None:
    blob = " ".join(str(r.get(k) or "") for k in
                    ("备注", "单号", "订单号", "渠道", "类型", "业务日期"))
    low = blob.lower()
    for kw in RETURN_KEYWORDS:
        if kw in low:
            raise LedgerError("E11", f"{label} 含退货语义({kw}), V1 不支持退货")


def _require_date(d, line: int) -> str:
    d = str(d or "").strip()
    if not d or not is_date_valid(d):
        raise LedgerError("E08", f"业务日期必须为 YYYY-MM-DD: 行{line} ({d})")
    # 拒绝退货以外的日期异常: 自然月边界由期间推导保证
    return d


def _check_doc_fingerprint(store: Store, sheet: str, period: str, doc: str,
                           lineno: str, sku: str, wh: str) -> None:
    id_field = {"入库流水": "入库ID", "出库流水": "出库ID"}.get(sheet, "")
    for e in [x for x in store.read_all(sheet) if x.get("记录状态") == S.RECORD_OK]:
        if (e.get("期间") == period and e.get("单号") == doc
                and e.get("明细行号") == lineno and e.get("SKU") == sku
                and e.get("仓库") == wh):
            raise LedgerError("E12",
                              f"单据冲突: {sheet} 单号 {doc} 行 {lineno} SKU {sku} @ {wh} "
                              f"已存在({e.get(id_field, '?')})")


def _resolve_price_amount(price_v, amount_v, q: Decimal, label: str):
    p, amt = None, None
    if price_v not in (None, ""):
        p = price(price_v)
        if p < 0:
            raise LedgerError("E08", f"单价不能为负: {label}")
        amt = money(q * p)
    if amount_v not in (None, ""):
        a = money(amount_v)
        if a < 0:
            raise LedgerError("E08", f"金额不能为负: {label}")
        if p is None:
            p = price(a / q)
            amt = a
        elif abs(a - amt) > Decimal("0.005"):
            raise LedgerError("E07", f"{label} 金额 {a} 与 数量×单价 {amt} 不符(差 > 0.005)")
        else:
            amt = a
    return p, amt


def qty_num(v) -> Decimal:
    return v if isinstance(v, Decimal) else norm_qty(v)


def _by_id(store: Store, sheet: str, id_field: str, rid: str) -> dict | None:
    for r in store.read_all(sheet):
        if r.get(id_field) == rid:
            return r
    return None


def _src(src: dict | None, key: str) -> str:
    return str((src or {}).get(key) or "")


def _raise_partial(op: str, posted: list, rejected: list) -> None:
    if rejected:
        msg = "; ".join(f"行{r}: [{c}] {m}" for r, c, m in rejected)
        raise LedgerError("E09", f"{op}: {len(rejected)} 行被拒绝, 本批未写入任何记录. {msg}")


def check_stock_snapshot(store: Store, period: str) -> dict:
    """当前期间暂估分仓库存(供查询与预览)。"""
    report = compute_month(store, period)
    out = {}
    for (sku, wh), w in sorted(report.warehouses.items()):
        out[(sku, wh)] = w
    return out


def _check_stock_available(store: Store, normalized: list[dict], kind: str) -> None:
    """按 (业务日期, 录入时间, ID) 时序校验调出/出库/负向调整不产生负库存。

    kind: "sale" 减少; "adjust" 按行符号; "transfer" 仅调出侧检查。
    """
    if not normalized:
        return
    period = normalized[0].get("期间")
    report = compute_month(store, period)
    balances: dict[tuple[str, str], Decimal] = {
        (sku, wh): w.open_qty for (sku, wh), w in report.warehouses.items()}

    def events_for() -> dict:
        evs: dict[tuple[str, str], list[tuple]] = {}
        for r in store.read_all(S.SHEET_IN):
            if r.get("期间") == period and r.get("记录状态") == S.RECORD_OK:
                evs.setdefault((r.get("SKU"), r.get("仓库")), []).append(
                    (r.get("业务日期") or "", r.get("录入时间") or "",
                     r.get("入库ID"), qty_num(r.get("数量"))))
        for r in store.read_all(S.SHEET_OUT):
            if r.get("期间") == period and r.get("记录状态") == S.RECORD_OK:
                evs.setdefault((r.get("SKU"), r.get("仓库")), []).append(
                    (r.get("业务日期") or "", r.get("录入时间") or "",
                     r.get("出库ID"), -qty_num(r.get("数量"))))
        for r in store.read_all(S.SHEET_ADJUST):
            if r.get("期间") == period and r.get("记录状态") == S.RECORD_OK:
                evs.setdefault((r.get("SKU"), r.get("仓库")), []).append(
                    (r.get("业务日期") or "", r.get("录入时间") or "",
                     r.get("调整ID"), qty_num(r.get("数量"))))
        for r in store.read_all(S.SHEET_TRANSFER):
            if r.get("期间") == period and r.get("记录状态") == S.RECORD_OK:
                evs.setdefault((r.get("SKU"), r.get("调出仓库")), []).append(
                    (r.get("业务日期") or "", r.get("录入时间") or "",
                     r.get("调拨ID"), -qty_num(r.get("数量"))))
                evs.setdefault((r.get("SKU"), r.get("调入仓库")), []).append(
                    (r.get("业务日期") or "", r.get("录入时间") or "",
                     r.get("调拨ID"), qty_num(r.get("数量"))))
        return evs

    # 先应用期间内既有流水(将本批视为发生在最后, 保守且确定)
    for (sku, wh), evs in events_for().items():
        k = (sku, wh)
        bal = balances.get(k, Decimal("0"))
        for e in sorted(evs):
            bal += e[3]
            if bal < 0:
                # 既有流水已负 -> 不重复报, 交期间检查报告
                break
        balances[k] = bal

    for i, row in enumerate(normalized):
        sku = row.get("SKU")
        q = qty_num(row.get("数量"))
        if kind == "transfer":
            fw = row.get("调出仓库")
            b = balances.get((sku, fw), Decimal("0"))
            if b - q < 0:
                raise LedgerError("E03", f"调出仓库 {fw} SKU {sku} 可用 {b} 不足 {q}")
            balances[(sku, fw)] = b - q
            balances[(sku, row.get("调入仓库"))] =                 balances.get((sku, row.get("调入仓库")), Decimal("0")) + q
            continue
        wh = row.get("仓库")
        delta = -q if kind == "sale" else q
        k = (sku, wh)
        b = balances.get(k, Decimal("0"))
        if b + delta < 0:
            raise LedgerError("E03", f"仓库 {wh} SKU {sku} 库存不足: 现有 {b}, 本次 {delta}")
        balances[k] = b + delta
