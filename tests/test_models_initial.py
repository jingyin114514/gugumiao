# -*- coding: utf-8 -*-
from lightmon.models import StockSnapshot, stock_to_dict


def test_stock_to_dict_includes_initial():
    stock = StockSnapshot(code="002837", name="英维克")
    data = stock_to_dict(stock)
    assert data["initial"] == "Y"
    assert data["pinyin_initials"] == "YWK"


def test_stock_to_dict_initial_hash_for_empty_name():
    stock = StockSnapshot(code="600519", name="")
    data = stock_to_dict(stock)
    assert data["initial"] == "#"
