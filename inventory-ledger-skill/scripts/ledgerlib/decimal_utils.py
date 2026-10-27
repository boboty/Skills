"""Decimal 精确计算与确定性尾差分配工具。

规则来源: references/business-rules.md 下篇 §1-§3
- 全部业务数值使用 Decimal, 禁止 float 参与计算。
- 舍入: ROUND_HALF_UP。
- 精度: 数量 6 位, 单价 4 位, 金额 2 位。
- 尾差: 最大余数法, 行序固定(记录 ID 升序) -> 确定且可复现。
"""
from __future__ import annotations

import decimal
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN

Q6 = Decimal("0.000001")   # 数量精度
Q4 = Decimal("0.0001")     # 单价精度
Q2 = Decimal("0.01")       # 金额精度
CENT = Decimal("0.01")

# 全局高精度上下文: 所有中间计算在 28 位精度下进行, 仅在最终落库处量化。
decimal.getcontext().prec = 28


def qty(x) -> Decimal:
    return _to_dec(x).quantize(Q6, rounding=ROUND_HALF_UP)


def price(x) -> Decimal:
    return _to_dec(x).quantize(Q4, rounding=ROUND_HALF_UP)


def money(x) -> Decimal:
    return _to_dec(x).quantize(Q2, rounding=ROUND_HALF_UP)


def _to_dec(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(str(x))
    if x is None or x == "":
        raise ValueError("数值字段为空")
    return Decimal(str(x))


def as_decimal(x) -> Decimal:
    """宽松转换: None/空 -> Decimal('0')"""
    if x is None or x == "":
        return Decimal("0")
    return _to_dec(x)


def largest_remainder_allocate(weights, total):
    """最大余数法金额尾差分配(确定性)。

    参数:
        weights: 各行的未舍入权重(Decimal 列表); 调用方必须按 记录 ID 升序 传入,
                 该顺序即尾差分配的比较顺序(余数相同的前行优先获得 1 分尾差)。
        total:   分配目标(2 位小数的 Decimal)。
    返回:
        每行分配后的 2 位小数金额列表, 恒有 sum(result) == total。
    """
    if len(weights) == 0:
        if total != 0:
            raise ValueError("无可分配行但目标金额非零")
        return []
    total = money(total)
    sw = sum(weights)
    if sw == 0:
        raise ValueError("分配权重之和为零")
    raw = [w * total / sw for w in weights]
    floored = [r.quantize(CENT, rounding=ROUND_DOWN) for r in raw]
    diff_cents = int((total - sum(floored)) / CENT)
    if diff_cents < 0:
        raise ValueError("尾差为负, 分配不可平衡")  # 理论不可达, 防御
    rem = [r - f for r, f in zip(raw, floored)]
    ranked = sorted(range(len(weights)), key=lambda i: (-rem[i], i))
    result = list(floored)
    for i in ranked[:diff_cents]:
        result[i] += CENT
    # 守恒断言
    if sum(result) != total:
        raise AssertionError(f"尾差分配不守恒: {sum(result)} != {total}")
    return result