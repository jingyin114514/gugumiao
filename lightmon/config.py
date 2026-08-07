# -*- coding: utf-8 -*-
"""配置加载与校验：股票池、阈值、灯号规则全部走 config.json。"""

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_CONFIG: Dict[str, Any] = {
    "data": {
        "history_file": "data/history.csv",
        "reports_dir": "data/reports",
        "timeout_seconds": 25,
        "max_workers": 4,
        # 主力资金首选数据源: auto=东财优先、失败自动切同花顺; em=只用东财; ths=只用同花顺
        "preferred_fund_flow": "auto",
    },
    # 市值分档与主力资金阈值（万元）。max_yi 为上限（含），null 表示兜底档。
    "market_cap_tiers": [
        {"name": "小盘", "max_yi": 50, "threshold_wan": 3000},
        {"name": "中盘", "max_yi": 200, "threshold_wan": 5000},
        {"name": "大盘", "max_yi": None, "threshold_wan": 10000},
    ],
    "valuation": {
        "percentile_years": 3,
        "green_max_pct": 50,   # PE/PB 分位均低于该值 -> 绿灯
        "red_min_pct": 80,     # PE/PB 分位均高于该值 -> 红灯
        "pe_extreme": 150,     # PE(TTM) 高于该绝对值视为 PE 极高
        "min_data_points": 60, # 分位计算最少样本数，不足则标黄
    },
    "fundamentals": {
        "gross_margin_floor_pp": 2.0,  # 毛利率较上期下滑超过该百分点视为毛利率恶化
    },
    "chips": {
        "inst_count_up": 3,        # 机构家数增加 >= 该值视为明显增持
        "inst_count_down": -5,     # 机构家数减少 <= 该值视为明显减仓
        "inst_ratio_up_pp": 0.2,   # 机构占流通股比例提升 >= 该百分点视为增持
        "inst_ratio_down_pp": -0.5,  # 机构占流通股比例下降 <= 该百分点视为减仓
    },
    "capital": {
        # 若为 true，5 日主力净流入 > 0 即亮绿灯（阈值表仍用于红灯判定）
        "green_any_inflow": False,
    },
    "margin_keywords": {
        "bullish": ["订单", "中标", "政策", "新品", "预增", "回购", "涨价", "扩产", "放量"],
        "bearish": ["解禁", "减持", "暴雷", "立案", "诉讼", "商誉减值", "亏损", "处罚", "仲裁"],
    },
    "build": {"min_green": 4, "max_red": 0},
    "watchlist": [],
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """把用户配置覆盖到默认配置上（嵌套字典逐层合并）。"""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(path: str = "config.json") -> Dict[str, Any]:
    """读取配置文件；文件不存在时生成默认模板并返回默认配置。"""
    cfg_path = Path(path)
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8-sig") as fh:
            user_cfg = json.load(fh)
        cfg = _deep_merge(DEFAULT_CONFIG, user_cfg)
    else:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        save_default_config(cfg_path)
    normalize(cfg)
    return cfg


def save_default_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(DEFAULT_CONFIG, fh, ensure_ascii=False, indent=2)


def normalize(cfg: Dict[str, Any]) -> None:
    """补全 watchlist 字段、推断市场、规范化代码。"""
    for item in cfg.get("watchlist", []):
        code, market = normalize_code(item.get("code", ""))
        item["code"] = code
        item["market"] = item.get("market") or market
        item.setdefault("name", "")
        item.setdefault("cost", None)
        item.setdefault("target_weight", None)
        item.setdefault("industry_light", "yellow")
        item.setdefault("industry_note", "")
        item.setdefault("margin_light", None)  # 手动覆盖：green/yellow/red
        item.setdefault("margin_note", "")


def normalize_code(raw: str):
    """返回 (6 位代码, 市场)。支持 '600519'、'sh600519'、'SZ000001' 等写法。"""
    raw = (raw or "").strip().lower()
    market = ""
    for prefix in ("sh", "sz", "bj"):
        if raw.startswith(prefix):
            market = prefix
            raw = raw[len(prefix):]
            break
    raw = raw.zfill(6)[-6:]
    if not market:
        if raw.startswith(("60", "68", "9")):
            market = "sh"
        elif raw.startswith(("00", "30", "20")):
            market = "sz"
        else:
            market = "bj"
    return raw, market


def with_market_symbol(code: str, market: str) -> str:
    """转成东财财务接口需要的 '002837.SZ' 形式。"""
    return f"{code}.{market.upper()}"


def find_tier(cfg: Dict[str, Any], mv_yi: Optional[float]) -> Optional[Dict[str, Any]]:
    """按总市值(亿元)匹配分档；市值缺失返回 None。"""
    if mv_yi is None:
        return None
    for tier in cfg.get("market_cap_tiers", []):
        max_yi = tier.get("max_yi")
        if max_yi is None or mv_yi <= max_yi:
            return tier
    return cfg.get("market_cap_tiers", [{}])[-1]


def recent_quarters(today, n: int = 8) -> List[str]:
    """生成最近 n 个报告期代码，如 ['20262','20261','20254',...]（新浪机构持股格式）。"""
    quarters: List[str] = []
    year = today.year
    q = (today.month - 1) // 3 + 1
    while len(quarters) < n:
        quarters.append(f"{year}{q}")
        q -= 1
        if q < 1:
            q = 4
            year -= 1
    return quarters
