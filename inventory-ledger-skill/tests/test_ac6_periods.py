"""AC6: 历史期间保护、受控重开与级联重算。"""
from decimal import Decimal

import pytest
from ledgerlib.store import LedgerError

from conftest import (post_opening, post_purchases, post_sales, month,
                      close_final, reopen, open_period, open_write, closed)


def build_two_months(masters):
    """2026-01 与 2026-02 均已结账的常规台账。"""
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 100, "单价": 10}])
    post_purchases(masters, [{"业务日期": "2026-01-10", "单号": "PO-601",
                              "明细行号": "1", "SKU": "SKU-001", "仓库": "A",
                              "数量": 100, "确认单价": 12}])
    post_sales(masters, [{"业务日期": "2026-01-15", "单号": "SO-601",
                          "明细行号": "1", "SKU": "SKU-001", "仓库": "A",
                          "数量": 40, "渠道": "本店", "订单号": "O-601",
                          "销售单价": 20}])
    close_final(masters, "2026-01")
    open_period(masters, "2026-02")
    post_purchases(masters, [{"业务日期": "2026-02-10", "单号": "PO-602",
                              "明细行号": "1", "SKU": "SKU-001", "仓库": "A",
                              "数量": 50, "确认单价": 15}])
    post_sales(masters, [{"业务日期": "2026-02-15", "单号": "SO-602",
                          "明细行号": "1", "SKU": "SKU-001", "仓库": "A",
                          "数量": 30, "渠道": "本店", "订单号": "O-602",
                          "销售单价": 25}])
    close_final(masters, "2026-02")
    return month(masters, "2026-02").months["SKU-001"]


def test_closed_period_rejects_direct_writes(masters):
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 10, "单价": 10}])
    close_final(masters, "2026-01")
    # 销售到已结账期间 -> E15
    with pytest.raises(LedgerError) as ei:
        post_sales(masters, [{"业务日期": "2026-01-20", "单号": "SO-700",
                              "明细行号": "1", "SKU": "SKU-001", "仓库": "A",
                              "数量": 1, "渠道": "本店", "订单号": "O-700",
                              "销售单价": 10}])
    assert ei.value.code in ("E15", "E09")
    # 结账保护: 期初重设也被拒绝
    with pytest.raises(LedgerError):
        post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 99, "单价": 10}])


def test_close_requires_previous_closed(masters):
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 10, "单价": 10}])
    open_period(masters, "2026-02")
    m = month(masters, "2026-02")
    codes = {c for c, _ in m.exceptions}
    assert "E13" in codes  # 2026-01 未结账 -> 2026-02 不能正式结账


def test_closed_period_materialized_and_versioned(masters):
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 10, "单价": 10}])
    close_final(masters, "2026-01")
    with open_write(masters) as st:
        rows = [r for r in st.read_all("月末结存") if r.get("期间") == "2026-01"]
    assert rows, "应有物化结存行"
    assert all(r.get("状态") == "已结账" for r in rows)
    assert {r.get("行类型") for r in rows} == {"公司", "仓库"}
    assert closed(masters, "2026-01") == "已结账"


def test_reopen_requires_auth_and_reason(masters):
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 10, "单价": 10}])
    close_final(masters, "2026-01")
    with pytest.raises(LedgerError) as ei:
        with open_write(masters) as st:
            from ledgerlib import engine
            engine.reopen(st, "2026-01", "", "", "test")
    assert ei.value.code == "E08"


def test_reopen_cascade_and_recompute(masters):
    m2_before = build_two_months(masters)
    assert closed(masters, "2026-02") == "已结账"

    # 授权重开 2026-01 -> 2026-02 级联重开
    reopen(masters, "2026-01", auth="AUTH-2026-001", reason="发现 1 月采购单价录入错误")
    assert closed(masters, "2026-01") == "未结账"
    assert closed(masters, "2026-02") == "未结账"
    with open_write(masters) as st:
        hist = [r for r in st.read_all("月末结存")
                if r.get("期间") in ("2026-01", "2026-02")]
        assert all(r.get("状态") == "历史" for r in hist), "原结存应保留为历史版本"

    # 更正 1 月采购成本 12 -> 20
    with open_write(masters) as st:
        from ledgerlib import biz
        rows = [r for r in st.read_all("入库流水")
                if r.get("期间") == "2026-01" and r.get("单号") == "PO-601"
                and r.get("记录状态") == "正常"]
        assert rows
        biz.correct_purchase(st, rows[0]["入库ID"], "test", "user",
                             basis="重开复核更正", new_price=20)
    # 级联期间在 2026-01 未重新结账前, 2026-02 不能结账
    with pytest.raises(LedgerError) as ei:
        close_final(masters, "2026-02")
    assert ei.value.code == "E18" or "E13" in str(ei.value.message)

    # 依序重结账
    close_final(masters, "2026-01")
    close_final(masters, "2026-02")

    m2_after = month(masters, "2026-02").months["SKU-001"]
    m1 = month(masters, "2026-01").months["SKU-001"]
    assert m1.avg_price == Decimal("15.0000")  # (1000+2000)/200
    assert m1.out_cost == Decimal("600.00")    # 40 × 15
    assert m1.end_amount == Decimal("2400.00")
    # 2 月起点变化 -> 2 月结果必须重算(而非沿用旧值)
    assert m2_after.end_amount != m2_before.end_amount
    assert closed(masters, "2026-02") == "已结账"

def test_audit_trail_covers_init_close_reopen(masters):
    """操作记录必须持久化 init/final_close/reopen(audit 先于 save)。"""
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 10, "单价": 10}])
    close_final(masters, "2026-01")
    reopen(masters, "2026-01", auth="AUTH-AUDIT", reason="审计轨迹验证")
    with open_write(masters) as st:
        ops = [r.get("操作类型") for r in st.read_all("操作记录")]
        rows = [r for r in st.read_all("操作记录")]
    assert len(rows) >= 3, f"审计行不足: {len(rows)}"
    assert "init" in ops
    assert "close" in ops
    assert "reopen" in ops
    rp = [r for r in rows if r.get("操作类型") == "reopen"][0]
    assert "AUTH-AUDIT" in rp.get("变更摘要")
