# -*- coding: utf-8 -*-
"""监控流水线：抓数 → 指标 → 灯号 → 历史/报告。CLI 与本地面板共用。"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import find_tier, normalize_code
from .fetcher import DataFetcher, quiet_stderr
from .indicators import (
    compute_52w,
    summarize_fundamentals,
    summarize_institution,
    summarize_valuation,
    to_num,
)
from .lights import evaluate
from .models import StockSnapshot
from .reporter import append_history, load_previous_buildable, render_markdown


def _fmt_flow(v: Optional[float]) -> str:
    if v is None:
        return "数据缺失"
    direction = "流入" if v >= 0 else "流出"
    s = abs(v)
    if s >= 10000:
        return f"{direction} {s / 10000:.2f}亿"
    return f"{direction} {s:.0f}万"


def _build_analysis_text(stock: StockSnapshot) -> str:
    """按已抓取数据自动生成客观金融说明（研报式口吻，不预测、不构成投资建议）。"""
    parts = []
    # 基本面
    if stock.rev_yoy is not None or stock.profit_yoy is not None:
        fb = []
        if stock.rev_yoy is not None:
            fb.append(f"营收同比 {stock.rev_yoy:+.1f}%")
        if stock.profit_yoy is not None:
            fb.append(f"归母净利同比 {stock.profit_yoy:+.1f}%")
        if stock.gross_margin is not None:
            fb.append(f"毛利率 {stock.gross_margin:.1f}%")
        if stock.prev_rev_yoy is not None:
            fb.append(f"上期营收同比 {stock.prev_rev_yoy:+.1f}%")
        parts.append("基本面：" + "，".join(fb))
    # 估值
    if stock.pe_ttm is not None or stock.pb is not None or stock.pe_pct is not None or stock.pb_pct is not None:
        va = []
        if stock.pe_ttm is not None:
            va.append(f"PE(TTM) {stock.pe_ttm:.1f}倍")
        if stock.pb is not None:
            va.append(f"PB {stock.pb:.2f}倍")
        if stock.pe_pct is not None:
            va.append(f"PE 3年分位 {stock.pe_pct:.0f}%")
        if stock.pb_pct is not None:
            va.append(f"PB 3年分位 {stock.pb_pct:.0f}%")
        parts.append("估值：" + "，".join(va))
    # 主力资金
    if stock.flow_5d_wan is not None or stock.flow_20d_wan is not None:
        fl = []
        if stock.flow_5d_wan is not None:
            fl.append(f"近5日主力净{_fmt_flow(stock.flow_5d_wan)}")
        if stock.flow_20d_wan is not None:
            fl.append(f"近20日主力净{_fmt_flow(stock.flow_20d_wan)}")
        if stock.threshold_wan:
            fl.append(f"档位阈值 {stock.threshold_wan / 10000:.0f}亿")
        parts.append("资金：" + "，".join(fl))
    # 机构持仓
    if stock.inst_count is not None:
        inst = [f"机构持仓 {stock.inst_count} 家"]
        if stock.inst_count_chg is not None:
            inst.append(f"家数环比 {stock.inst_count_chg:+d}")
        if stock.inst_ratio_pct is not None:
            inst.append(f"占流通股 {stock.inst_ratio_pct:.2f}%")
        if stock.inst_ratio_chg is not None:
            inst.append(f"比例环比 {stock.inst_ratio_chg:+.2f}pp")
        parts.append("筹码：" + "，".join(inst))
    # 产业 / 边际备注
    if stock.industry_note:
        parts.append(f"产业逻辑：{stock.industry_note}")
    if stock.margin_note:
        parts.append(f"边际变化：{stock.margin_note}")
    # 灯号结论
    status = stock.status or ("可建仓" if stock.buildable else "暂不可建仓")
    parts.append(f"六灯 {stock.green_count}绿{stock.red_count}红，综合判定：{status}")
    return "；".join(parts)


def process_stock(item: Dict[str, Any], cfg: Dict[str, Any], fetcher: DataFetcher,
                  spot: Optional[Any], prev_buildable: Dict[str, bool]) -> StockSnapshot:
    code = item["code"]
    market = item["market"]
    stock = StockSnapshot(
        code=code,
        market=market,
        name=item.get("name") or "",
        cost=item.get("cost"),
        target_weight=item.get("target_weight"),
        industry_light=item.get("industry_light") or "yellow",
        industry_note=item.get("industry_note") or "",
        margin_light=item.get("margin_light"),
        margin_note=item.get("margin_note") or "",
    )

    # ---- 实时行情 ----
    spot_row = None
    if spot is not None and not spot.empty:
        matched = spot[spot["code"] == code]
        if not matched.empty:
            spot_row = matched.iloc[0].to_dict()
    if spot_row is not None:
        stock.price = to_num(spot_row.get("price")) or stock.price
        stock.pct_chg = to_num(spot_row.get("pct_chg"))
        stock.volume = to_num(spot_row.get("volume"))
        stock.amount = to_num(spot_row.get("amount"))
        stock.mv_yi = to_num(spot_row.get("mv_yi"))
        stock.mv_source = str(spot_row.get("source") or "")
        if spot_row.get("name") and not stock.name:
            stock.name = str(spot_row["name"])
        if "quote_time" in spot_row:
            stock.quote_time = str(spot_row["quote_time"] or "")

    # ---- 日线（52 周区间）----
    daily = fetcher.fetch_daily(code, market)
    if daily is None or daily.empty:
        stock.warnings.append("日线数据缺失，52 周区间不可用")
    else:
        stats = compute_52w(daily, stock.price)
        for key, value in stats.items():
            setattr(stock, key, value)
        if stock.price is None:
            stock.price = to_num(daily["close"].iloc[-1])
        if stock.amount is None and "amount" in daily.columns:
            stock.amount = to_num(daily["amount"].iloc[-1])

    # ---- 估值 ----
    val_df = fetcher.fetch_valuation(code)
    if val_df is None or val_df.empty:
        stock.warnings.append("估值历史缺失，PE/PB 分位不可用")
    else:
        val = summarize_valuation(val_df, cfg.get("valuation", {}).get("percentile_years", 3))
        for key in ("pe_ttm", "pb", "pe_pct", "pb_pct", "mv_yi", "val_date", "val_points"):
            setattr(stock, key, val.get(key))
        if stock.price is None:
            stock.price = val.get("close")

    # ---- 市值分档与主力阈值 ----
    tier = find_tier(cfg, stock.mv_yi)
    if tier:
        stock.threshold_wan = float(tier.get("threshold_wan", 5000))
        stock.tier_name = str(tier.get("name", ""))
    else:
        stock.warnings.append("市值缺失，主力阈值按中盘 5000 万计")
        stock.threshold_wan = 5000.0

    # ---- 主力资金 ----
    flow = fetcher.fetch_fund_flow(code, market)
    stock.flow_1d_wan = flow.get("flow_1d_wan")
    stock.flow_5d_wan = flow.get("flow_5d_wan")
    stock.flow_20d_wan = flow.get("flow_20d_wan")
    stock.flow_source = flow.get("source") or ""
    if not stock.flow_source:
        stock.warnings.append("主力资金数据缺失")

    # ---- 机构持仓 ----
    inst_df = fetcher.fetch_institution(code)
    if inst_df is None or inst_df.empty:
        stock.warnings.append("机构持仓数据缺失")
    else:
        inst = summarize_institution(inst_df)
        stock.inst_count = inst.get("inst_count")
        stock.inst_count_prev = inst.get("inst_count_prev")
        stock.inst_ratio_pct = inst.get("inst_ratio_pct")
        stock.inst_ratio_prev_pct = inst.get("inst_ratio_prev_pct")
        if inst.get("inst_count") is not None:
            stock.inst_date = "最新报告期"

    # ---- 基本面 ----
    fin_df = fetcher.fetch_fundamentals(code, market)
    if fin_df is None or fin_df.empty:
        stock.warnings.append("财务数据缺失，基本面灯暂按黄灯")
    else:
        fin = summarize_fundamentals(fin_df)
        for key in ("rev_yoy", "profit_yoy", "net_profit", "gross_margin",
                    "prev_rev_yoy", "prev_profit_yoy", "prev_gross_margin", "report_date"):
            setattr(stock, key, fin.get(key))

    # ---- 灯号 ----
    evaluate(stock, cfg)
    stock.analysis_text = _build_analysis_text(stock)

    # ---- 状态变化提醒 ----
    was_buildable = prev_buildable.get(code)
    if was_buildable is False and stock.buildable:
        stock.alert = "新进入可建仓清单"
    return stock


def run_pipeline(cfg: Dict[str, Any], codes: Optional[str] = None,
                 max_workers: Optional[int] = None,
                 save_history: bool = True, save_report: bool = True,
                 progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """执行一次完整监控。返回 {stocks, meta, report_path, spot_ok, error}。"""
    def log(text: str) -> None:
        if progress:
            progress(text)

    watchlist = cfg.get("watchlist", [])
    if codes:
        wanted = {normalize_code(c)[0] for c in codes.split(",") if c.strip()}
        watchlist = [w for w in watchlist if w.get("code") in wanted]
    if not watchlist:
        return {"stocks": [], "meta": {}, "report_path": None, "spot_ok": False,
                "error": "自选股为空，请先编辑 config.json 的 watchlist"}

    history_path = Path(cfg["data"]["history_file"])
    reports_dir = Path(cfg["data"]["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    prev_buildable = {} if not save_history else load_previous_buildable(history_path)

    fetcher = DataFetcher(cfg)
    log("正在拉取实时行情 …")
    spot = None
    stocks: List[StockSnapshot] = []
    with quiet_stderr():
        spot = fetcher.fetch_spot()
        if spot is None:
            log("提示: 实时行情源均不可用，将退回日线收盘价。")
        workers = max_workers or int(cfg["data"].get("max_workers", 4))
        log(f"开始抓取 {len(watchlist)} 只自选股数据 …")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(process_stock, item, cfg, fetcher, spot, prev_buildable): item
                for item in watchlist
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    stock = future.result()
                    stocks.append(stock)
                    log(f"✓ {stock.name or item.get('name')} {stock.code} 完成"
                        + (f"（{len(stock.warnings)} 条数据提示）" if stock.warnings else ""))
                except Exception as exc:  # noqa: BLE001
                    log(f"✗ {item.get('code')} 处理失败: {exc}")
                    failed = StockSnapshot(code=item["code"], market=item["market"],
                                           name=item.get("name") or "")
                    failed.warnings.append(f"数据抓取失败: {exc}")
                    evaluate(failed, cfg)
                    stocks.append(failed)
    stocks.sort(key=lambda s: s.code)

    run_time = datetime.now()
    sources = set()
    for s in stocks:
        if s.mv_source:
            sources.add(s.mv_source)
        if s.flow_source:
            sources.add(s.flow_source)
    meta = {
        "run_time": run_time,
        "data_sources": " / ".join(sorted(sources)) if sources else "akshare",
    }

    report_path: Optional[Path] = None
    if save_report:
        report_path = reports_dir / f"report_{run_time:%Y%m%d_%H%M}.md"
        report_path.write_text(render_markdown(stocks, cfg, meta), encoding="utf-8")
        meta["report_path"] = str(report_path)
    if save_history:
        append_history(history_path, stocks, run_time)

    return {"stocks": stocks, "meta": meta, "report_path": report_path,
            "spot_ok": spot is not None, "error": ""}
