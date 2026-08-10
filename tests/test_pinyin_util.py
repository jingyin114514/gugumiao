# -*- coding: utf-8 -*-
from lightmon.pinyin_util import initial_of_name, initials_of_name


def test_initial_basic_names():
    assert initial_of_name("英维克") == "Y"
    assert initial_of_name("金富科技") == "J"
    assert initial_of_name("飞荣达") == "F"
    assert initial_of_name("申菱环境") == "S"
    assert initial_of_name("天地科技") == "T"


def test_initial_polyphone_default():
    assert initial_of_name("中航光电") == "Z"
    assert initial_of_name("中国平安") == "Z"


def test_initial_non_chinese_falls_to_hash():
    assert initial_of_name("3M中国") == "#"
    assert initial_of_name("600519") == "#"
    assert initial_of_name("") == "#"


def test_initials_full_string():
    assert initials_of_name("英维克") == "YWK"
    assert initials_of_name("贵州茅台") == "GZMT"
    assert initials_of_name("") == ""
