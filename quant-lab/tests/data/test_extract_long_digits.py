"""真实导出实测：消息里的订单号、哈希等超长数字串曾让 round(Decimal, 12) 抛 InvalidOperation，整批导入中断。"""
from __future__ import annotations

import decimal

import pytest

from quant_lab.data import extract as X

LONG = "12345678901234567890"          # 20 位：远超任何价格


def test_long_digit_strings_are_not_numbers_and_do_not_crash():
    nums = X.find_numbers(f"订单号 {LONG} 入场 62000")
    assert [int(n.value) for n in nums] == [62000]
    assert X.parse_number_token(LONG) is None


def test_long_digit_range_does_not_crash_entry_parsing():
    X.parse_message(f"BTC 做多\n入场：{LONG}-{LONG}1\n止损：61000")


def test_fifteen_digits_still_parse():
    assert X.parse_number_token("123456789012345") == 123456789012345


def test_mutant_without_the_length_guard_crashes(monkeypatch):
    """突变：去掉长度线，同一条消息就会让导入抛 InvalidOperation。"""
    monkeypatch.setattr(X, "_too_long", lambda token: False)
    with pytest.raises(decimal.InvalidOperation):
        X.find_numbers(f"订单号 {LONG}")
