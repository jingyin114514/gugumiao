# -*- coding: utf-8 -*-
"""指标计算：52 周区间、估值分位、资金流累计、机构/基本面汇总。"""

import re
from datetime import date, timedelta
from typing import Any, Dict, Optional

import pandas as pd


# ---------------------------------------------------------------- 通用工具
def to_num(value: Any) -> Optional[float]:
    """把任意值安全转成 float，失败返回 None。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_cn_amount(text: Any) -> Optional[float]:
    """解析中文金额为万元。'1.91亿'->19100, '3784.06万'->3784.06, '-8.98亿'->-89800。"""
    if text is None:
        return None
    s = str(text).strip().replace(",", "")
    if not s or s in ("-", "--", "nan", "None"):
        return None
    sign = 1.0
    if s.startswith("-"):
        sign = -1.0
        s = s[1:]
    elif s.startswith("+"):
        s = s[1:]
    s = s.strip()
    multiplier = 1.0
    if s.endswith("亿"):
        multiplier = 10000.0
        s = s[:-1]
    elif s.endswith("万"):
        multiplier = 1.0
        s = s[:-1]
    elif s.endswith("元"):
        multiplier = 1.0 / 10000.0
        s = s[:-1]
    try:
        return sign * float(s) * multiplier
    except ValueError:
        return None


def percentile_of(current: Optional[float], series: pd.Series) -> Optional[float]:
    """当前值在历史序列中的分位（%）：<= 当前值的天数占比。"""
    if current is None:
        return None
    clean = series.dropna()
    if clean.empty:
        return None
    return float((clean <= current).mean() * 100.0)


def fmt_wan(wan: Optional[float]) -> str:
    """万元 -> 展示字符串，如 '+7,912万'、'-10.16亿'。"""
    if wan is None:
        return "n/a"
    sign = "+" if wan > 0 else ""
    if abs(wan) >= 10000:
        return f"{sign}{wan / 10000:,.2f}亿"
    return f"{sign}{wan:,.0f}万"


# ---------------------------------------------------------------- 行情与 52 周
def compute_52w(daily: pd.DataFrame, price: Optional[float]) -> Dict[str, Optional[float]]:
    """基于近一年日线计算 52 周高/低点及当前位置。"""
    if daily is None or daily.empty:
        return {}
    today = pd.Timestamp(date.today())
    window = daily[daily["date"] >= today - pd.Timedelta(days=365)]
    if window.empty:
        window = daily
    high52 = float(window["high"].max())
    low52 = float(window["low"].min())
    if price is None:
        price = float(daily["close"].iloc[-1])
    result: Dict[str, Optional[float]] = {"high52": high52, "low52": low52}
    if high52:
        result["dist_high_pct"] = (price / high52 - 1.0) * 100.0
    if low52:
        result["dist_low_pct"] = (price / low52 - 1.0) * 100.0
    if high52 and low52 and high52 > low52:
        result["pos52_pct"] = (price - low52) / (high52 - low52) * 100.0
    return result


# ---------------------------------------------------------------- 估值分位
def summarize_valuation(df: pd.DataFrame, percentile_years: int = 3,
                        today: Optional[date] = None) -> Dict[str, Any]:
    """从估值历史表计算最新 PE/PB 与 3 年分位。"""
    if df is None or df.empty:
        return {}
    today = today or date.today()
    data = df.copy()
    for col in ("date", "pe_ttm", "pb", "mv_yi"):
        if col not in data.columns:
            return {}
    data["date"] = pd.to_datetime(data["date"])
    data = data.sort_values("date").drop_duplicates("date", keep="last")
    cutoff = pd.Timestamp(today) - pd.Timedelta(days=int(365 * percentile_years))
    window = data[data["date"] >= cutoff]
    if len(window) < 2:
        window = data
    last = data.iloc[-1]
    pe = to_num(last.get("pe_ttm"))
    pb = to_num(last.get("pb"))
    mv_yi = to_num(last.get("mv_yi"))
    return {
        "pe_ttm": pe,
        "pb": pb,
        "pe_pct": percentile_of(pe, window["pe_ttm"]),
        "pb_pct": percentile_of(pb, window["pb"]),
        "mv_yi": mv_yi,
        "val_date": str(last["date"].date()) if pd.notna(last["date"]) else "",
        "val_points": int(window[["pe_ttm", "pb"]].notna().all(axis=1).sum()),
        "close": to_num(last.get("close")),
    }


# ---------------------------------------------------------------- 主力资金
def summarize_em_fund_flow(df: pd.DataFrame) -> Dict[str, Optional[float]]:
    """东财个股资金流（元/日）-> 1/5/20 日累计（万元）。"""
    if df is None or df.empty:
        return {}
    data = df.copy()
    date_col = next((c for c in data.columns if "日期" in c), None)
    flow_col = next((c for c in data.columns if "主力净流入" in c and "占比" not in c), None)
    if not date_col or not flow_col:
        return {}
    data = data.sort_values(date_col)
    flows = pd.to_numeric(data[flow_col], errors="coerce") / 1e4  # 元 -> 万元
    flows = flows.dropna()
    if flows.empty:
        return {}
    return {
        "flow_1d_wan": float(flows.iloc[-1]),
        "flow_5d_wan": float(flows.tail(5).sum()),
        "flow_20d_wan": float(flows.tail(20).sum()),
    }


def summarize_ths_rank(rank_tables: Dict[str, Optional[pd.DataFrame]],
                       code: str) -> Dict[str, Optional[float]]:
    """同花顺排行（即时/5日/20日）-> 各周期主力净流入（万元）。"""
    out: Dict[str, Optional[float]] = {}
    mapping = {"即时": "flow_1d_wan", "5日排行": "flow_5d_wan", "20日排行": "flow_20d_wan"}
    for period, key in mapping.items():
        df = rank_tables.get(period)
        if df is None or df.empty:
            out[key] = None
            continue
        col = next((c for c in ("净额", "资金流入净额") if c in df.columns), None)
        if not col:
            out[key] = None
            continue
        row = df[df["股票代码"].astype(str).str.zfill(6) == code]
        if row.empty:
            out[key] = None
        else:
            out[key] = parse_cn_amount(row.iloc[0][col])
    return out


# ---------------------------------------------------------------- 机构持仓
def summarize_institution(df: pd.DataFrame) -> Dict[str, Any]:
    """新浪机构持股明细 -> 本期/上期家数与占流通股比例。"""
    if df is None or df.empty:
        return {}
    data = df.copy()
    for col in ("最新持股数", "持股数", "最新占流通股比例", "占流通股比例"):
        if col not in data.columns:
            return {}
    data["最新持股数"] = pd.to_numeric(data["最新持股数"], errors="coerce").fillna(0.0)
    data["持股数"] = pd.to_numeric(data["持股数"], errors="coerce").fillna(0.0)
    data["最新占流通股比例"] = pd.to_numeric(data["最新占流通股比例"], errors="coerce").fillna(0.0)
    data["占流通股比例"] = pd.to_numeric(data["占流通股比例"], errors="coerce").fillna(0.0)
    cur = data[data["最新持股数"] > 0]
    prev = data[data["持股数"] > 0]
    return {
        "inst_count": int(len(cur)),
        "inst_count_prev": int(len(prev)),
        "inst_ratio_pct": float(cur["最新占流通股比例"].sum()),
        "inst_ratio_prev_pct": float(prev["占流通股比例"].sum()),
    }


# ---------------------------------------------------------------- 基本面
_REV_COLS = ["TOTALOPERATEREVETZ", "YYZSRGDHBZC", "DJD_TOI_YOY"]
_PROFIT_COLS = ["PARENTNETPROFITTZ", "NETPROFITRPHBZC", "DJD_DPNP_YOY"]
_GM_COLS = ["GROSS_PROFIT_RATIO", "XSMLL", "MLR"]
_NET_PROFIT_COLS = ["PARENTNETPROFIT", "KCFJCXSYJLR"]


def _first_col(df: pd.DataFrame, candidates) -> Optional[str]:
    for col in candidates:
        if col in df.columns:
            return col
    return None


def summarize_fundamentals(df: pd.DataFrame) -> Dict[str, Any]:
    """东财财务主要指标 -> 最新/上期 营收同比、净利同比、毛利率。"""
    if df is None or df.empty:
        return {}
    data = df.copy()
    if "REPORT_DATE" not in data.columns:
        return {}
    data["REPORT_DATE"] = pd.to_datetime(data["REPORT_DATE"], errors="coerce")
    data = data.dropna(subset=["REPORT_DATE"]).sort_values("REPORT_DATE", ascending=False)
    rev_col = _first_col(data, _REV_COLS)
    profit_col = _first_col(data, _PROFIT_COLS)
    gm_col = _first_col(data, _GM_COLS)
    net_profit_col = _first_col(data, _NET_PROFIT_COLS)

    def row_vals(row: pd.Series) -> Dict[str, Optional[float]]:
        return {
            "rev_yoy": to_num(row[rev_col]) if rev_col else None,
            "profit_yoy": to_num(row[profit_col]) if profit_col else None,
            "gross_margin": to_num(row[gm_col]) if gm_col else None,
            "net_profit": to_num(row[net_profit_col]) if net_profit_col else None,
            "report_date": str(row["REPORT_DATE"].date()),
        }

    cur = row_vals(data.iloc[0])
    prev = row_vals(data.iloc[1]) if len(data) > 1 else {}
    merged = dict(cur)
    merged["prev_rev_yoy"] = prev.get("rev_yoy")
    merged["prev_profit_yoy"] = prev.get("profit_yoy")
    merged["prev_gross_margin"] = prev.get("gross_margin")
    merged["net_profit"] = cur.get("net_profit")
    return merged
