"""Decimal 精确计算与最大余数法尾差分配的单元测试。"""
from decimal import Decimal

import pytest

from ledgerlib.decimal_utils import (money, price, qty,
                                     largest_remainder_allocate)


def test_quantize_half_up():
    assert money("1.005") == Decimal("1.01")
    assert money("1.004") == Decimal("1.00")
    assert price("1.00005") == Decimal("1.0001")
    assert qty("0.0000005") == Decimal("0.000001")


def test_allocate_conservation_three_rows():
    r = largest_remainder_allocate([Decimal(1), Decimal(1), Decimal(1)],
                                   Decimal("1.00"))
    assert sum(r) == Decimal("1.00")
    # 余数相同 -> 行序靠前者获得 1 分尾差
    assert r == [Decimal("0.34"), Decimal("0.33"), Decimal("0.33")]


def test_allocate_deterministic_repeat():
    weights = [Decimal("3"), Decimal("7"), Decimal("2"), Decimal("8")]
    a = largest_remainder_allocate(weights, Decimal("100.00"))
    b = largest_remainder_allocate(weights, Decimal("100.00"))
    assert a == b
    assert sum(a) == Decimal("100.00")
    for w, v in zip(weights, a):
        assert abs(v - w * Decimal("100") / Decimal("20")) <= Decimal("0.01")


def test_allocate_tail_cases():
    # 总金额很小但行数多
    r = largest_remainder_allocate([Decimal(1), Decimal(1)], Decimal("0.01"))
    assert sum(r) == Decimal("0.01")
    assert r == [Decimal("0.01"), Decimal("0.00")]
    # 单行全额
    r2 = largest_remainder_allocate([Decimal(5)], Decimal("88.88"))
    assert r2 == [Decimal("88.88")]


def test_allocate_zero_total():
    r = largest_remainder_allocate([Decimal(1), Decimal(2)], Decimal("0"))
    assert r == [Decimal("0.00"), Decimal("0.00")]


def test_allocate_zero_weights_raises():
    with pytest.raises(ValueError):
        largest_remainder_allocate([Decimal(0), Decimal(0)], Decimal("1.00"))