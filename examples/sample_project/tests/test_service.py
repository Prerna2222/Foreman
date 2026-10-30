
from miniapp.service import total_with_fee
from miniapp.nested.calculator import multiply


def test_total_with_fee():
    assert total_with_fee(10, 5) == 15


def test_multiply():
    assert multiply(4, 3) == 12
