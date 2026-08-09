# -*- coding: utf-8 -*-
"""输出：CLI 报告、Markdown 报告、CSV 历史记录。"""

import csv
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .lights import DIMENSIONS, DIM_LABELS, light_str, red_summary
from .models import StockSnapshot


# ------------------------------------------------------------------ 终端配色
class Palette:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    DIM = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def _enable_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


def _color(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{Palette.RESET}" if enabled else text


def _disp_width(text: str) -> int:
    """终端显示宽度：CJK/全角按 2 计算。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    gap = width - _disp_width(text)
    if gap <= 0:
        return text
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def _fmt_price(pct: Optional[float]) -> str:
    if pct is None:
        return "--"
    return f"{pct:+.2f}%"


def _fmt_wan(wan: Optional[float]) -> str:
    if wan is None:
        return "--"
    sign = "+" if wan > 0 else ""
    if abs(wan) >= 10000:
        return f"{sign}{wan / 10000:,.2f}亿"
    return f"{sign}{wan:,.0f}万"


def _fmt_int(value: Optional[float]) -> str:
    return "--" if value is None else f"{int(value):,}"


# ------------------------------------------------------------------ CLI 报告
def render_cli(stocks: List[StockSnapshot], cfg: Dict[str, Any], meta: Dict[str, Any],
               color: bool = False) -> str:
    if color:
        _enable_ansi()
    now = meta.get("run_time", datetime.now())
    lines: List[str] = []
    width = 60
    sep = "=" * width
    lines.append(sep)
    title = f"📊 自选股监控报告 - {now:%Y-%m-%d %H:%M}"
    lines.append(_pad(title, width, "center"))
    lines.append(sep)
    sources_txt = f"数据源: {meta.get('data_sources', '--')} · 共 {len(stocks)} 只"
    lines.append(_color(sources_txt, Palette.DIM, color))
    lines.append("灯号顺序：产业 · 基本面 · 估值 · 长期筹码 · 短期主力 · 边际变化")
    lines.append("")

    for idx, stock in enumerate(stocks):
        lines.extend(_render_stock_block(stock, color))
        if idx < len(stocks) - 1:
            lines.append("")

    lines.append("")
    lines.append(sep)
    lines.append("🚦 建仓清单（绿灯≥4 且无红灯）:")
    buildable = [s for s in stocks if s.buildable]
    if buildable:
        for s in buildable:
            tag = s.alert
            suffix = f"  {tag}" if tag else ""
            lines.append(f"→ {s.name} {s.code}{suffix}")
    else:
        lines.append("→ 暂无")

    alerts = [s for s in stocks if s.alert]
    if alerts:
        lines.append("")
        lines.append("🔥 状态变化提醒（上次不可建仓 → 本次可建仓）:")
        for s in alerts:
            lines.append(f"→ {s.name} {s.code}：新进入建仓清单，请重点评估")

    watched = [s for s in stocks if s.red_count >= 2]
    if watched:
        lines.append("")
        lines.append("⚠️ 需要关注的票（出现 2 个以上红灯）:")
        for s in watched:
            reasons = "，".join(red_summary(s))
            lines.append(f"→ {s.name}（{s.red_count}红）：{reasons}")

    warnings = [(s, w) for s in stocks for w in s.warnings]
    if warnings:
        lines.append("")
        lines.append("⚠️ 数据缺失提示:")
        for s, w in warnings[:8]:
            lines.append(f"→ {s.name} {s.code}：{w}")
        if len(warnings) > 8:
            lines.append(f"→ 另有 {len(warnings) - 8} 条，详见 Markdown 报告")

    lines.append(sep)
    if meta.get("report_path"):
        lines.append(f"{Palette.DIM}报告已保存: {meta['report_path']}{Palette.RESET}" if color
                     else f"报告已保存: {meta['report_path']}")
    return "\n".join(lines)


def _render_stock_block(stock: StockSnapshot, color: bool) -> List[str]:
    pct_color = Palette.GREEN if (stock.pct_chg or 0) >= 0 else Palette.RED
    arrow = "📈" if (stock.pct_chg or 0) >= 0 else "📉"
    lines: List[str] = []
    price_txt = f"{stock.price:.2f}元" if stock.price is not None else "--元"
    pct_txt = _fmt_price(stock.pct_chg)
    header = f"【{stock.name} {stock.code}】  {price_txt}  "
    if color:
        header += _color(pct_txt, pct_color, True)
    else:
        header += pct_txt
    header += f"  {arrow}"
    if stock.alert:
        header += _color("  🔥 " + stock.alert, Palette.YELLOW, color)
    lines.append(header)

    light_txt = light_str(stock)
    lines.append(f"├─ 灯号: {light_txt} → {stock.status}（绿{stock.green_count} / 红{stock.red_count}）")

    val = f"PE {stock.pe_ttm:.1f}倍(TTM)" if stock.pe_ttm is not None else "PE --"
    if stock.pe_pct is not None:
        val += f"·分位{stock.pe_pct:.0f}%"
    val += " | "
    val += f"PB {stock.pb:.1f}倍" if stock.pb is not None else "PB --"
    if stock.pb_pct is not None:
        val += f"·分位{stock.pb_pct:.0f}%"
    if stock.val_date:
        val += f" {Palette.DIM}({stock.val_date}){Palette.RESET}" if color else f" ({stock.val_date})"
    lines.append(f"├─ 估值: {val}")

    flow = f"1日 {_fmt_wan(stock.flow_1d_wan)}"
    flow5 = _fmt_wan(stock.flow_5d_wan)
    light5 = stock.lights.get("capital")
    if color and light5:
        flow5 = _color(flow5, {"green": Palette.GREEN, "red": Palette.RED}.get(light5, Palette.YELLOW), True)
    flow += f" | 5日 {flow5} | 20日 {_fmt_wan(stock.flow_20d_wan)}"
    if stock.flow_source:
        flow += f" {Palette.DIM}({stock.flow_source}){Palette.RESET}" if color else f" ({stock.flow_source})"
    lines.append(f"├─ 主力: {flow}")

    if stock.inst_count is not None:
        chg_txt = ""
        if stock.inst_count_chg is not None or stock.inst_ratio_chg is not None:
            bits = []
            if stock.inst_count_chg is not None:
                bits.append(f"家数{stock.inst_count_chg:+d}")
            if stock.inst_ratio_chg is not None:
                bits.append(f"比例{stock.inst_ratio_chg:+.2f}pp")
            if bits:
                chg_txt = f"（{'，'.join(bits)}）"
        inst_line = f"├─ 机构: {stock.inst_count}家 · 占流通{stock.inst_ratio_pct:.2f}%{chg_txt}"
        if stock.inst_date:
            inst_line += _color(f"（{stock.inst_date}）", Palette.DIM, color)
        lines.append(inst_line)
    else:
        lines.append("├─ 机构: 数据缺失")

    if stock.high52 is not None and stock.low52 is not None:
        pos = f"位置{stock.pos52_pct:.0f}%" if stock.pos52_pct is not None else "位置--"
        lines.append(f"├─ 位置: 52周高 {stock.high52:.2f} · 低 {stock.low52:.2f} · {pos}"
                     + (f" | 距高 {stock.dist_high_pct:+.1f}% | 距低 {stock.dist_low_pct:+.1f}%"
                        if stock.dist_high_pct is not None and stock.dist_low_pct is not None else ""))
    else:
        lines.append("├─ 位置: 数据缺失")

    cost_txt = f"成本 {stock.cost:.2f}" if stock.cost is not None else "成本 --"
    pnl = stock.pnl_pct
    pnl_txt = f"盈亏 {pnl:+.2f}%" if pnl is not None else "盈亏 --"
    if color and pnl is not None:
        pnl_txt = _color(pnl_txt, Palette.GREEN if pnl >= 0 else Palette.RED, True)
    weight_txt = f"目标仓位 {stock.target_weight:.1f}%" if stock.target_weight is not None else "目标仓位 --"
    tier = f" | {stock.tier_name}股" if stock.tier_name else ""
    lines.append(f"└─ 仓位: {cost_txt} · {pnl_txt} · {weight_txt}{tier}")
    return lines


# ------------------------------------------------------------------ Markdown 报告
_MD_STYLE = """
<style>
  :root { --ink:#1F2937; --muted:#6B7280; --green:#16A34A; --amber:#D97706; --red:#DC2626; --line:#E5E7EB; }
  body { font-family:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; color:var(--ink);
         max-width:920px; margin:0 auto; padding:28px 20px; line-height:1.65; }
  h1 { font-size:1.65em; margin:0 0 4px; border-left:4px solid var(--ink); padding-left:12px; }
  .meta { color:var(--muted); font-size:0.92em; margin-bottom:24px; }
  h2 { font-size:1.15em; margin:28px 0 8px; padding-bottom:6px; border-bottom:1px solid var(--line); }
  table { border-collapse:collapse; width:100%; font-size:0.92em; margin:12px 0; }
  th,td { border:1px solid var(--line); padding:6px 10px; text-align:center; }
  th { background:#F9FAFB; font-weight:600; }
  td.l, th.l { text-align:left; }
  .ok { color:var(--green); font-weight:600; }
  .warn { color:var(--amber); font-weight:600; }
  .bad { color:var(--red); font-weight:600; }
  .dim { color:var(--muted); }
  code { background:#F3F4F6; padding:1px 5px; border-radius:4px; font-size:0.92em; }
  blockquote { border-left:3px solid var(--line); margin:8px 0; padding:2px 14px; color:var(--muted); }
</style>
"""


def render_markdown(stocks: List[StockSnapshot], cfg: Dict[str, Any], meta: Dict[str, Any]) -> str:
    now = meta.get("run_time", datetime.now())
    lines: List[str] = []
    lines.append(_MD_STYLE.strip())
    lines.append("")
    lines.append(f"# 📊 自选股监控报告")
    lines.append("")
    lines.append(f"<div class=\"meta\">{now:%Y-%m-%d %H:%M} · 数据源 {meta.get('data_sources', '--')} · "
                 f"共 {len(stocks)} 只 · 灯号顺序：产业 · 基本面 · 估值 · 长期筹码 · 短期主力 · 边际变化</div>")
    lines.append("")
    lines.append("## 灯号总览")
    lines.append("")
    lines.append("| 股票 | 产业 | 基本面 | 估值 | 长期筹码 | 短期主力 | 边际 | 绿 | 红 | 综合 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for s in stocks:
        marks = {k: {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(s.lights.get(k), "🟡")
                 for k in DIMENSIONS}
        cls = "ok" if s.buildable else ("bad" if s.red_count >= 2 else "")
        lines.append(f"| **{s.name}** {s.code} | {marks['industry']} | {marks['fundamental']} | "
                     f"{marks['valuation']} | {marks['chips']} | {marks['capital']} | {marks['margin']} | "
                     f"{s.green_count} | {s.red_count} | <span class=\"{cls}\">{s.status}</span> |")
    lines.append("")
    lines.append("## 建仓清单")
    lines.append("")
    buildable = [s for s in stocks if s.buildable]
    if buildable:
        for s in buildable:
            lines.append(f"- **{s.name} {s.code}**：六灯 {light_str(s)}，{s.status}。{s.alert or ''}")
    else:
        lines.append("暂无。")
    lines.append("")
    lines.append("## 关注清单（红灯 ≥ 2）")
    lines.append("")
    watched = [s for s in stocks if s.red_count >= 2]
    if watched:
        for s in watched:
            reasons = "，".join(red_summary(s))
            lines.append(f"- **{s.name} {s.code}**（{s.red_count}红）：{reasons}")
    else:
        lines.append("暂无。")
    lines.append("")

    lines.append("## 个股明细")
    for s in stocks:
        lines.append("")
        lines.append(f"### {s.name}（{s.code}）{light_str(s)}")
        lines.append("")
        lines.append(f"- **现价** {s.price:.2f} 元（{s.pct_chg:+.2f}%）" if s.price is not None else "- **现价** 数据缺失")
        lines.append(f"- **灯号** {light_str(s)} → **{s.status}**（绿 {s.green_count} / 红 {s.red_count}）")
        for dim in DIMENSIONS:
            mark = {"green": "🟢", "yellow": "🟡", "red": "🔴"}[s.lights.get(dim, "yellow")]
            reason = s.light_reasons.get(dim, "")
            if reason:
                lines.append(f"  - {DIM_LABELS[dim]}：{mark} {reason}")
        lines.append(f"- **估值** PE(TTM) {s.pe_ttm:.1f} 倍（分位 {s.pe_pct:.0f}%）· PB {s.pb:.1f} 倍（分位 {s.pb_pct:.0f}%）"
                     if s.pe_ttm is not None else "- **估值** 数据缺失")
        lines.append(f"- **主力资金** 1日 {_fmt_wan(s.flow_1d_wan)} · 5日 {_fmt_wan(s.flow_5d_wan)} · "
                     f"20日 {_fmt_wan(s.flow_20d_wan)}（{s.flow_source or 'n/a'}）")
        if s.inst_count is not None:
            chg = ""
            if s.inst_count_chg is not None and s.inst_ratio_chg is not None:
                chg = f"，环比家数 {s.inst_count_chg:+d}、比例 {s.inst_ratio_chg:+.2f}pp"
            lines.append(f"- **机构持仓** {s.inst_count} 家 · 占流通股 {s.inst_ratio_pct:.2f}%{chg}（{s.inst_date}）")
        else:
            lines.append("- **机构持仓** 数据缺失")
        if s.high52 is not None and s.low52 is not None:
            lines.append(f"- **52周区间** 高 {s.high52:.2f} · 低 {s.low52:.2f} · 现价位置 {s.pos52_pct:.0f}%"
                         + (f" · 距高 {s.dist_high_pct:+.1f}% · 距低 {s.dist_low_pct:+.1f}%" if s.dist_high_pct is not None else ""))
        if s.report_date:
            lines.append(f"- **最新财报**（{s.report_date}）营收同比 {s.rev_yoy:+.1f}% · 净利同比 {s.profit_yoy:+.1f}%"
                         + (f" · 毛利率 {s.gross_margin:.1f}%" if s.gross_margin is not None else ""))
        if s.cost is not None:
            pnl = s.pnl_pct
            lines.append(f"- **我的仓位** 成本 {s.cost:.2f} · 盈亏 {pnl:+.2f}% · 目标仓位 {s.target_weight or 0:.1f}%")
        if s.industry_note:
            lines.append(f"- **产业逻辑** {s.industry_note}")
        if s.margin_note:
            lines.append(f"- **边际变化备注** {s.margin_note}")
        if s.warnings:
            lines.append("- **数据提示** " + "；".join(s.warnings))
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("<div class=\"dim\">本报告由灯号框架自选股监控工具生成，数据来自公开接口，仅供个人决策参考，不构成投资建议。</div>")
    return "\n".join(lines)


# ------------------------------------------------------------------ CSV 历史
CSV_COLUMNS = [
    "run_time", "code", "name", "price", "pct_chg", "volume", "amount", "mv_yi",
    "high52", "low52", "pos52_pct", "pe_ttm", "pb", "pe_pct", "pb_pct",
    "flow_1d_wan", "flow_5d_wan", "flow_20d_wan",
    "inst_count", "inst_count_chg", "inst_ratio_pct", "inst_ratio_chg", "inst_date",
    "rev_yoy", "profit_yoy", "gross_margin", "report_date",
    "industry_light", "fundamental_light", "valuation_light", "chips_light",
    "capital_light", "margin_light", "green_count", "red_count", "buildable",
    "cost", "pnl_pct", "target_weight", "status", "warnings",
]


def _row_for(stock: StockSnapshot, run_time: datetime) -> Dict[str, Any]:
    return {
        "run_time": run_time.strftime("%Y-%m-%d %H:%M:%S"),
        "code": stock.code,
        "name": stock.name,
        "price": stock.price,
        "pct_chg": stock.pct_chg,
        "volume": stock.volume,
        "amount": stock.amount,
        "mv_yi": stock.mv_yi,
        "high52": stock.high52,
        "low52": stock.low52,
        "pos52_pct": stock.pos52_pct,
        "pe_ttm": stock.pe_ttm,
        "pb": stock.pb,
        "pe_pct": stock.pe_pct,
        "pb_pct": stock.pb_pct,
        "flow_1d_wan": stock.flow_1d_wan,
        "flow_5d_wan": stock.flow_5d_wan,
        "flow_20d_wan": stock.flow_20d_wan,
        "inst_count": stock.inst_count,
        "inst_count_chg": stock.inst_count_chg,
        "inst_ratio_pct": stock.inst_ratio_pct,
        "inst_ratio_chg": stock.inst_ratio_chg,
        "inst_date": stock.inst_date,
        "rev_yoy": stock.rev_yoy,
        "profit_yoy": stock.profit_yoy,
        "gross_margin": stock.gross_margin,
        "report_date": stock.report_date,
        "industry_light": stock.lights.get("industry", ""),
        "fundamental_light": stock.lights.get("fundamental", ""),
        "valuation_light": stock.lights.get("valuation", ""),
        "chips_light": stock.lights.get("chips", ""),
        "capital_light": stock.lights.get("capital", ""),
        "margin_light": stock.lights.get("margin", ""),
        "green_count": stock.green_count,
        "red_count": stock.red_count,
        "buildable": 1 if stock.buildable else 0,
        "cost": stock.cost,
        "pnl_pct": stock.pnl_pct,
        "target_weight": stock.target_weight,
        "status": stock.status,
        "warnings": "；".join(stock.warnings),
    }


def append_history(path: Path, stocks: List[StockSnapshot], run_time: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        if new_file:
            writer.writeheader()
        for stock in stocks:
            writer.writerow(_row_for(stock, run_time))


def load_previous_buildable(path: Path) -> Dict[str, bool]:
    """读取历史里每只股票最近一次是否可建仓（用于状态变化提醒）。"""
    if not path.exists():
        return {}
    result: Dict[str, bool] = {}
    with open(path, "r", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            code = (row.get("code") or "").strip()
            if not code:
                continue
            try:
                result[code] = bool(int(row.get("buildable") or 0))
            except (TypeError, ValueError):
                result[code] = False
    return result


def load_previous_lights(path: Path) -> Dict[str, Dict[str, str]]:
    """读取历史里每只股票最近一次的六灯状态（用于灯号转换提示）。"""
    if not path.exists():
        return {}
    light_cols = ["industry_light", "fundamental_light", "valuation_light",
                  "chips_light", "capital_light", "margin_light"]
    result: Dict[str, Dict[str, str]] = {}
    with open(path, "r", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            code = (row.get("code") or "").strip()
            if not code:
                continue
            result[code] = {col[:-6]: (row.get(col) or "").strip()
                            for col in light_cols if row.get(col)}
    return result
