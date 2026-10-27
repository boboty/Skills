"""AC4: 跨仓调拨 —— 双边一致、防重复、公司级净零。"""
from decimal import Decimal

import pytest
from ledgerlib.store import LedgerError

from conftest import (post_opening, post_transfers, month,
                      open_write, closed, reason_codes)


def setup_stock(masters):
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 100, "单价": 10}])


def test_transfer_reduces_out_side_and_increases_in_side(masters):
    setup_stock(masters)
    post_transfers(masters, [{"业务日期": "2026-01-10", "调拨单号": "TR-001",
                              "明细行号": "1", "SKU": "SKU-001",
                              "调出仓库": "A", "调入仓库": "B", "数量": 20}])
    m = month(masters, "2026-01")
    assert m.ok, m.exceptions
    wh = {(s, w): v for (s, w), v in m.warehouses.items()}
    assert wh[("SKU-001", "A")].end_qty == Decimal("80")   # 只减不增 -> 错
    assert wh[("SKU-001", "B")].end_qty == Decimal("20")   # 只增不减 -> 错
    # 公司级数量不变: 100
    sku = m.months["SKU-001"]
    assert sku.end_qty == Decimal("100")
    assert wh[("SKU-001", "A")].transfer_net == Decimal("-20")
    assert wh[("SKU-001", "B")].transfer_net == Decimal("20")
    # 调拨不产生金额变化(无采购成本动作时期末金额 = 期初)
    assert sku.end_amount == Decimal("1000.00")


def test_transfer_duplicate_document_blocked(masters):
    setup_stock(masters)
    post_transfers(masters, [{"业务日期": "2026-01-10", "调拨单号": "TR-001",
                              "明细行号": "1", "SKU": "SKU-001",
                              "调出仓库": "A", "调入仓库": "B", "数量": 20}])
    with pytest.raises(LedgerError) as ei:
        post_transfers(masters, [{"业务日期": "2026-01-12", "调拨单号": "TR-001",
                                  "明细行号": "1", "SKU": "SKU-001",
                                  "调出仓库": "A", "调入仓库": "B", "数量": 20}])
    assert "E12" in reason_codes(ei), ei.value.message
    # 库存只调拨了一次
    m = month(masters, "2026-01")
    wh = {(s, w): v for (s, w), v in m.warehouses.items()}
    assert wh[("SKU-001", "A")].end_qty == Decimal("80")


def test_transfer_same_wh_rejected(masters):
    setup_stock(masters)
    with pytest.raises(LedgerError) as ei:
        post_transfers(masters, [{"业务日期": "2026-01-10", "调拨单号": "TR-002",
                                  "明细行号": "1", "SKU": "SKU-001",
                                  "调出仓库": "A", "调入仓库": "A", "数量": 20}])
    assert "E08" in reason_codes(ei), ei.value.message


def test_transfer_insufficient_stock_blocked(masters):
    setup_stock(masters)
    with pytest.raises(LedgerError) as ei:
        post_transfers(masters, [{"业务日期": "2026-01-10", "调拨单号": "TR-003",
                                  "明细行号": "1", "SKU": "SKU-001",
                                  "调出仓库": "A", "调入仓库": "B", "数量": 999}])
    assert "E03" in reason_codes(ei), ei.value.message


def test_transfer_all_or_nothing(masters):
    """两行调拨中一行非法 -> 整批不落库(原子)。"""
    setup_stock(masters)
    with pytest.raises(LedgerError) as ei:
        post_transfers(masters, [
            {"业务日期": "2026-01-10", "调拨单号": "TR-004", "明细行号": "1",
             "SKU": "SKU-001", "调出仓库": "A", "调入仓库": "B", "数量": 10},
            {"业务日期": "2026-01-11", "调拨单号": "TR-004", "明细行号": "2",
             "SKU": "SKU-001", "调出仓库": "A", "调入仓库": "B", "数量": 0},  # 非法
        ])
    assert ei.value.code == "E09"
    m = month(masters, "2026-01")
    wh = {(s, w): v for (s, w), v in m.warehouses.items()}
    assert wh[("SKU-001", "A")].end_qty == Decimal("100")  # 未发生任何调拨