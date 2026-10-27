"""AC1 / AC2: 多仓统一成本 + 采购附加费用分摊(任务书 §10 验收样例)。"""
from decimal import Decimal

from conftest import (post_opening, post_purchases, post_sales,
                      post_transfers, post_fees, allocate_fee, month)

OPENING = [
    {"SKU": "SKU-001", "仓库": "A", "数量": 100, "单价": 10},
    {"SKU": "SKU-001", "仓库": "B", "数量": 50, "单价": 10},
]
PURCHASE = [{"业务日期": "2026-01-10", "单号": "PO-001", "明细行号": "1",
             "SKU": "SKU-001", "仓库": "A", "数量": 100, "确认单价": 12}]
SALES = [
    {"业务日期": "2026-01-15", "单号": "SO-001", "明细行号": "1",
     "SKU": "SKU-001", "仓库": "A", "数量": 40, "渠道": "本店",
     "订单号": "ORD-1", "销售单价": 20},
    {"业务日期": "2026-01-18", "单号": "SO-002", "明细行号": "1",
     "SKU": "SKU-001", "仓库": "B", "数量": 30, "渠道": "多渠道",
     "订单号": "ORD-2", "销售单价": 22},
]
TRANSFER = [{"业务日期": "2026-01-20", "调拨单号": "TR-001", "明细行号": "1",
             "SKU": "SKU-001", "调出仓库": "A", "调入仓库": "B", "数量": 20}]


def build_ac1(masters):
    post_opening(masters, OPENING)
    post_purchases(masters, PURCHASE)
    post_sales(masters, SALES)
    post_transfers(masters, TRANSFER)
    return month(masters, "2026-01")


def test_ac1_multi_warehouse_unified_cost(masters):
    m = build_ac1(masters)
    assert m.ok, m.exceptions
    sku = m.months["SKU-001"]
    assert sku.avg_price == Decimal("10.8000"), sku.avg_price
    assert sku.out_cost == Decimal("756.00")
    assert sku.end_amount == Decimal("1944.00")
    assert sku.end_qty == Decimal("180")
    # 756 + 1,944 = 2,700
    assert sku.out_cost + sku.end_amount == Decimal("2700.00")
    wh = {(s, w): v for (s, w), v in m.warehouses.items()}
    assert wh[("SKU-001", "A")].end_qty == Decimal("140")
    assert wh[("SKU-001", "B")].end_qty == Decimal("40")
    # 调拨不参与公司级统计: 公司期末数量 = 期初+采购-出库
    assert sku.end_qty == Decimal("150") + Decimal("100") - Decimal("70")


def test_ac2_purchase_extra_fee(masters):
    post_opening(masters, OPENING)
    post_purchases(masters, PURCHASE)
    post_sales(masters, SALES)
    post_transfers(masters, TRANSFER)
    post_fees(masters, [{"业务日期": "2026-01-12", "费用单号": "FEE-01",
                         "关联采购单号": "PO-001", "费用总额": 100,
                         "分摊规则": "按数量比例"}])
    allocate_fee(masters, "FEE-000001")
    m = month(masters, "2026-01")
    assert m.ok, m.exceptions
    sku = m.months["SKU-001"]
    assert sku.pool_amount == Decimal("2800.00")  # 1500 + 1200 + 100
    assert sku.avg_price == Decimal("11.2000")
    assert sku.out_cost == Decimal("784.00")
    assert sku.end_amount == Decimal("2016.00")
    assert sku.out_cost + sku.end_amount == Decimal("2800.00")


def test_ac2_fee_amount_rule_and_tail(masters):
    """按金额比例分摊: 两行采购 3:7, 总费用 10.00 -> 3.00 / 7.00。"""
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 10, "单价": 10}])
    post_purchases(masters, [
        {"业务日期": "2026-01-05", "单号": "PO-002", "明细行号": "1",
         "SKU": "SKU-001", "仓库": "A", "数量": 30, "确认单价": 10},
        {"业务日期": "2026-01-06", "单号": "PO-002", "明细行号": "2",
         "SKU": "SKU-001", "仓库": "A", "数量": 70, "确认单价": 10},
    ])
    post_fees(masters, [{"业务日期": "2026-01-07", "费用单号": "FEE-02",
                         "关联采购单号": "PO-002", "费用总额": 10,
                         "分摊规则": "按金额比例"}])
    allocate_fee(masters, "FEE-000001")
    m = month(masters, "2026-01")
    assert m.ok, m.exceptions
    assert m.months["SKU-001"].fee == Decimal("10.00")


def test_ac2_fee_explicit_rule(masters):
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 10, "单价": 10}])
    res = post_purchases(masters, [
        {"业务日期": "2026-01-05", "单号": "PO-003", "明细行号": "1",
         "SKU": "SKU-001", "仓库": "A", "数量": 10, "确认单价": 10},
        {"业务日期": "2026-01-06", "单号": "PO-003", "明细行号": "2",
         "SKU": "SKU-001", "仓库": "A", "数量": 20, "确认单价": 10},
    ])
    post_fees(masters, [{"业务日期": "2026-01-07", "费用单号": "FEE-03",
                         "关联采购单号": "PO-003", "费用总额": 30,
                         "分摊规则": "逐明细指定"}])
    in1, in2 = res["ids"][0], res["ids"][1]
    allocate_fee(masters, "FEE-000001", explicit=[(in1, Decimal("9.99")),
                                                  (in2, Decimal("20.01"))])
    m = month(masters, "2026-01")
    assert m.months["SKU-001"].fee == Decimal("30.00")


def test_ac2_fee_explicit_must_balance(masters):
    import pytest
    from ledgerlib.store import LedgerError
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 10, "单价": 10}])
    res = post_purchases(masters, [
        {"业务日期": "2026-01-05", "单号": "PO-004", "明细行号": "1",
         "SKU": "SKU-001", "仓库": "A", "数量": 10, "确认单价": 10},
    ])
    post_fees(masters, [{"业务日期": "2026-01-07", "费用单号": "FEE-04",
                         "关联采购单号": "PO-004", "费用总额": 30,
                         "分摊规则": "逐明细指定"}])
    with pytest.raises(LedgerError) as ei:
        allocate_fee(masters, "FEE-000001", explicit=[(res["ids"][0], Decimal("29.99"))])
    assert ei.value.code == "E07"