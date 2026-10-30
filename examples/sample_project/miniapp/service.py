
"""Application service using the nested calculator module."""

from .nested.calculator import add


def total_with_fee(price: int, fee: int) -> int:
    return add(price, fee)
