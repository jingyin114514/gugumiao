# -*- coding: utf-8 -*-
"""股票名称 -> 拼音首字母。"""
from typing import Optional

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:  # 依赖缺失时降级：返回空串，前端归入 "#" 组
    lazy_pinyin = None


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def initials_of_name(name: Optional[str]) -> str:
    """返回名称完整拼音首字母串（大写），如 '英维克' -> 'YWK'。"""
    if not name or lazy_pinyin is None:
        return ""
    chars = lazy_pinyin(str(name), style=Style.FIRST_LETTER)
    return "".join(ch[0] for ch in chars if ch).upper()


def initial_of_name(name: Optional[str]) -> str:
    """返回名称首字母（A-Z）；名称为空或首字符非汉字返回 '#'。"""
    text = str(name or "")
    if not text or not _is_cjk(text[0]):
        return "#"
    initials = initials_of_name(text)
    return initials[0].upper() if initials else "#"
