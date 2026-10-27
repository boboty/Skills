"""pytest 公共配置: 将 scripts/ 加入导入路径并准备测试辅助。"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pytest  # noqa: E402
from ledgerlib.store import Store  # noqa: E402
from ledgerlib import biz, engine  # noqa: E402


@pytest.fixture
def ledger(tmp_path):
    """全新台账(测试公司, 起始期间 2026-01)。"""
    path = str(tmp_path / "ledger.xlsx")
    Store.create(path, "测试公司", "2026-01", "test")
    return path


@pytest.fixture
def masters(ledger):
    """含 SKU-001 + A/B 两仓库的台账。"""
    with Store.open(ledger, for_write=True) as st:
        biz.add_sku(st, "SKU-001", "测试商品", "件", "test")
        biz.add_warehouse(st, "A", "A仓", "test")
        biz.add_warehouse(st, "B", "B仓", "test")
    return ledger


def open_write(ledger):
    return Store.open(ledger, for_write=True)


def month(ledger, period):
    with Store.open(ledger, for_write=False) as st:
        return engine.compute_month(st, period)


def closed(ledger, period):
    with Store.open(ledger, for_write=False) as st:
        return (st.get_period(period) or {}).get("状态")


# 便捷写入(每条即一个锁定打开-保存-关闭周期)
def post_opening(ledger, rows):
    with Store.open(ledger, for_write=True) as st:
        return biz.set_opening(st, rows, "test", "user")


def post_purchases(ledger, rows):
    with Store.open(ledger, for_write=True) as st:
        return biz.add_purchases(st, rows, "test", "user")


def post_sales(ledger, rows):
    with Store.open(ledger, for_write=True) as st:
        return biz.add_sales(st, rows, "test", "user")


def post_transfers(ledger, rows):
    with Store.open(ledger, for_write=True) as st:
        return biz.add_transfers(st, rows, "test", "user")


def post_adjustments(ledger, rows):
    with Store.open(ledger, for_write=True) as st:
        return biz.add_adjustments(st, rows, "test", "user")


def post_fees(ledger, rows):
    with Store.open(ledger, for_write=True) as st:
        return biz.add_fee(st, rows, "test", "user")


def allocate_fee(ledger, fee_id, explicit=None):
    with Store.open(ledger, for_write=True) as st:
        return biz.allocate_fee(st, fee_id, "test", "user", explicit)


def close_final(ledger, period, confirm="user"):
    with Store.open(ledger, for_write=True) as st:
        return engine.final_close(st, period, "test", confirm)


def reopen(ledger, period, auth="AUTH-1", reason="测试更正"):
    with Store.open(ledger, for_write=True) as st:
        engine.reopen(st, period, auth, reason, "test")


def open_period(ledger, period):
    with Store.open(ledger, for_write=True) as st:
        biz.open_period(st, period, "test")

def reason_codes(ei) -> set:
    """从批处理异常(E09)信息中提取逐行原因码集合, 如 {"E12"}。"""
    import re
    return set(re.findall(r"\[(E\d+)\]", str(ei.value)))
