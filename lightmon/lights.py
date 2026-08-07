# -*- coding: utf-8 -*-
"""六维灯号判断与综合建仓结论。"""

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .models import StockSnapshot


DIMENSIONS = ["industry", "fundamental", "valuation", "chips", "capital", "margin"]
DIM_LABELS = {
    "industry": "产业逻辑",
    "fundamental": "基本面",
    "valuation": "估值",
    "chips": "长期筹码",
    "capital": "短期主力",
    "margin": "边际变化",
}
RED_TEXT = {
    "industry": "产业逻辑转弱",
    "fundamental": "基本面恶化",
    "valuation": "估值过高",
    "chips": "机构减仓",
    "capital": "主力净流出",
    "margin": "存在利空",
}


def evaluate(stock: StockSnapshot, cfg: Dict[str, Any]) -> None:
    """计算全部灯号并写入 stock 对象。"""
    lights: Dict[str, str] = {}
    reasons: Dict[str, str] = {}

    lights["industry"], reasons["industry"] = _industry(stock)
    lights["fundamental"], reasons["fundamental"] = _fundamental(stock, cfg)
    lights["valuation"], reasons["valuation"] = _valuation(stock, cfg)
    lights["chips"], reasons["chips"] = _chips(stock, cfg)
    lights["capital"], reasons["capital"] = _capital(stock, cfg)
    lights["margin"], reasons["margin"] = _margin(stock, cfg)

    stock.lights = lights
    stock.light_reasons = reasons
    stock.green_count = sum(1 for v in lights.values() if v == "green")
    stock.red_count = sum(1 for v in lights.values() if v == "red")

    min_green = int(cfg.get("build", {}).get("min_green", 4))
    max_red = int(cfg.get("build", {}).get("max_red", 0))
    stock.buildable = stock.green_count >= min_green and stock.red_count <= max_red
    if stock.buildable:
        stock.status = "可建仓"
    elif stock.green_count >= min_green - 1 and stock.red_count <= max_red + 1:
        stock.status = "接近可建仓"
    else:
        stock.status = "暂不可建仓"


def _industry(stock: StockSnapshot) -> Tuple[str, str]:
    light = (stock.industry_light or "yellow").lower()
    if light not in ("green", "yellow", "red"):
        light = "yellow"
    note = stock.industry_note or ""
    return light, note


def _fundamental(stock: StockSnapshot, cfg: Dict[str, Any]) -> Tuple[str, str]:
    rev = stock.rev_yoy
    profit = stock.profit_yoy
    net_profit = stock.net_profit
    gm = stock.gross_margin
    prev_gm = getattr(stock, "prev_gross_margin", None)
    prev_profit = stock.prev_profit_yoy
    floor_pp = float(cfg.get("fundamentals", {}).get("gross_margin_floor_pp", 2.0))
    if rev is None or profit is None:
        return "yellow", "财务数据缺失"
    if net_profit is not None and net_profit < 0:
        return "red", "持续亏损"
    if rev < 0:
        return "red", "营收下滑"
    gm_ok = True
    if gm is not None and prev_gm is not None:
        gm_ok = gm >= prev_gm - floor_pp
    if rev > 0 and profit > 0:
        if gm_ok:
            if prev_profit is not None and profit < prev_profit:
                return "yellow", f"利润增速放缓（同比 {profit:.0f}% < 上期 {prev_profit:.0f}%）"
            return "green", "营收利润双增，毛利率稳定"
        return "yellow", f"增收增利但毛利率下滑 {gm - prev_gm:.1f}pp"
    if rev > 0 and profit <= 0:
        return "yellow", f"增收不增利（净利同比 {profit:.0f}%）"
    return "yellow", "利润增速放缓"


def _valuation(stock: StockSnapshot, cfg: Dict[str, Any]) -> Tuple[str, str]:
    val = cfg.get("valuation", {})
    green_max = float(val.get("green_max_pct", 50))
    red_min = float(val.get("red_min_pct", 80))
    pe_extreme = float(val.get("pe_extreme", 150))
    min_points = int(val.get("min_data_points", 60))
    pe, pb = stock.pe_ttm, stock.pb
    if pe is None or pb is None:
        return "yellow", "估值数据缺失"
    if pe <= 0:
        return "red", "PE 为负（亏损）"
    if stock.val_points and stock.val_points < min_points:
        return "yellow", f"分位样本不足（{stock.val_points} 天）"
    if pe > pe_extreme:
        return "red", f"PE 极高（{pe:.1f} 倍）"
    pe_pct = stock.pe_pct
    pb_pct = stock.pb_pct
    if pe_pct is None or pb_pct is None:
        return "yellow", "分位数据缺失"
    if pe_pct >= red_min and pb_pct >= red_min:
        return "red", f"PE/PB 双高分位（{pe_pct:.0f}%/{pb_pct:.0f}%）"
    if pe_pct < green_max and pb_pct < green_max:
        return "green", f"PE/PB 均处中低位（{pe_pct:.0f}%/{pb_pct:.0f}%）"
    return "yellow", f"估值分位分化（PE {pe_pct:.0f}% / PB {pb_pct:.0f}%）"


def _chips(stock: StockSnapshot, cfg: Dict[str, Any]) -> Tuple[str, str]:
    chips_cfg = cfg.get("chips", {})
    count_up = float(chips_cfg.get("inst_count_up", 3))
    count_down = float(chips_cfg.get("inst_count_down", -5))
    ratio_up = float(chips_cfg.get("inst_ratio_up_pp", 0.2))
    ratio_down = float(chips_cfg.get("inst_ratio_down_pp", -0.5))

    inst_missing = stock.inst_count is None or stock.inst_count_prev is None
    count_chg = stock.inst_count_chg
    ratio_chg = stock.inst_ratio_chg
    inst_up = (not inst_missing) and (
        (count_chg is not None and count_chg >= count_up)
        or (ratio_chg is not None and ratio_chg >= ratio_up)
    )
    inst_down = (not inst_missing) and (
        (count_chg is not None and count_chg <= count_down)
        or (ratio_chg is not None and ratio_chg <= ratio_down)
    )
    if inst_missing:
        return "yellow", "机构持仓数据缺失"
    if inst_down:
        return "red", "机构明显减仓"
    if inst_up:
        return "green", "机构持续增持"
    return "yellow", "机构持仓持平"


def _capital(stock: StockSnapshot, cfg: Dict[str, Any]) -> Tuple[str, str]:
    flow5 = stock.flow_5d_wan
    if flow5 is None:
        return "yellow", "主力资金数据缺失"
    threshold = stock.threshold_wan
    if threshold is None:
        threshold = 5000.0
    capital_cfg = cfg.get("capital", {})
    if capital_cfg.get("green_any_inflow") and flow5 > 0:
        return "green", f"5 日主力净流入 {flow5 / 10000:.2f} 亿"
    if flow5 >= threshold:
        return "green", f"5 日主力净流入 {flow5 / 10000:.2f} 亿（> {threshold / 10000:.0f} 亿阈值）"
    if flow5 <= -threshold:
        return "red", f"5 日主力净流出 {abs(flow5) / 10000:.2f} 亿（> {threshold / 10000:.0f} 亿阈值）"
    return "yellow", f"5 日主力净流 {flow5 / 10000:+.2f} 亿（阈值内）"


def _margin(stock: StockSnapshot, cfg: Dict[str, Any]) -> Tuple[str, str]:
    override = (stock.margin_light or "").lower()
    if override in ("green", "yellow", "red"):
        return override, "手动指定"
    note = stock.margin_note or ""
    keywords = cfg.get("margin_keywords", {})
    bullish = [k for k in keywords.get("bullish", []) if k in note]
    bearish = [k for k in keywords.get("bearish", []) if k in note]
    if bearish:
        return "red", f"检测到利空关键词：{', '.join(bearish)}"
    if bullish:
        return "green", f"检测到催化剂关键词：{', '.join(bullish)}"
    return "yellow", "催化剂不明确"


def red_summary(stock: StockSnapshot) -> List[str]:
    """红灯对应的简短原因，用于关注清单。"""
    return [RED_TEXT.get(dim, dim) for dim, light in stock.lights.items() if light == "red"]


def light_str(stock: StockSnapshot) -> str:
    return "".join({"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(stock.lights.get(d), "🟡")
                   for d in DIMENSIONS)
