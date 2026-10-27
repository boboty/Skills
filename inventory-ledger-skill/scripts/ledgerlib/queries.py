"""查询层: 库存/流水/月度/异常/待确认, 一律实时重算, 区分暂估与已结账。"""
from __future__ import annotations

from . import schema as S
from .engine import compute_month, _materialized
from .store import Store

QUERY_SHEETS = {
    "in": S.SHEET_IN, "out": S.SHEET_OUT, "transfer": S.SHEET_TRANSFER,
    "adjust": S.SHEET_ADJUST, "opening": S.SHEET_OPENING, "fee": S.SHEET_FEE,
}


def query_stock(store: Store, period: str | None = None,
                sku: str | None = None, wh: str | None = None) -> dict:
    """分仓库存(期末) + 公司级金额。period 缺省取最近有活动的期间。"""
    period = period or _latest_active_period(store)
    report = compute_month(store, period)
    closed = (store.get_period(period) or {}).get("状态") == S.PERIOD_CLOSED
    rows = []
    for (s, w), wq in sorted(report.warehouses.items()):
        if sku and s != sku:
            continue
        if wh and w != wh:
            continue
        rows.append({
            "期间": period, "SKU": s, "仓库": w,
            "期初数量": str(wq.open_qty), "计价入库数量": str(wq.in_qty),
            "出库数量": str(wq.sales_qty), "调整数量": str(wq.adjust_qty),
            "调拨净": str(wq.transfer_net), "期末数量": str(wq.end_qty),
        })
    company = []
    for sk, m in sorted(report.months.items()):
        if sku and sk != sku:
            continue
        company.append({
            "SKU": sk, "期末数量": str(m.end_qty), "期末金额": str(m.end_amount),
            "加权单价": str(m.avg_price) if m.avg_price is not None else None,
            "待确认数量": str(m.pending_qty),
        })
    return {"期间": period, "状态": "已结账" if closed else "暂估",
            "仓库": rows, "公司": company}


def query_flows(store: Store, period: str | None = None,
                kind: str | None = None, sku: str | None = None) -> dict:
    sheet = QUERY_SHEETS.get(kind or "")
    out = {}
    for k, sh in QUERY_SHEETS.items():
        if sheet and sh != sheet:
            continue
        rows = []
        for r in store.read_all(sh):
            if period and r.get("期间") != period:
                continue
            if sku and r.get("SKU") != sku:
                continue
            rows.append({f: str(v) for f, v in r.items() if f != "_row"})
        out[k] = rows
    return out


def query_monthly(store: Store, period: str) -> dict:
    report = compute_month(store, period)
    closed = (store.get_period(period) or {}).get("状态") == S.PERIOD_CLOSED
    materialized = _materialized(store, period) if closed else None
    skus = []
    for sk, m in sorted(report.months.items()):
        row = {
            "SKU": sk, "期初数量": str(m.open_qty), "期初金额": str(m.open_amount),
            "计价入库数量": str(m.in_qty + m.gain_qty),
            "计价入库金额": str(m.in_amount + m.gain_amount),
            "附加费用分摊额": str(m.fee), "出库数量": str(m.sales_qty),
            "出库成本": str(m.out_cost), "期末数量": str(m.end_qty),
            "期末金额": str(m.end_amount),
            "加权单价": str(m.avg_price) if m.avg_price is not None else None,
        }
        if materialized and sk in materialized:
            row["物化期末金额"] = str(materialized[sk].get("期末金额") or "")
        skus.append(row)
    return {"期间": period, "状态": "已结账" if closed else "暂估", "SKU": skus}


def query_exceptions(store: Store, period: str) -> dict:
    report = compute_month(store, period)
    return {"期间": period,
            "异常": [{"code": c, "message": m} for c, m in report.exceptions]}


def query_pending(store: Store, period: str | None = None) -> dict:
    rows = []
    for r in store.read_all(S.SHEET_IN):
        if r.get("成本状态") != S.COST_CONFIRMED:
            if period and r.get("期间") != period:
                continue
            rows.append({"入库ID": r.get("入库ID"), "期间": r.get("期间"),
                         "单号": r.get("单号"), "SKU": r.get("SKU"),
                         "仓库": r.get("仓库"), "数量": str(r.get("数量"))})
    return {"待确认记录": rows, "count": len(rows)}


def status(store: Store) -> dict:
    periods = []
    for r in store.read_all(S.SHEET_PERIODS):
        periods.append({"期间": r.get("期间"), "状态": r.get("状态"),
                        "核算版本": r.get("核算版本"), "更新时间": r.get("更新时间")})
    periods.sort(key=lambda x: x["期间"])
    return {
        "公司": store.meta("公司名称"), "币种": store.meta("币种"),
        "成本方法": store.meta("成本方法"), "起始期间": store.meta("起始期间"),
        "精度": {"数量": store.meta("数量精度"), "单价": store.meta("单价精度"),
                 "金额": store.meta("金额精度"), "舍入": store.meta("舍入模式")},
        "期间": periods,
    }


def _latest_active_period(store: Store) -> str:
    periods = set()
    for sh in (S.SHEET_IN, S.SHEET_OUT, S.SHEET_TRANSFER, S.SHEET_ADJUST):
        for r in store.read_all(sh):
            if r.get("期间"):
                periods.add(r.get("期间"))
    if periods:
        return max(periods)
    start = store.meta(S.META_START_PERIOD)
    d = store.get_period(start)
    return start if d else ""