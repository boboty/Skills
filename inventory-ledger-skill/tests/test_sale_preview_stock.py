"""销售导入预览与提交库存校验一致性回归(验收 A–E)。

背景: 预览此前只执行 normalize_sale(行级字段校验), 不校验库存可用量;
提交阶段 add_sales -> _check_stock_available 才抛 E03, 造成"预览成功、
提交被拒"的前后不一致。修复后预览与提交共用 biz.check_stock_batch
同一规则。本文件覆盖:

A. 单行超量(现有 10, 售 999)预览即 E03;
B. 多行单独可用、合计超量(现有 10, 6+6)预览识别 E03, 且不误伤其他有货行;
C. 正常销售预览(ok、无拒绝)且提交成功, 预览不写盘;
D. 预览通过后库存变化, 提交独立重新校验并拒绝(E03), 不信任此前预览;
E. 隔离台账副本中实际超量提交: E03 + 业务行/导入登记/审计 零残留, 原台账不受影响。
"""
from __future__ import annotations

import shutil

import pytest
from ledgerlib import importer
from ledgerlib.store import LedgerError

from conftest import month, open_write, post_opening, post_sales, reason_codes

SALE_HEADER = ["业务日期", "单号", "明细行号", "SKU", "仓库", "数量",
               "渠道", "订单号", "销售单价"]


def _csv(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text("\n".join([",".join(SALE_HEADER)] +
                           [",".join(r) for r in rows]) + "\n",
                 encoding="utf-8-sig")
    return str(p)


def _row(date, doc, line, sku, wh, qty, price="10"):
    return [date, doc, line, sku, wh, qty, "本店", f"O-{doc}-{line}", price]


def _preview(ledger, path):
    with open_write(ledger) as st:
        return importer.preview(st, path, "sale")


def _out_docs(ledger):
    with open_write(ledger) as st:
        return [r.get("单号") for r in st.read_all("出库流水")]


def _import_records(ledger):
    with open_write(ledger) as st:
        return [r for r in st.read_all("导入记录")]


# ---------------------------------------------------------------- A 单行超量
def test_A_single_over_qty_preview_E03(masters, tmp_path):
    """现有 10 件售 999 -> 预览 rejected E03, ok 为空, 且预览不写盘。"""
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 10, "单价": 10}])
    f = _csv(tmp_path, "sale_over.csv",
             [_row("2026-01-10", "S-A1", "1", "SKU-001", "A", "999")])
    pr = _preview(masters, f)
    assert len(pr.rejected) == 1 and pr.rejected[0][1] == "E03", pr.rejected
    assert "库存不足" in pr.rejected[0][2]
    assert pr.ok == []
    # 预览不写盘
    m = month(masters, "2026-01")
    assert m.months["SKU-001"].sales_qty == 0
    assert _out_docs(masters) == []


# ---------------------------------------------------------------- B 批次累计
def test_B_batch_cumulative_over_preview_E03(masters, tmp_path):
    """库存 10: 两行各 6 单独可用、合计 12 超量 -> 预览识别 E03(第 2 行);
    第三行(另一仓库有货)不被误伤。"""
    post_opening(masters, [
        {"SKU": "SKU-001", "仓库": "A", "数量": 10, "单价": 10},
        {"SKU": "SKU-001", "仓库": "B", "数量": 5, "单价": 10},
    ])
    rows = [_row("2026-01-10", "S-B1", "1", "SKU-001", "A", "6"),
            _row("2026-01-10", "S-B2", "1", "SKU-001", "A", "6"),
            _row("2026-01-10", "S-B3", "1", "SKU-001", "B", "3")]
    f = _csv(tmp_path, "sale_cum.csv", rows)
    pr = _preview(masters, f)
    assert {c for _, c, _ in pr.rejected} == {"E03"}, pr.rejected
    # 只拒绝超量的第 2 行(文件物理行 3); ok 保留第 1、3 行
    assert [r[0] for r in pr.rejected] == [3]
    assert sorted(r.get("单号") for r in pr.ok) == ["S-B1", "S-B3"]


# ---------------------------------------------------------------- C 正常销售
def test_C_normal_sale_preview_and_commit_ok(masters, tmp_path):
    """正常销售(10 售 6): 预览 ok、无拒绝; 预览不写盘; 提交成功。"""
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 10, "单价": 10}])
    f = _csv(tmp_path, "sale_ok.csv",
             [_row("2026-01-10", "S-C1", "1", "SKU-001", "A", "6")])
    pr = _preview(masters, f)
    assert len(pr.ok) == 1 and pr.rejected == [] and pr.pending == []
    assert month(masters, "2026-01").months["SKU-001"].sales_qty == 0  # 预览未写盘
    with open_write(masters) as st:
        res = importer.commit(st, f, "sale", "test", "user")
    assert res["ok"] == 1 and res["rejected"] == 0
    m = month(masters, "2026-01")
    assert m.months["SKU-001"].sales_qty == 6
    assert m.months["SKU-001"].end_qty == 4


# ---------------------------------------------------------------- D 提交重校验
def test_D_commit_reevaluates_after_stock_change(masters, tmp_path):
    """预览通过(10 售 6)后另有销售吃掉库存(剩 3), 提交必须独立重新校验并拒绝(E03)。"""
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 10, "单价": 10}])
    f = _csv(tmp_path, "sale_d.csv",
             [_row("2026-01-10", "S-D1", "1", "SKU-001", "A", "6")])
    assert len(_preview(masters, f).ok) == 1          # 预览时库存充足
    post_sales(masters, [{"业务日期": "2026-01-12", "单号": "S-D0",
                          "明细行号": "1", "SKU": "SKU-001", "仓库": "A",
                          "数量": 7, "渠道": "本店", "订单号": "O-D0",
                          "销售单价": 10}])           # 库存仅剩 3
    with open_write(masters) as st:
        with pytest.raises(LedgerError) as ei:
            importer.commit(st, f, "sale", "test", "user")
    assert "E03" in reason_codes(ei), ei.value.message
    # 零残留: 仅 S-D0 一笔已入账销售, 本批次业务行/导入登记/审计均未产生
    assert _out_docs(masters) == ["S-D0"]
    assert not any(r.get("文件名", "").endswith("sale_d.csv")
                   for r in _import_records(masters))
    with open_write(masters) as st:
        assert not any("sale_d.csv" in str(r.get("指令摘要") or "")
                       for r in st.read_all("操作记录"))


# ---------------------------------------------------------------- E 零残留
def test_E_over_commit_zero_residue_isolated_copy(masters, tmp_path):
    """隔离台账副本中实际执行超量提交(999/10): E03 拒绝 + 业务行/导入记录/审计
    零残留; 原台账不受影响。"""
    post_opening(masters, [{"SKU": "SKU-001", "仓库": "A", "数量": 10, "单价": 10}])
    copy = str(tmp_path / "副本.xlsx")
    shutil.copy2(masters, copy)                        # 隔离副本
    f = _csv(tmp_path, "sale_over_e.csv",
             [_row("2026-01-10", "S-E1", "1", "SKU-001", "A", "999")])
    with open_write(masters) as st:
        file_hash = importer.preview(st, f, "sale").file_hash
    with open_write(copy) as st:
        with pytest.raises(LedgerError) as ei:
            importer.commit(st, f, "sale", "test", "user")
    assert "E03" in reason_codes(ei), ei.value.message
    # 业务行零残留 + 期初库存原样(10)
    assert _out_docs(copy) == []
    m = month(copy, "2026-01")
    assert m.months["SKU-001"].end_qty == 10
    # 导入登记零残留(该文件摘要无记录)
    assert not any(r.get("文件摘要") == file_hash and r.get("导入类型") == "sale"
                   for r in _import_records(copy))
    # 审计零残留: 无 import/sale 类型操作
    with open_write(copy) as st:
        types = [r.get("操作类型") for r in st.read_all("操作记录")]
    assert "import" not in types and "sale" not in types, types
    # 原台账未受波及
    assert _out_docs(masters) == []