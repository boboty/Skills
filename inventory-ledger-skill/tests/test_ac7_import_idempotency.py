"""AC7: 导入幂等 —— 同文件不得重复入账, 单据行冲突必须阻止。"""
import pathlib

import pytest
from ledgerlib.store import LedgerError, Store
from ledgerlib import importer

from conftest import open_write


PURCHASE_CSV = """业务日期,单号,明细行号,SKU,仓库,数量,确认单价
2026-01-10,PO-701,1,SKU-001,A,100,12
"""


def _write_csv(tmp_path, content, name="imp.csv"):
    p = pathlib.Path(tmp_path) / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def _commit(ledger, path):
    with open_write(ledger) as st:
        return importer.commit(st, path, "purchase", "test", "user")


def test_same_file_reimport_blocked(masters, tmp_path):
    f = _write_csv(tmp_path, PURCHASE_CSV)
    _commit(masters, f)
    with pytest.raises(LedgerError) as ei:
        _commit(masters, f)
    assert ei.value.code == "E12"
    assert "已导入" in ei.value.message
    # 数量没有翻倍
    with open_write(masters) as st:
        from ledgerlib import engine
        m = engine.compute_month(st, "2026-01")
    assert m.months["SKU-001"].in_qty == 100


def test_doc_line_conflict_blocked(masters, tmp_path):
    """同一单据+明细行冲突(不同内容)必须阻止。"""
    f1 = _write_csv(tmp_path, PURCHASE_CSV, "a.csv")
    _commit(masters, f1)
    # 不同文件(不同摘要)但同一单号+明细行, 内容不同
    f2 = _write_csv(tmp_path, PURCHASE_CSV.replace(",12", ",99"), "b.csv")
    with open_write(masters) as st:
        pr = importer.preview(st, f2, "purchase")
    assert len(pr.rejected) == 1
    assert pr.rejected[0][1] == "E12"
    with pytest.raises(LedgerError) as ei:
        _commit(masters, f2)
    assert ei.value.code == "E09" or "E12" in str(ei.value.message)
    # 库存仍只有 100
    with open_write(masters) as st:
        from ledgerlib import engine
        m = engine.compute_month(st, "2026-01")
    assert m.months["SKU-001"].in_qty == 100


def test_preview_then_commit_same_outcome_single_write(masters, tmp_path):
    f = _write_csv(tmp_path, PURCHASE_CSV)
    with open_write(masters) as st:
        pr = importer.preview(st, f, "purchase")
    assert pr.imported_before is False
    assert len(pr.ok) == 1 and len(pr.rejected) == 0
    # 预览不写盘
    with open_write(masters) as st:
        from ledgerlib import engine
        m = engine.compute_month(st, "2026-01")
        assert m.months.get("SKU-001") is None  # 预览不写盘
    _commit(masters, f)
    with open_write(masters) as st:
        from ledgerlib import engine
        assert engine.compute_month(st, "2026-01").months["SKU-001"].in_qty == 100


def test_partial_rows_reported_not_silently_dropped(masters, tmp_path):
    csv = """业务日期,单号,明细行号,SKU,仓库,数量,确认单价
2026-01-10,PO-702,1,SKU-001,A,10,12
2026-01-11,PO-703,1,SKU-NOPE,A,10,12
2026-01-12,PO-704,1,SKU-001,A,10
"""
    f = _write_csv(tmp_path, csv)
    with open_write(masters) as st:
        pr = importer.preview(st, f, "purchase")
    assert len(pr.ok) == 1        # PO-702
    assert len(pr.pending) == 1   # PO-704 缺成本
    assert len(pr.rejected) == 1  # SKU-NOPE 不存在
    assert pr.rejected[0][1] == "E01"
    # 整体事务: 有拒绝行时 commit 默认拒绝整个批次
    with pytest.raises(LedgerError) as ei:
        _commit(masters, f)
    assert ei.value.code == "E09"
    # 人工裁决后允许部分写入(成功/拒绝/待确认数量均报告)
    with open_write(masters) as st:
        res = importer.commit(st, f, "purchase", "test", "user", allow_partial=True)
        # ok=实际写入行数(含待确认), pending=其中待确认, rejected=拒绝行
        assert res["ok"] == 2 and res["pending"] == 1 and res["rejected"] == 1
        from ledgerlib import engine
        m = engine.compute_month(st, "2026-01")
        assert m.months["SKU-001"].in_qty == 10
        assert m.months["SKU-001"].pending_qty == 10