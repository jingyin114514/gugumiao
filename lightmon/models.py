# -*- coding: utf-8 -*-
"""单只股票的监控快照数据结构。"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class StockSnapshot:
    code: str = ""
    name: str = ""
    market: str = "sz"

    # ---- 行情 ----
    price: Optional[float] = None          # 最新价（元）
    pct_chg: Optional[float] = None        # 今日涨跌幅 %
    volume: Optional[float] = None         # 成交量（手）
    amount: Optional[float] = None         # 成交额（元）
    quote_time: str = ""

    # ---- 52 周区间 ----
    high52: Optional[float] = None
    low52: Optional[float] = None
    pos52_pct: Optional[float] = None      # 当前价在 52 周区间的位置 %
    dist_high_pct: Optional[float] = None  # 距 52 周高点 %
    dist_low_pct: Optional[float] = None   # 距 52 周低点 %

    # ---- 市值 ----
    mv_yi: Optional[float] = None          # 总市值（亿元）
    mv_source: str = ""

    # ---- 估值 ----
    pe_ttm: Optional[float] = None
    pb: Optional[float] = None
    pe_pct: Optional[float] = None         # 3 年分位 %
    pb_pct: Optional[float] = None
    val_date: str = ""
    val_points: int = 0                    # 分位样本数

    # ---- 主力资金（万元）----
    flow_1d_wan: Optional[float] = None
    flow_5d_wan: Optional[float] = None
    flow_20d_wan: Optional[float] = None
    flow_source: str = ""

    # ---- 机构持仓（十大流通股东口径）----
    inst_count: Optional[int] = None
    inst_count_prev: Optional[int] = None
    inst_ratio_pct: Optional[float] = None
    inst_ratio_prev_pct: Optional[float] = None
    inst_date: str = ""

    # ---- 基本面（最新报告期）----
    rev_yoy: Optional[float] = None        # 营收同比 %
    profit_yoy: Optional[float] = None     # 归母净利同比 %
    net_profit: Optional[float] = None     # 归母净利润（元，判断是否亏损）
    gross_margin: Optional[float] = None   # 毛利率 %
    prev_rev_yoy: Optional[float] = None
    prev_profit_yoy: Optional[float] = None
    prev_gross_margin: Optional[float] = None
    report_date: str = ""

    # ---- 用户配置 ----
    cost: Optional[float] = None
    target_weight: Optional[float] = None
    industry_light: str = "yellow"
    industry_note: str = ""
    margin_light: Optional[str] = None
    margin_note: str = ""

    # ---- 灯号结论 ----
    lights: Dict[str, str] = field(default_factory=dict)
    light_reasons: Dict[str, str] = field(default_factory=dict)
    green_count: int = 0
    red_count: int = 0
    buildable: bool = False
    status: str = ""          # 可建仓 / 接近可建仓 / 暂不可建仓
    alert: str = ""           # 相对上次运行的状态变化提示
    warnings: List[str] = field(default_factory=list)
    threshold_wan: Optional[float] = None  # 本次命中的主力阈值（万元）
    tier_name: str = ""

    @property
    def pnl_pct(self) -> Optional[float]:
        if self.cost and self.price:
            return (self.price - self.cost) / self.cost * 100.0
        return None

    @property
    def inst_count_chg(self) -> Optional[int]:
        if self.inst_count is not None and self.inst_count_prev is not None:
            return self.inst_count - self.inst_count_prev
        return None

    @property
    def inst_ratio_chg(self) -> Optional[float]:
        if self.inst_ratio_pct is not None and self.inst_ratio_prev_pct is not None:
            return self.inst_ratio_pct - self.inst_ratio_prev_pct
        return None

def stock_to_dict(stock: StockSnapshot) -> Dict[str, Any]:
    """序列化为 JSON 友好的字典（供本地面板渲染）。"""
    keys = [
        "code", "name", "market", "price", "pct_chg", "volume", "amount",
        "high52", "low52", "pos52_pct", "dist_high_pct", "dist_low_pct",
        "mv_yi", "mv_source", "pe_ttm", "pb", "pe_pct", "pb_pct", "val_date", "val_points",
        "flow_1d_wan", "flow_5d_wan", "flow_20d_wan", "flow_source",
        "inst_count", "inst_count_prev", "inst_ratio_pct", "inst_ratio_prev_pct", "inst_date",
        "rev_yoy", "profit_yoy", "gross_margin", "prev_rev_yoy", "prev_profit_yoy", "report_date",
        "cost", "target_weight", "industry_light", "industry_note", "margin_light", "margin_note",
        "lights", "light_reasons", "green_count", "red_count", "buildable", "status", "alert",
        "warnings", "threshold_wan", "tier_name",
        "pnl_pct", "inst_count_chg", "inst_ratio_chg",
    ]
    return {key: getattr(stock, key) for key in keys}
