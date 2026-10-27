"""V1 定向修复回归测试(基于 boboty/Skills 2b1be80 之后的新提交)。

覆盖:
1. 同采购单多张费用单 -> 累计分摊, 禁止覆盖造成成本少计;
2. 费用分摊不得跨期间改动已结账采购流水(费用单须与采购同月);
3. 采购成本更正后费用分摊不丢失(更正行继承);
4. 导入来源行号保留 + 业务写入与导入登记原子提交(失败不留半截状态);
5. 加权成本内部全精度 + 金额尾差守恒(大数量/循环小数/零结存不误报)。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import (allocate_fee, close_final, month, open_period,
                      open_write, post_fees, post_opening, post_purchases,
                      post_sales, reason_codes)
from ledgerlib import biz, engine, importer
from ledgerlib.store import LedgerError, Store


def _in_rows(ledger, sheet_field="单号", doc=None):
    with Store.open(ledger, for_write=False) as st:
        return [r for r in st.read_all("入库流水")
                if doc is None or r.get(sheet_field) == doc]


# ---------------------------------------------------------------- 1 同单多费
def test_same_doc_multi_fee_accumulates(masters):
    """同一采购单两张费用单(金额比例 + 数量比例): 行内分摊额必须累计,
    覆盖式写入会丢掉第一张单、少计成本。"""
    post_purchases(masters, [
        {"业务日期": "2026-01-03", "单号": "PO-MF", "明细行号": "1",
         "SKU": "SKU-001", "仓库": "A", "数量": 100, "确认单价": 10},
        {"业务日期": "2026-01-03", "单号": "PO-MF", "明细行号": "2",
         "SKU": "SKU-001", "仓库": "B", "数量": 200, "确认单价": 20},
    ])
    post_fees(masters, [
        {"业务日期": "2026-01-06", "费用单号": "F-MF-1", "关联采购单号": "PO-MF",
         "费用总额": 100, "分摊规则": "按金额比例"},
        {"业务日期": "2026-01-07", "费用单号": "F-MF-2", "关联采购单号": "PO-MF",
         "费用总额": 50, "分摊规则": "按数量比例"},
    ])
    allocate_fee(masters, "FEE-000001")
    allocate_fee(masters, "FEE-000002")
    rows = _in_rows(masters, doc="PO-MF")
    by_line = {r.get("明细行号"): r for r in rows}
    # F-MF-1 金额比例: 行1 20.00, 行2 80.00; F-MF-2 数量比例: 行1 16.67, 行2 33.33
    assert by_line["1"].get("附加费用分摊额") == Decimal("36.67")   # 累计 20.00+16.67
    assert by_line["2"].get("附加费用分摊额") == Decimal("113.33")  # 累计 80.00+33.33
    rep = month(masters, "2026-01")
    m = rep.months["SKU-001"]
    assert m.fee == Decimal("150.00")                  # 两单费用全额入池
    assert m.pool_amount == Decimal("5150.00")         # 5000 + 150, 无少计
    assert not any(c in ("E06", "E07") for c, _ in rep.exceptions), rep.exceptions


# ---------------------------------------------------------------- 2 跨期费用
def test_cross_period_fee_blocked(masters):
    """费用单必须与关联采购同月: 跨期分摊会被拒绝, 不能修改已结账采购流水。"""
    post_purchases(masters, [
        {"业务日期": "2026-01-10", "单号": "PO-CP", "明细行号": "1",
         "SKU": "SKU-001", "仓库": "A", "数量": 10, "确认单价": 10},
    ])
    close_final(masters, "2026-01")
    open_period(masters, "2026-02")
    # 场景 A: 费用单落在 02(开放), 关联采购在 01(已结账) -> 登记阶段即拒绝(E16),
    # 02 月不存在悬挂费用(否则无法分摊又无法结账)
    with pytest.raises(LedgerError) as ei:
        post_fees(masters, [
            {"业务日期": "2026-02-05", "费用单号": "F-CP-1",
             "关联采购单号": "PO-CP", "费用总额": 10, "分摊规则": "按数量比例"},
        ])
    assert "E16" in reason_codes(ei), ei.value.message
    with Store.open(masters, for_write=False) as st:
        assert not any(r.get("费用单号") == "F-CP-1" for r in st.read_all("附加费用"))
    # 场景 B: 直接向已结账期间登记费用单 -> 期间保护拒绝
    with pytest.raises(LedgerError) as ei:
        post_fees(masters, [
            {"业务日期": "2026-01-15", "费用单号": "F-CP-2",
             "关联采购单号": "PO-CP", "费用总额": 5, "分摊规则": "按数量比例"},
        ])
    assert "E15" in reason_codes(ei), ei.value.message
    # 已结账的 01 月未受干扰(无费用、核对通过)
    rep = month(masters, "2026-01")
    assert not any(c in ("E07", "E16") for c, _ in rep.exceptions), rep.exceptions


# ---------------------------------------------------------------- 3 更正继承费用
def test_corrected_purchase_keeps_allocated_fee(masters):
    """已分摊费用后更正采购成本: 更正行必须继承分摊额, 否则费用随原行作废丢失。"""
    post_purchases(masters, [
        {"业务日期": "2026-01-03", "单号": "PO-CF", "明细行号": "1",
         "SKU": "SKU-001", "仓库": "A", "数量": 10, "确认单价": 10},
    ])
    post_fees(masters, [
        {"业务日期": "2026-01-06", "费用单号": "F-CF-1", "关联采购单号": "PO-CF",
         "费用总额": 50, "分摊规则": "按数量比例"},
    ])
    allocate_fee(masters, "FEE-000001")
    old_id = _in_rows(masters, doc="PO-CF")[0].get("入库ID")
    with open_write(masters) as st:
        out = biz.correct_purchase(st, old_id, "test", "user",
                                   basis="发票更正 10->12", new_price=12)
    rows = _in_rows(masters, doc="PO-CF")
    korr = next(r for r in rows if r.get("入库ID") == out["new_id"])
    old_row = next(r for r in rows if r.get("入库ID") == old_id)
    assert korr.get("附加费用分摊额") == Decimal("50.00")   # 继承, 不丢失
    assert old_row.get("记录状态") == "已更正"              # 原行保留可追溯
    rep = month(masters, "2026-01")
    m = rep.months["SKU-001"]
    assert m.fee == Decimal("50.00")
    assert m.pool_amount == Decimal("120.00") + Decimal("50.00")  # 新金额+继承费用
    assert not any(c == "E07" for c, _ in rep.exceptions), rep.exceptions


# ---------------------------------------------------------------- 4 导入行号+原子
def _csv(tmp_path, name, header, rows):
    p = tmp_path / name
    p.write_text("\n".join([",".join(header)] + [",".join(r) for r in rows]) + "\n",
                 encoding="utf-8-sig")
    return str(p)


def test_import_preserves_source_row_and_is_atomic(masters, tmp_path, monkeypatch):
    """来源行号必须与文件行一致; 导入途中失败(登记环节)不得留下半截业务写入。"""
    path = _csv(tmp_path, "purchase_v1fix.csv",
                ["业务日期", "单号", "明细行号", "SKU", "仓库", "数量", "确认单价"],
                [["2026-01-08", "PO-IMP", "1", "SKU-001", "A", "10", "10"],
                 ["2026-01-09", "PO-IMP", "2", "SKU-001", "B", "5", "20"]])
    # 1) 来源行号保留(CSV 数据物理行 2/3)
    with Store.open(masters, for_write=True) as st:
        res = importer.commit(st, path, "purchase", "test", "user")
    assert res["ok"] == 2
    rows = _in_rows(masters, doc="PO-IMP")
    assert sorted(r.get("来源行号") for r in rows) == ["2", "3"]
    assert all(str(r.get("来源文件")).endswith("purchase_v1fix.csv") for r in rows)
    # 2) 中途失败 -> 原子回滚(业务行不入库, 导入登记不产生)
    path2 = _csv(tmp_path, "purchase_v1fix2.csv",
                 ["业务日期", "单号", "明细行号", "SKU", "仓库", "数量", "确认单价"],
                 [["2026-01-10", "PO-IMP2", "1", "SKU-001", "A", "1", "1"]])
    from ledgerlib import store as store_mod

    def boom(*a, **k):
        raise RuntimeError("模拟登记环节磁盘故障")

    monkeypatch.setattr(store_mod.Store, "register_import", boom)
    with Store.open(masters, for_write=True) as st:
        with pytest.raises(RuntimeError):
            importer.commit(st, path2, "purchase", "test", "user")
    monkeypatch.undo()
    with Store.open(masters, for_write=False) as st:
        assert not any(r.get("单号") == "PO-IMP2" for r in st.read_all("入库流水"))
        assert st.find_import(path2, "purchase") is None  # 未登记 -> 可完整重试
    # 3) 修复后重试成功; 再次提交同文件被幂等拦截
    with Store.open(masters, for_write=True) as st:
        res = importer.commit(st, path2, "purchase", "test", "user")
    assert res["ok"] == 1
    with Store.open(masters, for_write=True) as st:
        with pytest.raises(LedgerError) as ei:
            importer.commit(st, path2, "purchase", "test", "user")
    assert "E12" in reason_codes(ei), ei.value.message


# ---------------------------------------------------------------- 5 精度守恒
def test_repeating_decimal_large_qty_conserves(masters):
    """循环小数单价 + 大数量: 内部全精度均价核算, 守恒恒等, 无 E06/E07 误报。"""
    post_opening(masters, [
        {"SKU": "SKU-001", "仓库": "A", "数量": 1000,
         "单价": 10, "金额": 10000},
    ])
    # 采购 3 件 @33.33 -> 池 10099.99/1003 = 10.0698...(循环)
    post_purchases(masters, [
        {"业务日期": "2026-01-05", "单号": "P-RD", "明细行号": "1",
         "SKU": "SKU-001", "仓库": "A", "数量": 3, "确认单价": 33.33},
    ])
    rep0 = month(masters, "2026-01")
    m0 = rep0.months["SKU-001"]
    assert m0.avg_price == Decimal("10.0698")          # 4 位展示
    assert m0.pool_amount == Decimal("10099.99")
    assert m0.end_amount == Decimal("10099.99")        # 无出库: 期末=池
    # 部分售出 500: 期末 = round2(503 × 内部均价), 守恒恒等
    post_sales(masters, [{"业务日期": "2026-01-20", "单号": "S-RD",
                          "明细行号": "1", "SKU": "SKU-001", "仓库": "A",
                          "数量": 500, "渠道": "本店", "订单号": "O",
                          "销售单价": 30}])
    rep = month(masters, "2026-01")
    m = rep.months["SKU-001"]
    assert m.avg_internal is not None
    expect_end = (m.pool_amount * Decimal("503") / Decimal("1003")).quantize(Decimal("0.01"))
    assert m.end_amount == expect_end
    assert m.end_amount + m.out_cost == m.pool_amount  # 完整成本守恒
    assert not any(c in ("E06", "E07") for c, _ in rep.exceptions), rep.exceptions
    # 零结存: 全量售出 -> 期末 0.00, 出库成本 = 全池
    post_sales(masters, [{"业务日期": "2026-01-25", "单号": "S-RD2",
                          "明细行号": "1", "SKU": "SKU-001", "仓库": "A",
                          "数量": 503, "渠道": "本店", "订单号": "O2",
                          "销售单价": 30}])
    rep2 = month(masters, "2026-01")
    m2 = rep2.months["SKU-001"]
    assert m2.end_qty == Decimal("0")
    assert m2.end_amount == Decimal("0.00")
    assert m2.out_cost == m2.pool_amount
    assert not any(c in ("E06", "E07") for c, _ in rep2.exceptions), rep2.exceptions