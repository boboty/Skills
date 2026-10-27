"""AC5: 采购成本更正 —— 加权成本/出库成本/期末金额重算, 原数据可追溯。"""
from decimal import Decimal

from conftest import (post_opening, post_purchases, post_sales, month,
                      close_final)


def test_ac5_cost_correction_recomputes_all(masters):
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 100, "单价": 10}])
    res = post_purchases(masters, [{"业务日期": "2026-01-10", "单号": "PO-501",
                                    "明细行号": "1", "SKU": "SKU-001",
                                    "仓库": "A", "数量": 100, "确认单价": 12}])
    post_sales(masters, [{"业务日期": "2026-01-15", "单号": "SO-501",
                          "明细行号": "1", "SKU": "SKU-001", "仓库": "A",
                          "数量": 40, "渠道": "本店", "订单号": "O-501",
                          "销售单价": 20}])
    in_id = res["ids"][0]

    # 更正前: (1000 + 1200)/200 = 11.00, 出库 440, 期末 1760
    before = month(masters, "2026-01").months["SKU-001"]
    assert before.avg_price == Decimal("11.0000")
    assert before.out_cost == Decimal("440.00")
    assert before.end_amount == Decimal("1760.00")

    # 更正: 采购单价 12 -> 14 (依据: 供应商发票更正)
    from conftest import open_write
    with open_write(masters) as st:
        from ledgerlib import biz
        out = biz.correct_purchase(st, in_id, "test", "user",
                                   basis="供应商发票 SK-2026-001 更正", new_price=14)
    assert out["old_id"] == in_id

    after = month(masters, "2026-01").months["SKU-001"]
    assert after.avg_price == Decimal("12.0000")      # (1000+1400)/200
    assert after.out_cost == Decimal("480.00")         # 40 × 12
    assert after.end_amount == Decimal("1920.00")      # 2400 - 480

    # 追溯: 原行保留为"已更正", 更正行指向原行
    with open_write(masters) as st:
        rows = {r.get("入库ID"): r for r in st.read_all("入库流水")}
    assert rows[in_id]["记录状态"] == "已更正"
    new_rows = [r for r in rows.values()
                if r.get("记录状态") == "正常" and r.get("更正关联ID") == in_id]
    assert len(new_rows) == 1
    assert new_rows[0]["确认单价"] == Decimal("14.0000")
    assert new_rows[0]["单号"] == "PO-501"   # 沿用原单号与明细
    assert new_rows[0]["备注"].startswith("供应商发票")


def test_ac5_correction_requires_basis_and_open_period(masters):
    import pytest
    from ledgerlib.store import LedgerError
    from conftest import open_write
    from ledgerlib import biz
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 10, "单价": 10}])
    res = post_purchases(masters, [{"业务日期": "2026-01-10", "单号": "PO-502",
                                    "明细行号": "1", "SKU": "SKU-001",
                                    "仓库": "A", "数量": 10, "确认单价": 12}])
    in_id = res["ids"][0]
    # 缺依据 -> 拒绝
    with pytest.raises(LedgerError) as ei:
        with open_write(masters) as st:
            biz.correct_purchase(st, in_id, "test", "user", basis="", new_price=14)
    assert ei.value.code == "E08"
    # 结账后更正 -> E15
    close_final(masters, "2026-01")
    with pytest.raises(LedgerError) as ei:
        with open_write(masters) as st:
            biz.correct_purchase(st, in_id, "test", "user",
                                 basis="x", new_price=14)
    assert ei.value.code == "E15"