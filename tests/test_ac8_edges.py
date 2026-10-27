"""AC8: 异常边界 —— 每条错误必须明确报错, 不得自动生成不合规记录。"""
import pathlib
from decimal import Decimal

import pytest
from ledgerlib.store import LedgerError
from ledgerlib import importer, engine, biz

from conftest import (post_opening, post_purchases, post_sales, post_adjustments,
                      open_write, close_final, open_period, reason_codes)


# ---------------------------------------------------------------- E01/E02 档案缺失
def test_sku_not_exists(masters):
    with pytest.raises(LedgerError) as ei:
        post_purchases(masters, [{"业务日期": "2026-01-10", "单号": "P1",
                                  "明细行号": "1", "SKU": "NO-SKU",
                                  "仓库": "A", "数量": 10, "确认单价": 1}])
    assert "E01" in reason_codes(ei), ei.value.message


def test_warehouse_not_exists(masters):
    with pytest.raises(LedgerError) as ei:
        post_sales(masters, [{"业务日期": "2026-01-10", "单号": "S1",
                              "明细行号": "1", "SKU": "SKU-001",
                              "仓库": "NO-WH", "数量": 1, "渠道": "本店",
                              "订单号": "O", "销售单价": 1}])
    assert "E02" in reason_codes(ei), ei.value.message


# ---------------------------------------------------------------- E03 负库存
def test_negative_stock_blocked(masters):
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 5, "单价": 10}])
    with pytest.raises(LedgerError) as ei:
        post_sales(masters, [{"业务日期": "2026-01-10", "单号": "S2",
                              "明细行号": "1", "SKU": "SKU-001",
                              "仓库": "A", "数量": 6, "渠道": "本店",
                              "订单号": "O", "销售单价": 10}])
    assert "E03" in reason_codes(ei), ei.value.message


# ---------------------------------------------------------------- E04 成本缺失
def test_pending_cost_blocks_close(masters):
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 10, "单价": 10}])
    # 无单价/金额 -> 待确认
    post_purchases(masters, [{"业务日期": "2026-01-10", "单号": "P2",
                              "明细行号": "1", "SKU": "SKU-001",
                              "仓库": "A", "数量": 10}])
    with open_write(masters) as st:
        m = engine.compute_month(st, "2026-01")
    assert any(c == "E04" for c, _ in m.exceptions)
    with pytest.raises(LedgerError) as ei:
        close_final(masters, "2026-01")
    assert ei.value.code == "E18"  # 试结账未通过
    # 补确认(带单价)后可结账
    with open_write(masters) as st:
        from conftest import post_purchases as _pp
        rows = st.read_all("入库流水")
        pending = [r for r in rows if r.get("成本状态") == "待确认"]
        biz.confirm_cost(st, [pending[0]["入库ID"]], "test", "user",
                         basis="补录采购单价", new_price=11)
    with open_write(masters) as st:
        assert engine.compute_month(st, "2026-01").ok
    close_final(masters, "2026-01")


def test_gain_without_basis_rejected(masters):
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 1, "单价": 10}])
    with pytest.raises(LedgerError) as ei:
        post_adjustments(masters, [{"业务日期": "2026-01-10", "调整类型": "盘盈",
                                    "单号": "ADJ-1", "明细行号": "1",
                                    "SKU": "SKU-001", "仓库": "A",
                                    "数量": 5}])
    assert "E04" in reason_codes(ei), ei.value.message


# ---------------------------------------------------------------- E05 计量单位
def test_unit_mismatch_rejected_at_import(masters, tmp_path):
    p = pathlib.Path(tmp_path) / "u.csv"
    p.write_text("业务日期,单号,明细行号,SKU,仓库,数量,确认单价,单位\n"
                 "2026-01-10,P-U,1,SKU-001,A,10,12,箱\n", encoding="utf-8")
    with open_write(masters) as st:
        pr = importer.preview(st, str(p), "purchase")
    assert len(pr.rejected) == 1 and pr.rejected[0][1] == "E05"


# ---------------------------------------------------------------- E06/E07 零库存金额/尾差超限
def test_zero_stock_with_amount_and_tail_over(masters):
    """池 5000.00+0.03=5000.03, 数量 10001, 全月加权单价 0.499953...
    -> 4 位单价 0.5000, 出库成本 = 0.5000×10001 = 5000.50 > 池,
    期末金额 -0.47 且数量 0: 触发 E06(零库存带金额)与 E07(金额尾差超限)。"""
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A",
                            "数量": 10000, "单价": 0.5}])
    post_purchases(masters, [
        {"业务日期": "2026-01-05", "单号": "P-T1", "明细行号": "1",
         "SKU": "SKU-001", "仓库": "A", "数量": 1, "确认单价": 0.03},
    ])
    post_sales(masters, [{"业务日期": "2026-01-20", "单号": "S-T1",
                          "明细行号": "1", "SKU": "SKU-001", "仓库": "A",
                          "数量": 10001, "渠道": "本店", "订单号": "O",
                          "销售单价": 2}])
    with open_write(masters) as st:
        m = engine.compute_month(st, "2026-01")
    codes = {c for c, _ in m.exceptions}
    assert "E07" in codes, m.exceptions
    assert "E06" in codes


# ---------------------------------------------------------------- E08 自然月边界
def test_period_boundary_enforced(masters):
    # 期间未开启 -> 拒绝
    with pytest.raises(LedgerError) as ei:
        post_purchases(masters, [{"业务日期": "2026-02-01", "单号": "P3",
                                  "明细行号": "1", "SKU": "SKU-001",
                                  "仓库": "A", "数量": 10, "确认单价": 1}])
    assert "E08" in reason_codes(ei), ei.value.message
    # 开启后即可入账, 业务日期所在期间与推导一致
    open_period(masters, "2026-02")
    post_purchases(masters, [{"业务日期": "2026-02-01", "单号": "P3",
                              "明细行号": "1", "SKU": "SKU-001",
                              "仓库": "A", "数量": 10, "确认单价": 1}])


# ---------------------------------------------------------------- E11 退货
def test_return_semantics_rejected(masters):
    with pytest.raises(LedgerError) as ei:
        post_purchases(masters, [{"业务日期": "2026-01-10", "单号": "PR-1",
                                  "明细行号": "1", "SKU": "SKU-001",
                                  "仓库": "A", "数量": -5, "确认单价": 10}])
    assert "E11" in reason_codes(ei), ei.value.message
    with pytest.raises(LedgerError) as ei:
        post_purchases(masters, [{"业务日期": "2026-01-10", "单号": "PR-2",
                                  "明细行号": "1", "SKU": "SKU-001",
                                  "仓库": "A", "数量": 5, "确认单价": 10,
                                  "备注": "采购退货"}])
    assert "E11" in reason_codes(ei), ei.value.message


# ---------------------------------------------------------------- E14 零分母
def test_zero_denominator(masters):
    # 待确认(无成本依据)数量仓库有货可售, 但计价池数量为 0 -> E14
    post_purchases(masters, [{"业务日期": "2026-01-10", "单号": "P4",
                              "明细行号": "1", "SKU": "SKU-001",
                              "仓库": "A", "数量": 10}])
    post_sales(masters, [{"业务日期": "2026-01-15", "单号": "S4",
                          "明细行号": "1", "SKU": "SKU-001", "仓库": "A",
                          "数量": 5, "渠道": "本店", "订单号": "O",
                          "销售单价": 10}])
    with open_write(masters) as st:
        m = engine.compute_month(st, "2026-01")
    assert any(c == "E14" for c, _ in m.exceptions)


# ---------------------------------------------------------------- 盘亏/报废按当月统一加权
def test_loss_uses_monthly_weighted_cost(masters):
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 100, "单价": 10}])
    post_purchases(masters, [{"业务日期": "2026-01-10", "单号": "P5",
                              "明细行号": "1", "SKU": "SKU-001",
                              "仓库": "A", "数量": 100, "确认单价": 20}])
    post_adjustments(masters, [{"业务日期": "2026-01-15", "调整类型": "报废",
                                "单号": "ADJ-2", "明细行号": "1",
                                "SKU": "SKU-001", "仓库": "A", "数量": -20}])
    m = engine.month if False else None
    from conftest import month as _m
    res = _m(masters, "2026-01")
    assert res.ok, res.exceptions
    sku = res.months["SKU-001"]
    assert sku.avg_price == Decimal("15")  # (1000+2000)/200
    assert sku.loss_qty == Decimal("20")
    assert sku.out_cost == Decimal("300.00")  # 报废按统一月末成本 20×15
    assert sku.end_qty == Decimal("180")


# ---------------------------------------------------------------- 盘盈进入计价池
def test_gain_enters_cost_pool(masters):
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 100, "单价": 10}])
    post_adjustments(masters, [{"业务日期": "2026-01-10", "调整类型": "盘盈",
                                "单号": "ADJ-3", "明细行号": "1",
                                "SKU": "SKU-001", "仓库": "A", "数量": 20,
                                "计价依据类型": "实物盘点", "计价单价": 12}])
    m = None
    from conftest import month as _m
    res = _m(masters, "2026-01")
    assert res.ok, res.exceptions
    sku = res.months["SKU-001"]
    # 池 = 1000 + 240 = 1240; 数量 = 120 -> 10.3333
    assert sku.avg_price == Decimal("10.3333")
    assert sku.end_amount == Decimal("1240.00")
    assert sku.end_qty == Decimal("120")