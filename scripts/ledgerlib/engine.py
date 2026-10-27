"""月度成本引擎: 自然月末一次加权平均的计算/结账/重开/对账。

口径(对应 references/business-rules.md):
- 成本池(公司+SKU): 期初金额 + 本月已确认计价入库金额(采购确认 + 正向调整计价) + 附加费用分摊额。
- 加权单价(4 位) = 成本池金额 / 成本池数量; 池数量 = 期初数量 + 已确认采购数量 + 正向调整数量。
- 出库成本(销售 + 盘亏/报废/负向更正)按统一月末加权单价计算, 尾差用最大余数法分配到行。
- 期末金额 = 成本池金额 − 出库成本总额(恒等守恒); E07 校验 期末金额 ≈ round2(期末数量×单价) 差 ≤ 0.01。
- 期末数量(物理) = 期初 + 计价入库(采购, 结账期即已确认) − 出库(销售) + 调整(净额) + 调拨净。
- 调拨在公司层面数量/金额净变化为零; 不参与成本池。
- 期初衔接: 首期取期初库存, 其他期间取上一期间期末(已结账则与物化结存核对)。
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal

from . import schema as S
from .decimal_utils import money, price, qty, largest_remainder_allocate, CENT
from .store import Store, LedgerError, is_period_valid, prev_period, next_period, now_str

# 例外清单: 期间内的输入性异常(记账时已拦截的除外)
EXC_SKU_MISSING = "E01"
EXC_WH_MISSING = "E02"
EXC_NEGATIVE = "E03"
EXC_COST_MISSING = "E04"
EXC_UNIT = "E05"
EXC_ZERO_STOCK_AMOUNT = "E06"
EXC_TAIL_OVER = "E07"
EXC_PERIOD = "E08"
EXC_DUPLICATE = "E12"
EXC_SEQUENCE = "E13"
EXC_DENOMINATOR = "E14"


@dataclass
class ConsumptionRow:
    """一笔按统一加权成本计价的出库/损耗行。"""
    kind: str            # "sale" | "loss"
    key: str             # 记录 ID
    sku: str
    wh: str
    biz_date: str
    created_at: str
    qty: Decimal         # 正数
    row_number: int      # 物理行号(用于结账写回)
    cost: Decimal = Decimal("0")  # 结账时写回的成本(分摊后)


@dataclass
class SkuMonth:
    sku: str
    period: str
    open_qty: Decimal = Decimal("0")
    open_amount: Decimal = Decimal("0")
    in_qty: Decimal = Decimal("0")        # 采购确认数量
    in_amount: Decimal = Decimal("0")     # 采购确认金额
    gain_qty: Decimal = Decimal("0")      # 正向调整(盘盈/更正+)数量
    gain_amount: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    sales_qty: Decimal = Decimal("0")
    loss_qty: Decimal = Decimal("0")
    pending_qty: Decimal = Decimal("0")   # 待确认采购物理数量
    transfer_net: Decimal = Decimal("0")  # 公司级恒为 0
    out_cost: Decimal = Decimal("0")
    avg_price: Decimal | None = None      # 4 位; None=本池无动因
    consumption: list[ConsumptionRow] = field(default_factory=list)
    end_amount: Decimal = Decimal("0")

    @property
    def pool_qty(self) -> Decimal:
        return self.open_qty + self.in_qty + self.gain_qty

    @property
    def pool_amount(self) -> Decimal:
        return self.open_amount + self.in_amount + self.gain_amount + self.fee

    @property
    def adjust_qty(self) -> Decimal:
        return self.gain_qty - self.loss_qty

    @property
    def end_qty(self) -> Decimal:
        return (self.open_qty + self.in_qty + self.pending_qty
                + self.adjust_qty + self.transfer_net - self.sales_qty)


@dataclass
class WhQty:
    sku: str
    wh: str
    open_qty: Decimal = Decimal("0")
    in_qty: Decimal = Decimal("0")         # 采购(物理)
    sales_qty: Decimal = Decimal("0")
    gain_qty: Decimal = Decimal("0")
    loss_qty: Decimal = Decimal("0")
    transfer_in: Decimal = Decimal("0")
    transfer_out: Decimal = Decimal("0")

    @property
    def adjust_qty(self) -> Decimal:
        return self.gain_qty - self.loss_qty

    @property
    def transfer_net(self) -> Decimal:
        return self.transfer_in - self.transfer_out

    @property
    def end_qty(self) -> Decimal:
        return (self.open_qty + self.in_qty + self.adjust_qty
                + self.transfer_net - self.sales_qty)


class CloseReport:
    """试结账/结账结果: 各 SKU 月度结果 + 仓库数量 + 异常清单。"""

    def __init__(self, period: str):
        self.period = period
        self.months: dict[str, SkuMonth] = {}
        self.warehouses: dict[tuple[str, str], WhQty] = {}
        self.exceptions: list[tuple[str, str]] = []  # (code, message)
        self.tentative = False

    def add_exc(self, code: str, msg: str) -> None:
        self.exceptions.append((code, msg))

    @property
    def ok(self) -> bool:
        return not self.exceptions


def _active_rows(store: Store, sheet: str, period: str) -> list[dict]:
    """指定期间内状态=正常 的流水行(流水表记录状态字段为 记录状态)。"""
    return [r for r in store.read_all(sheet)
            if r.get("期间") == period
            and r.get("记录状态", S.RECORD_OK) in (S.RECORD_OK,)]


def _num(r: dict, field: str) -> Decimal:
    from .decimal_utils import as_decimal
    return as_decimal(r.get(field))


def _sku_unit(store: Store, sku: str) -> str | None:
    for r in store.read_all(S.SHEET_SKU):
        if r.get("SKU") == sku:
            return r.get("基本单位")
    return None


def compute_month(store: Store, period: str, _depth: int = 0) -> CloseReport:
    """计算一个自然月的全部结果(暂估口径, 不写盘)。"""
    if not is_period_valid(period):
        raise LedgerError("E08", f"期间格式应为 {S.PERIOD_FMT}: {period}")
    if _depth > 120:
        raise LedgerError("E10", "期初衔接递归过深, 检查期间链")
    start = store.meta(S.META_START_PERIOD)
    report = CloseReport(period)
    months: dict[str, SkuMonth] = report.months
    wh_qty: dict[tuple[str, str], WhQty] = report.warehouses

    # ---------- 1. 月初: 首期用期初库存, 其他期间用上月期末 ----------
    opens: dict[str, tuple[Decimal, Decimal]] = {}
    if period == start:
        for r in store.read_all(S.SHEET_OPENING):
            if r.get("起始期间") != start or r.get("状态") != S.RECORD_OK:
                continue
            sku, wh = r.get("SKU"), r.get("仓库")
            oq, oa = opens.get(sku, (Decimal("0"), Decimal("0")))
            opens[sku] = (oq + _num(r, "数量"), oa + _num(r, "金额"))
            k = (sku, wh)
            w = wh_qty.setdefault(k, WhQty(sku=sku, wh=wh))
            w.open_qty += _num(r, "数量")
    else:
        prev = prev_period(period)
        prev_report = compute_month(store, prev, _depth + 1)
        prev_exc = [e for e in prev_report.exceptions if e[0] != EXC_SEQUENCE]
        if prev_exc:
            for code, msg in prev_exc:
                report.add_exc(code, f"上月(含)异常影响本期: {msg}")
        mat = _materialized(store, prev)
        for sku, m in prev_report.months.items():
            if m.avg_price is not None:
                opens[sku] = (m.end_qty, m.end_amount)
            else:
                opens[sku] = (m.end_qty, Decimal("0"))
            if mat is not None:
                mrow = mat.get(sku)
                if mrow is not None:
                    mat_end = _num(mrow, "期末数量")
                    mat_amt = _num(mrow, "期末金额")
                    if mat_end != m.end_qty or mat_amt != m.end_amount:
                        report.add_exc("E16", f"{prev} 已结账结存与流水重算不一致 "
                                                  f"(数量 {mat_end} vs {m.end_qty}, 金额 {mat_amt} vs {m.end_amount})")
        # 仓库数量: 用上月逐仓期末
        for (sku, wh), w in prev_report.warehouses.items():
            k = (sku, wh)
            nw = wh_qty.setdefault(k, WhQty(sku=sku, wh=wh))
            nw.open_qty += w.end_qty
        # 上月异常中"负库存"之类需带到本期检查? 由上月报告携带即可
    for sku, (oq, oa) in opens.items():
        m = months.setdefault(sku, SkuMonth(sku=sku, period=period))
        m.open_qty, m.open_amount = oq, oa

    # ---------- 2. 入库流水(采购) ----------
    for r in _active_rows(store, S.SHEET_IN, period):
        sku = r.get("SKU")
        m = months.setdefault(sku, SkuMonth(sku=sku, period=period))
        q = _num(r, "数量")
        if q <= 0:
            report.add_exc("E11", f"入库行 {r.get('入库ID')} 数量 {q} <= 0 (退货语义, V1 不支持)")
            continue
        # 仓库物理
        w = wh_qty.setdefault((sku, r.get("仓库")), WhQty(sku=sku, wh=r.get("仓库")))
        w.in_qty += q
        m.pending_qty += q
        if r.get("成本状态") == S.COST_CONFIRMED:
            amt = _num(r, "确认金额")
            m.in_qty += q
            m.in_amount += amt
            m.pending_qty -= q
            m.fee += _num(r, "附加费用分摊额")
        # E04: 待确认成本 —— 结账检查

    # ---------- 3. 调整流水(盘盈/盘亏/报废/受控更正) ----------
    for r in _active_rows(store, S.SHEET_ADJUST, period):
        sku = r.get("SKU")
        m = months.setdefault(sku, SkuMonth(sku=sku, period=period))
        q = _num(r, "数量")
        w = wh_qty.setdefault((sku, r.get("仓库")), WhQty(sku=sku, wh=r.get("仓库")))
        if q > 0:  # 盘盈/更正+: 进入计价池, 必须有计价依据
            amt = _num(r, "计价金额")
            if amt <= 0 and q > 0:
                # 计价依据类型缺失或金额为 0 -> E04(见下), 但仍按 0 计入? 不: 数额为0的盘盈视同无依据
                if not (r.get("计价依据类型") and _num(r, "计价单价") >= 0):
                    report.add_exc(EXC_COST_MISSING,
                                   f"盘盈行 {r.get('调整ID')} 缺少明确成本计价依据")
            w.gain_qty += q
            m.gain_qty += q
            m.gain_amount += amt
        elif q < 0:  # 盘亏/报废/更正-: 损耗, 按当月统一加权
            w.loss_qty += (-q)
            m.loss_qty += (-q)
        else:
            report.add_exc("E08", f"调整行 {r.get('调整ID')} 数量为零")

    # ---------- 4. 销售出库 ----------
    for r in _active_rows(store, S.SHEET_OUT, period):
        sku = r.get("SKU")
        m = months.setdefault(sku, SkuMonth(sku=sku, period=period))
        q = _num(r, "数量")
        if q <= 0:
            report.add_exc("E11", f"出库行 {r.get('出库ID')} 数量 {q} <= 0 (退货语义, V1 不支持)")
            continue
        w = wh_qty.setdefault((sku, r.get("仓库")), WhQty(sku=sku, wh=r.get("仓库")))
        w.sales_qty += q
        m.sales_qty += q
        m.consumption.append(ConsumptionRow(
            kind="sale", key=r.get("出库ID"), sku=sku, wh=r.get("仓库"),
            biz_date=r.get("业务日期") or "", created_at=r.get("录入时间") or "",
            qty=q, row_number=r.get("_row", 0)))

    # ---------- 5. 调拨(公司级净零, 仓库级变更) ----------
    for r in _active_rows(store, S.SHEET_TRANSFER, period):
        sku = r.get("SKU")
        q = _num(r, "数量")
        if q <= 0:
            report.add_exc("E11", f"调拨行 {r.get('调拨ID')} 数量 {q} <= 0 (退货语义, V1 不支持)")
            continue
        fw, tw = r.get("调出仓库"), r.get("调入仓库")
        if fw == tw:
            report.add_exc("E08", f"调拨行 {r.get('调拨ID')} 调出调入仓库相同")
            continue
        wf = wh_qty.setdefault((sku, fw), WhQty(sku=sku, wh=fw))
        wt = wh_qty.setdefault((sku, tw), WhQty(sku=sku, wh=tw))
        wf.transfer_out += q
        wt.transfer_in += q
        m = months.setdefault(sku, SkuMonth(sku=sku, period=period))
        m.transfer_net += Decimal("0")  # 公司级恒 0

    # ---------- 6. 出库成本分配(含销售与损耗, 统一月末加权) ----------
    for sku, m in months.items():
        cons = [c for c in m.consumption]
        # 损耗行加入消耗池
        for r in _active_rows(store, S.SHEET_ADJUST, period):
            if r.get("SKU") == sku and _num(r, "数量") < 0:
                cons.append(ConsumptionRow(
                    kind="loss", key=r.get("调整ID"), sku=sku, wh=r.get("仓库"),
                    biz_date=r.get("业务日期") or "", created_at=r.get("录入时间") or "",
                    qty=-_num(r, "数量"), row_number=0))
        # 冻结规则: 行序=记录ID升序, 确定可复现
        cons.sort(key=lambda c: c.key)
        m.consumption = cons
        total_out_qty = sum((c.qty for c in cons), Decimal("0"))
        if m.pool_qty == 0 and (total_out_qty > 0 or m.pool_amount != 0):
            if total_out_qty > 0:
                report.add_exc(EXC_DENOMINATOR,
                               f"SKU {sku}: 加权单价零分母(池数量 0)但存在出库/损耗 {total_out_qty}")
            if m.pool_amount != 0:
                report.add_exc(EXC_ZERO_STOCK_AMOUNT,
                               f"SKU {sku}: 成本池金额 {m.pool_amount} 但池数量为 0")
        if total_out_qty > 0 and m.pool_qty > 0:
            m.avg_price = price(m.pool_amount / m.pool_qty)
            weights = [c.qty * m.avg_price for c in cons]
            target = money(m.avg_price * total_out_qty)
            allocated = largest_remainder_allocate(weights, target)
            for c, v in zip(cons, allocated):
                c.cost = v
            m.out_cost = sum(allocated, Decimal("0"))
        elif m.avg_price is None and m.pool_qty > 0:
            m.avg_price = price(m.pool_amount / m.pool_qty)
        # 期末金额(恒等守恒)
        m.end_amount = money(m.pool_amount - m.out_cost)

    # ---------- 7. 期间级检查 ----------
    if period != start:
        if store.get_period(prev_period(period)) is None:
            report.add_exc(EXC_PERIOD, f"期间 {period} 缺少上一期间登记")
        elif store.get_period(prev_period(period)).get("状态") != S.PERIOD_CLOSED:
            report.add_exc(EXC_SEQUENCE, f"期间 {prev_period(period)} 未结账, 不能正式结账 {period}")
    else:
        if store.get_period(period) is None:
            report.add_exc(EXC_PERIOD, f"起始期间 {period} 未登记")

    # 待确认成本
    for r in _active_rows(store, S.SHEET_IN, period):
        if r.get("成本状态") != S.COST_CONFIRMED:
            report.add_exc(EXC_COST_MISSING,
                           f"入库行 {r.get('入库ID')} 成本待确认, 须确认后方可结账")

    # 附加费用核对: 未分摊 -> E04; 已分摊则 Σ行分摊额 = 费用总额
    for f in store.read_all(S.SHEET_FEE):
        if f.get("期间") != period:
            continue
        if f.get("分摊状态") != "已分摊":
            report.add_exc(EXC_COST_MISSING,
                           f"费用单 {f.get('费用ID')} 未分摊, 结账前须完成分摊")
            continue
        total = _num(f, "费用总额")
        alloc = Decimal("0")
        for r in _active_rows(store, S.SHEET_IN, period):
            if r.get("单号") == f.get("关联采购单号"):
                alloc += _num(r, "附加费用分摊额")
        if alloc != total:
            report.add_exc("E07", f"费用单 {f.get('费用ID')} 分摊合计 {alloc} != 总额 {total}")

    # 负库存时序检查
    _check_negative_sequence(store, period, report)

    # 对账: 期末数量核对 + E06/E07
    for sku, m in months.items():
        if m.end_qty == 0 and m.end_amount != 0:
            report.add_exc(EXC_ZERO_STOCK_AMOUNT,
                           f"SKU {sku}: 期末数量 0 但期末金额 {m.end_amount}")
        pool_end_qty = m.end_qty - m.pending_qty
        if m.avg_price is not None:
            expect = money(pool_end_qty * m.avg_price)
            if abs(expect - m.end_amount) > CENT:
                report.add_exc(EXC_TAIL_OVER,
                               f"SKU {sku}: 期末金额 {m.end_amount} 与 数量×单价 {expect} 差 "
                               f"{abs(expect - m.end_amount)} > 0.01")
    # 公司=仓库合计
    wh_total: dict[str, Decimal] = {}
    for (sku, wh), w in wh_qty.items():
        wh_total[sku] = wh_total.get(sku, Decimal("0")) + w.end_qty
    for sku, m in months.items():
        if sku not in wh_total:
            wh_total[sku] = Decimal("0")
        if wh_total[sku] != m.end_qty:
            report.add_exc("E16", f"SKU {sku}: 仓库合计 {wh_total[sku]} != 公司期末 {m.end_qty}")
    # 调拨公司级净零
    t_net = {sku: Decimal("0") for sku in months}
    for (sku, _), w in wh_qty.items():
        t_net[sku] = t_net.get(sku, Decimal("0")) + w.transfer_net
    for sku, net in t_net.items():
        if net != 0:
            report.add_exc("E16", f"SKU {sku}: 调拨公司级净数量 {net} != 0")

    report.tentative = (store.get_period(period) or {}).get("状态") != S.PERIOD_CLOSED
    return report


def _materialized(store: Store, period: str) -> dict[str, dict] | None:
    """已结账期间的物化结存(公司行, 最新已结账版本)。"""
    rows = [r for r in store.read_all(S.SHEET_MONTHLY)
            if r.get("期间") == period and r.get("行类型") == S.ROW_COMPANY
            and r.get("状态") == S.MONTHLY_FINAL]
    if not rows:
        return None
    out = {}
    for r in rows:
        out[r.get("SKU")] = r
    return out


def _check_negative_sequence(store: Store, period: str, report: CloseReport) -> None:
    """按 (业务日期, 录入时间, ID) 时序逐仓核算, 任一时点不得为负。"""
    events: dict[tuple[str, str], list[tuple]] = {}
    for r in _active_rows(store, S.SHEET_IN, period):
        events.setdefault((r.get("SKU"), r.get("仓库")), []).append(
            (r.get("业务日期") or "", r.get("录入时间") or "", r.get("入库ID"), _num(r, "数量")))
    for r in _active_rows(store, S.SHEET_OUT, period):
        events.setdefault((r.get("SKU"), r.get("仓库")), []).append(
            (r.get("业务日期") or "", r.get("录入时间") or "", r.get("出库ID"), -_num(r, "数量")))
    for r in _active_rows(store, S.SHEET_ADJUST, period):
        events.setdefault((r.get("SKU"), r.get("仓库")), []).append(
            (r.get("业务日期") or "", r.get("录入时间") or "", r.get("调整ID"), _num(r, "数量")))
    for r in _active_rows(store, S.SHEET_TRANSFER, period):
        events.setdefault((r.get("SKU"), r.get("调出仓库")), []).append(
            (r.get("业务日期") or "", r.get("录入时间") or "", r.get("调拨ID"), -_num(r, "数量")))
        events.setdefault((r.get("SKU"), r.get("调入仓库")), []).append(
            (r.get("业务日期") or "", r.get("录入时间") or "", r.get("调拨ID"), _num(r, "数量")))
    for (sku, wh), evs in events.items():
        q = report.warehouses.get((sku, wh), WhQty(sku=sku, wh=wh)).open_qty
        for e in sorted(evs):
            q += e[3]
            if q < 0:
                report.add_exc(EXC_NEGATIVE,
                               f"SKU {sku} @ {wh} 在 {e[0]} 记录 {e[2]} 后库存为负 {q}")
                break


def dry_close(store: Store, period: str) -> CloseReport:
    """试结账: 计算 + 对账 + 异常清单, 不写盘。"""
    return compute_month(store, period)


def final_close(store: Store, period: str, operator: str,
                confirmed_by: str) -> CloseReport:
    """正式结账: 全部检查通过后冻结期间并物化结存。"""
    store.ensure_period_open(period)
    if not confirmed_by:
        raise LedgerError("E08", "正式结账必须提供用户明确确认(confirmed_by)")
    report = compute_month(store, period)
    if not report.ok:
        raise LedgerError("E18",
                          f"试结账未通过({len(report.exceptions)} 项异常), 已拒绝结账: "
                          + "; ".join(f"{c} {m}" for c, m in report.exceptions[:10]))
    if period != store.meta(S.META_START_PERIOD):
        prev = store.get_period(prev_period(period))
        if prev is None or prev.get("状态") != S.PERIOD_CLOSED:
            raise LedgerError(EXC_SEQUENCE, f"期间 {prev_period(period)} 未结账, 不能结账 {period}")
    # 写回各销售行出库成本
    for sku, m in report.months.items():
        for c in m.consumption:
            if c.kind == "sale":
                store.update_cell(S.SHEET_OUT, c.row_number, "出库成本", c.cost)
    # 写月末结存
    ver = int((store.get_period(period) or {}).get("核算版本") or "0") + 1
    for sku, m in sorted(report.months.items()):
        store.append_row(S.SHEET_MONTHLY, {
            "期间": period, "SKU": sku, "仓库": "", "行类型": S.ROW_COMPANY,
            "期初数量": m.open_qty, "计价入库数量": m.in_qty + m.gain_qty,
            "出库数量": m.sales_qty, "调整数量": -m.loss_qty, "调拨净": Decimal("0"),
            "期末数量": m.end_qty, "期初金额": m.open_amount,
            "计价入库金额": m.in_amount + m.gain_amount, "附加费用分摊额": m.fee,
            "出库成本": m.out_cost, "期末金额": m.end_amount,
            "加权单价": m.avg_price if m.avg_price is not None else "",
            "状态": S.MONTHLY_FINAL, "核算版本": ver, "更新时间": now_str(),
        })
    for (sku, wh), w in sorted(report.warehouses.items()):
        store.append_row(S.SHEET_MONTHLY, {
            "期间": period, "SKU": sku, "仓库": wh, "行类型": S.ROW_WAREHOUSE,
            "期初数量": w.open_qty, "计价入库数量": w.in_qty,
            "出库数量": w.sales_qty, "调整数量": w.adjust_qty, "调拨净": w.transfer_net,
            "期末数量": w.end_qty,
            "期初金额": "", "计价入库金额": "", "附加费用分摊额": "",
            "出库成本": "", "期末金额": "", "加权单价": "",
            "状态": S.MONTHLY_FINAL, "核算版本": ver, "更新时间": now_str(),
        })
    store.set_period(period, S.PERIOD_CLOSED, operator,
                     f"正式结账, 用户确认: {confirmed_by}")
    store.audit("close", operator,
                f"正式结账 {period} (版本 {ver})",
                f"用户确认: {confirmed_by}; 写回出库成本与月末结存",
                f"P{period}", "成功")
    store.save()
    return report


def reopen(store: Store, period: str, authorization: str, reason: str,
           operator: str) -> None:
    """受控重开: 要求授权标识与原因; 级联重开后续已结账期间并保留历史版本。"""
    d = store.get_period(period)
    if d is None:
        raise LedgerError("E08", f"期间 {period} 不存在")
    if d.get("状态") != S.PERIOD_CLOSED:
        raise LedgerError("E08", f"期间 {period} 未结账, 无需重开")
    if not authorization or not reason:
        raise LedgerError("E08", "受控重开必须提供授权标识与原因")
    # 标记本期及后续所有已结账期间的结存为历史
    affected = [period]
    p = next_period(period)
    while True:
        dp = store.get_period(p)
        if dp is None or dp.get("状态") != S.PERIOD_CLOSED:
            break
        affected.append(p)
        p = next_period(p)
    for r in store.read_all(S.SHEET_MONTHLY):
        if r.get("期间") in affected and r.get("状态") == S.MONTHLY_FINAL:
            store.update_cell(S.SHEET_MONTHLY, r["_row"], "状态", S.MONTHLY_HISTORY)
    for ap in affected:
        store.set_period(ap, S.PERIOD_OPEN, operator,
                         f"受控重开(级联), 授权: {authorization}, 原因: {reason}")
    store.audit("reopen", operator,
                f"受控重开 {period}" + (f", 级联 {affected[1:]}" if len(affected) > 1 else ""),
                f"授权: {authorization}; 原因: {reason}; 原结存保留为历史版本",
                f"P{'/'.join(affected)}", "成功")
    store.save()