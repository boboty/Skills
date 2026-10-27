"""AC3: 先销售后采购, 最终必须使用全月统一加权成本, 不得使用暂估值。"""
from decimal import Decimal

from conftest import post_opening, post_purchases, post_sales, month


def test_ac3_sale_before_purchase_uses_final_monthly_cost(masters):
    # 期初 50@10 = 500; 1月2日先销售 30(期初库存充足); 1月20日采购 100@12
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 50, "单价": 10}])
    post_sales(masters, [{"业务日期": "2026-01-02", "单号": "SO-101",
                          "明细行号": "1", "SKU": "SKU-001", "仓库": "A",
                          "数量": 30, "渠道": "本店", "订单号": "O-101",
                          "销售单价": 15}])
    post_purchases(masters, [{"业务日期": "2026-01-20", "单号": "PO-101",
                              "明细行号": "1", "SKU": "SKU-001", "仓库": "A",
                              "数量": 100, "确认单价": 12}])

    m = month(masters, "2026-01")
    assert m.ok, m.exceptions
    sku = m.months["SKU-001"]
    # 全月统一加权 = (500 + 1200) / (50 + 100) = 1700/150 = 11.3333...
    assert sku.pool_amount == Decimal("1700.00")
    assert sku.avg_price == Decimal("11.3333"), sku.avg_price
    assert sku.out_cost == Decimal("340.00"), sku.out_cost
    assert sku.end_amount == Decimal("1360.00")
    # 关键: 若错误使用销售时点的暂估单价 10 将得到 300, 必须为 340
    assert sku.out_cost != Decimal("300.00")
    # 行级成本同样使用最终单价
    assert len(sku.consumption) == 1
    assert sku.consumption[0].cost == Decimal("340.00")