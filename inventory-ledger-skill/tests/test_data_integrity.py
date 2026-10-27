"""V1 数据完整性收口: 三项修复的回归测试。

Fix1: 批次内部重复业务行防重(采购/销售, 预览与提交一致, 默认整批拒绝);
Fix2: 费用登记前验证关联采购单(须同月有效入库行, 防跨期悬挂);
Fix3: 统一导入批次号(业务行/导入登记/审计同一 ID, 唯一确定, 原子提交)。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import (close_final, month, open_period, open_write,
                      post_fees, post_opening, post_purchases, reason_codes)
from ledgerlib import biz, importer
from ledgerlib.store import LedgerError, Store


def _csv(tmp_path, name, header, rows):
    p = tmp_path / name
    p.write_text("\n".join([",".join(header)] + [",".join(r) for r in rows]) + "\n",
                 encoding="utf-8-sig")
    return str(p)


P_HEADER = ["业务日期", "单号", "明细行号", "SKU", "仓库", "数量", "确认单价"]
S_HEADER = ["业务日期", "单号", "明细行号", "SKU", "仓库", "数量", "销售单价"]
F_HEADER = ["业务日期", "费用单号", "关联采购单号", "费用总额", "分摊规则"]
O_HEADER = ["SKU", "仓库", "数量", "单价"]


def _import(ledger, path, imp_type, **kw):
    with Store.open(ledger, for_write=True) as st:
        return importer.commit(st, path, imp_type, "test", "user", **kw)


def _rows(ledger, sheet):
    with Store.open(ledger, for_write=False) as st:
        return st.read_all(sheet)


# ================================================================ Fix1 批次内防重
def test_same_csv_dup_purchase_blocked(masters, tmp_path):
    """同一 CSV 两条相同采购行 -> 预览即识别重复, 提交整批拒绝, 不得重复入账。"""
    path = _csv(tmp_path, "p_dup.csv", P_HEADER,
                [["2026-01-08", "PO-D1", "1", "SKU-001", "A", "10", "10"],
                 ["2026-01-08", "PO-D1", "1", "SKU-001", "A", "10", "10"]])
    with Store.open(masters, for_write=True) as st:
        pr = importer.preview(st, path, "purchase")
    assert len(pr.ok) == 1 and len(pr.rejected) == 1
    assert pr.rejected[0][1] == "E12"
    with pytest.raises(LedgerError) as ei:
        _import(masters, path, "purchase")
    assert "E12" in reason_codes(ei), ei.value.message
    assert not any(r.get("单号") == "PO-D1" for r in _rows(masters, "入库流水"))


def test_same_csv_dup_sale_blocked(masters, tmp_path):
    """同一 CSV 两条相同销售行 -> 不得重复出库。"""
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 100, "单价": 10}])
    path = _csv(tmp_path, "s_dup.csv", S_HEADER,
                [["2026-01-10", "S-D1", "1", "SKU-001", "A", "5", "20"],
                 ["2026-01-10", "S-D1", "1", "SKU-001", "A", "5", "20"]])
    with pytest.raises(LedgerError) as ei:
        _import(masters, path, "sale")
    assert "E12" in reason_codes(ei), ei.value.message
    assert not any(r.get("单号") == "S-D1" for r in _rows(masters, "出库流水"))


def test_batch_mixed_dup_and_ok_all_blocked(masters, tmp_path):
    """一个批次包含重复记录与正常记录 -> 默认整个批次不落盘。"""
    path = _csv(tmp_path, "p_mix.csv", P_HEADER,
                [["2026-01-08", "PO-OK1", "1", "SKU-001", "A", "10", "10"],
                 ["2026-01-09", "PO-D2", "1", "SKU-001", "A", "3", "5"],
                 ["2026-01-09", "PO-D2", "1", "SKU-001", "A", "3", "5"]])
    with pytest.raises(LedgerError) as ei:
        _import(masters, path, "purchase")
    assert "E12" in reason_codes(ei), ei.value.message
    ins = _rows(masters, "入库流水")
    assert not any(r.get("单号") == "PO-OK1" for r in ins)  # 正常行也未落盘
    assert not any(r.get("单号") == "PO-D2" for r in ins)


def test_normal_multi_batch_still_works(masters, tmp_path):
    """正常多行采购(跨仓)与多行销售仍可成功, 互查不误伤。"""
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 100, "单价": 10}])
    path = _csv(tmp_path, "p_ok.csv", P_HEADER,
                [["2026-01-08", "PO-N1", "1", "SKU-001", "A", "10", "10"],
                 ["2026-01-08", "PO-N1", "2", "SKU-001", "B", "20", "11"]])
    res = _import(masters, path, "purchase")
    assert res["ok"] == 2
    path2 = _csv(tmp_path, "s_ok.csv", S_HEADER,
                 [["2026-01-15", "S-N1", "1", "SKU-001", "A", "5", "20"],
                  ["2026-01-15", "S-N1", "2", "SKU-001", "B", "3", "21"]])
    res2 = _import(masters, path2, "sale")
    assert res2["ok"] == 2


# ================================================================ Fix2 跨期费用悬挂
def test_fee_registration_rejects_closed_period_purchase(masters):
    """1 月采购已结账, 2 月登记关联该采购的费用 -> 登记阶段拒绝, 无悬挂费用。"""
    post_purchases(masters, [
        {"业务日期": "2026-01-10", "单号": "PO-FX", "明细行号": "1",
         "SKU": "SKU-001", "仓库": "A", "数量": 10, "确认单价": 10},
    ])
    close_final(masters, "2026-01")
    open_period(masters, "2026-02")
    with pytest.raises(LedgerError) as ei:
        post_fees(masters, [
            {"业务日期": "2026-02-05", "费用单号": "F-FX-1",
             "关联采购单号": "PO-FX", "费用总额": 10, "分摊规则": "按数量比例"},
        ])
    assert "E16" in reason_codes(ei), ei.value.message
    assert not any(r.get("费用单号") == "F-FX-1" for r in _rows(masters, "附加费用"))


def test_fee_registration_rejects_missing_purchase(masters):
    """费用关联不存在的采购单 -> 拒绝且不落盘。"""
    with pytest.raises(LedgerError) as ei:
        post_fees(masters, [
            {"业务日期": "2026-01-05", "费用单号": "F-MISS",
             "关联采购单号": "PO-NOT-EXIST", "费用总额": 10,
             "分摊规则": "按数量比例"},
        ])
    assert "E16" in reason_codes(ei), ei.value.message
    assert not any(r.get("费用单号") == "F-MISS" for r in _rows(masters, "附加费用"))


def test_fee_same_month_registers_allocates_closes(masters):
    """同月合法采购与费用 -> 正常登记、分摊、结账。"""
    post_purchases(masters, [
        {"业务日期": "2026-01-03", "单号": "PO-FX2", "明细行号": "1",
         "SKU": "SKU-001", "仓库": "A", "数量": 10, "确认单价": 10},
    ])
    post_fees(masters, [
        {"业务日期": "2026-01-06", "费用单号": "F-FX2-1",
         "关联采购单号": "PO-FX2", "费用总额": 50, "分摊规则": "按数量比例"},
    ])
    from conftest import allocate_fee
    allocate_fee(masters, "FEE-000001")
    close_final(masters, "2026-01")   # 结账通过, 无悬挂
    rep = month(masters, "2026-01")
    assert not any(c in ("E04", "E07") for c, _ in rep.exceptions), rep.exceptions


def test_fee_multi_same_month_accumulate_and_closes(masters):
    """同月多张费用单 -> 累计分摊正确, 成本池守恒, 可正常结账。"""
    post_purchases(masters, [
        {"业务日期": "2026-01-03", "单号": "PO-FX3", "明细行号": "1",
         "SKU": "SKU-001", "仓库": "A", "数量": 10, "确认单价": 10},
    ])
    post_fees(masters, [
        {"业务日期": "2026-01-05", "费用单号": "F-FX3-1",
         "关联采购单号": "PO-FX3", "费用总额": 30, "分摊规则": "按数量比例"},
        {"业务日期": "2026-01-06", "费用单号": "F-FX3-2",
         "关联采购单号": "PO-FX3", "费用总额": 20, "分摊规则": "按数量比例"},
    ])
    from conftest import allocate_fee
    allocate_fee(masters, "FEE-000001")
    allocate_fee(masters, "FEE-000002")
    row = _rows(masters, "入库流水")[0]
    assert row.get("附加费用分摊额") == Decimal("50.00")   # 同月同单累计
    from conftest import month as _month
    rep = _month(masters, "2026-01")
    m = rep.months["SKU-001"]
    assert m.fee == Decimal("50.00")
    assert m.end_amount + m.out_cost == m.pool_amount     # 成本池守恒
    close_final(masters, "2026-01")
    assert not any(c in ("E04", "E07") for c, _ in rep.exceptions), rep.exceptions


# ================================================================ Fix3 统一批次号
def test_import_batch_id_matches_rows_and_registry(masters, tmp_path):
    """采购/销售/期初三类导入: 业务行批次号 == 导入登记批次号, 同批次全一致。
    (费用文件与采购为不同文件, 批次一致性由 register 统一生成保证, 此处验证三类代表。)
    期初须在起始期间无业务流水时导入, 故放最前。"""
    o = _csv(tmp_path, "o3.csv", O_HEADER,
             [["SKU-001", "A", "100", "10"]])
    res_o = _import(masters, o, "opening")
    p = _csv(tmp_path, "p3.csv", P_HEADER,
             [["2026-01-08", "PO-B1", "1", "SKU-001", "A", "10", "10"],
              ["2026-01-08", "PO-B1", "2", "SKU-001", "B", "10", "11"]])
    res_p = _import(masters, p, "purchase")
    s = _csv(tmp_path, "s3.csv", S_HEADER,
             [["2026-01-10", "S-B1", "1", "SKU-001", "A", "5", "20"]])
    res_s = _import(masters, s, "sale")

    with Store.open(masters, for_write=False) as st:
        reg = {r.get("导入类型"): r.get("批次号")
               for r in st.read_all("导入记录")}
        in_rows = st.read_all("入库流水")
        out_rows = st.read_all("出库流水")
        open_rows = st.read_all("期初库存")
    assert reg["purchase"] == res_p["batch"]
    assert reg["sale"] == res_s["batch"]
    assert reg["opening"] == res_o["batch"]
    assert {r.get("导入批次") for r in in_rows if r.get("单号") == "PO-B1"} == {res_p["batch"]}
    assert {r.get("导入批次") for r in out_rows if r.get("单号") == "S-B1"} == {res_s["batch"]}
    assert {r.get("导入批次") for r in open_rows if r.get("状态") == "正常"} == {res_o["batch"]}


def test_consecutive_imports_distinct_batch_ids(masters, tmp_path):
    """连续导入两个不同文件 -> 批次号不冲突(时间戳+序号)。"""
    p1 = _csv(tmp_path, "p4a.csv", P_HEADER,
              [["2026-01-08", "PO-B2", "1", "SKU-001", "A", "10", "10"]])
    p2 = _csv(tmp_path, "p4b.csv", P_HEADER,
              [["2026-01-09", "PO-B3", "1", "SKU-001", "B", "10", "10"]])
    r1 = _import(masters, p1, "purchase")
    r2 = _import(masters, p2, "purchase")
    assert r1["batch"] != r2["batch"]
    with Store.open(masters, for_write=False) as st:
        rows = st.read_all("导入记录")
    assert len({r.get("批次号") for r in rows}) == 2


def test_failed_import_leaves_no_half_batch(masters, tmp_path, monkeypatch):
    """模拟提交失败: 不得出现只有业务行或只有导入登记的半成品。"""
    path = _csv(tmp_path, "p5.csv", P_HEADER,
                [["2026-01-08", "PO-B4", "1", "SKU-001", "A", "10", "10"]])
    from ledgerlib import store as store_mod

    def boom(*a, **k):
        raise RuntimeError("模拟登记环节磁盘故障")

    monkeypatch.setattr(store_mod.Store, "register_import", boom)
    with Store.open(masters, for_write=True) as st:
        with pytest.raises(RuntimeError):
            importer.commit(st, path, "purchase", "test", "user")
    monkeypatch.undo()
    with Store.open(masters, for_write=False) as st:
        assert not any(r.get("单号") == "PO-B4" for r in st.read_all("入库流水"))
        assert st.read_all("导入记录") == []          # 无导入登记残留
        assert not any("导入" in r.get("操作类型", "")
                       for r in st.read_all("操作记录"))  # 无审计残留