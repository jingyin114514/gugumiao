# -*- coding: utf-8 -*-
"""数据抓取：akshare 优先，多种数据源自动降级。所有方法失败时返回 None。"""

import io
import threading
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from typing import Any, Dict, Optional

import pandas as pd

from .config import recent_quarters, with_market_symbol
from .indicators import to_num

try:
    import akshare as ak
except Exception:  # pragma: no cover
    ak = None


class FetchError(Exception):
    pass


class quiet_stderr:
    """会话级静默：把 sys.stderr 换成缓冲区，防止超时后仍在后台运行的
    akshare 线程把 tqdm 进度条写到终端。"""

    def __enter__(self):
        import sys
        self._orig = sys.stderr
        sys.stderr = io.StringIO()
        return self

    def __exit__(self, *exc):
        import sys
        sys.stderr = self._orig
        return False


class DataFetcher:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.timeout = float(cfg.get("data", {}).get("timeout_seconds", 25))
        self._ths_lock = threading.Lock()
        self._ths_cache: Optional[Dict[str, Optional[pd.DataFrame]]] = None
        self._em_flow_failed = False
        self._spot_cache: Optional[pd.DataFrame] = None
        self._spot_source = ""

    # ------------------------------------------------------------------ 通用
    def _run(self, fn, *args, label="", timeout=None, **kwargs):
        """带超时与静默的执行。返回 None 表示失败。"""
        result: Dict[str, Any] = {}
        wait = timeout or self.timeout

        def target():
            try:
                result["data"] = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(wait)
        if "data" not in result:
            if thread.is_alive():
                pass  # 超时：线程仍在跑，标记失败返回 None
            return None
        return result.get("data")

    # ------------------------------------------------------------------ 实时行情
    def fetch_spot(self, force: bool = False) -> Optional[pd.DataFrame]:
        """拉取全市场实时行情，返回标准化 DataFrame：code,name,price,pct_chg,volume,amount,mv_yi,source。"""
        if self._spot_cache is not None and not force:
            return self._spot_cache
        for label, fn, parser in (
            ("em", self._spot_em, self._parse_spot_em),
            ("tx", self._spot_tx, self._parse_spot_tx),
            ("sina", self._spot_sina, self._parse_spot_sina),
        ):
            raw = self._run(fn, label=f"spot_{label}")
            if raw is None or len(raw) == 0:
                continue
            parsed = parser(raw)
            if parsed is not None and len(parsed):
                self._spot_cache = parsed
                self._spot_source = label
                return parsed
        return None

    def fetch_quotes(self, codes) -> list:
        """快速拉取指定股票当天行情（走实时行情接口，秒级）。"""
        spot = self.fetch_spot(force=True)
        if spot is None or spot.empty:
            return []
        out = []
        for code in codes:
            row = spot[spot["code"] == str(code).zfill(6)]
            if row.empty:
                continue
            it = row.iloc[0]
            out.append({
                "code": str(code).zfill(6),
                "name": str(it.get("name") or ""),
                "price": to_num(it.get("price")),
                "pct_chg": to_num(it.get("pct_chg")),
                "volume": to_num(it.get("volume")),       # 手
                "amount": to_num(it.get("amount")),       # 元
                "mv_yi": to_num(it.get("mv_yi")),         # 亿元
                "float_mv_yi": to_num(it.get("float_mv_yi")),
                "turnover_rate": to_num(it.get("turnover_rate")),  # %
                "source": str(it.get("source") or ""),
            })
        return out

    def _spot_em(self):
        return ak.stock_zh_a_spot_em()

    def _spot_tx(self):
        return ak.stock_zh_a_spot_tx()

    def _spot_sina(self):
        return ak.stock_zh_a_spot()

    @staticmethod
    def _norm_code_6(value: Any) -> str:
        s = str(value).strip().lower()
        for prefix in ("sh", "sz", "bj"):
            if s.startswith(prefix):
                s = s[len(prefix):]
                break
        return s.zfill(6)[-6:]

    @staticmethod
    def _parse_spot_em(df: pd.DataFrame) -> Optional[pd.DataFrame]:
        need = {"代码", "名称", "最新价", "涨跌幅"}
        if not need.issubset(df.columns):
            return None
        out = pd.DataFrame({
            "code": df["代码"].astype(str).str.zfill(6),
            "name": df["名称"].astype(str),
            "price": pd.to_numeric(df["最新价"], errors="coerce"),
            "pct_chg": pd.to_numeric(df["涨跌幅"], errors="coerce"),
            "volume": pd.to_numeric(df.get("成交量"), errors="coerce") if "成交量" in df.columns else None,
            "amount": pd.to_numeric(df.get("成交额"), errors="coerce") if "成交额" in df.columns else None,
            "mv_yi": (pd.to_numeric(df.get("总市值"), errors="coerce") / 1e8) if "总市值" in df.columns else None,
            "float_mv_yi": (pd.to_numeric(df.get("流通市值"), errors="coerce") / 1e8) if "流通市值" in df.columns else None,
            "turnover_rate": pd.to_numeric(df.get("换手率"), errors="coerce") if "换手率" in df.columns else None,
            "source": "东财",
        })
        return out.dropna(subset=["code"])

    @staticmethod
    def _parse_spot_tx(df: pd.DataFrame) -> Optional[pd.DataFrame]:
        if not {"code", "zxj", "zdf"}.issubset(df.columns):
            return None
        out = pd.DataFrame({
            "code": df["code"].map(lambda c: DataFetcher._norm_code_6(c)),
            "name": df["name"].astype(str),
            "price": pd.to_numeric(df["zxj"], errors="coerce"),
            "pct_chg": pd.to_numeric(df["zdf"], errors="coerce"),
            "volume": pd.to_numeric(df.get("volume"), errors="coerce") if "volume" in df.columns else None,
            "amount": (pd.to_numeric(df.get("turnover"), errors="coerce") * 1e4) if "turnover" in df.columns else None,
            "mv_yi": pd.to_numeric(df.get("zsz"), errors="coerce") if "zsz" in df.columns else None,
            "float_mv_yi": pd.to_numeric(df.get("ltsz"), errors="coerce") if "ltsz" in df.columns else None,
            "turnover_rate": pd.to_numeric(df.get("hsl"), errors="coerce") if "hsl" in df.columns else None,
            "source": "腾讯",
        })
        return out.dropna(subset=["code"])

    @staticmethod
    def _parse_spot_sina(df: pd.DataFrame) -> Optional[pd.DataFrame]:
        need = {"代码", "名称", "最新价", "涨跌幅"}
        if not need.issubset(df.columns):
            return None
        out = pd.DataFrame({
            "code": df["代码"].map(lambda c: DataFetcher._norm_code_6(c)),
            "name": df["名称"].astype(str),
            "price": pd.to_numeric(df["最新价"], errors="coerce"),
            "pct_chg": pd.to_numeric(df["涨跌幅"], errors="coerce"),
            "volume": (pd.to_numeric(df.get("成交量"), errors="coerce") / 100) if "成交量" in df.columns else None,
            "amount": pd.to_numeric(df.get("成交额"), errors="coerce") if "成交额" in df.columns else None,
            "mv_yi": None,
            "float_mv_yi": None,
            "turnover_rate": None,
            "source": "新浪",
        })
        return out.dropna(subset=["code"])

    def spot_row(self, code: str) -> Optional[Dict[str, Any]]:
        spot = self.fetch_spot()
        if spot is None or spot.empty:
            return None
        row = spot[spot["code"] == code]
        if row.empty:
            return None
        item = row.iloc[0].to_dict()
        return item

    # ------------------------------------------------------------------ 日线
    def fetch_daily(self, code: str, market: str) -> Optional[pd.DataFrame]:
        """近一年日线，标准化为 date/high/low/close/volume/amount。"""
        start = (date.today() - timedelta(days=400)).strftime("%Y%m%d")
        end = date.today().strftime("%Y%m%d")
        for label, fn, parser in (
            ("em", lambda: ak.stock_zh_a_hist(symbol=code, period="daily",
                                              start_date=start, end_date=end, adjust=""),
             self._parse_hist_em),
            ("sina", lambda: ak.stock_zh_a_daily(symbol=f"{market}{code}", start_date=start, end_date=end),
             self._parse_hist_sina),
            ("tx", lambda: ak.stock_zh_a_hist_tx(symbol=f"{market}{code}", start_date=start, end_date=end),
             self._parse_hist_tx),
        ):
            raw = self._run(fn, label=f"hist_{label}")
            if raw is None or len(raw) == 0:
                continue
            parsed = parser(raw)
            if parsed is not None and len(parsed):
                return parsed
        return None

    @staticmethod
    def _parse_hist_em(df: pd.DataFrame) -> Optional[pd.DataFrame]:
        need = {"日期", "最高", "最低", "收盘"}
        if not need.issubset(df.columns):
            return None
        return pd.DataFrame({
            "date": pd.to_datetime(df["日期"]),
            "high": pd.to_numeric(df["最高"], errors="coerce"),
            "low": pd.to_numeric(df["最低"], errors="coerce"),
            "close": pd.to_numeric(df["收盘"], errors="coerce"),
            "volume": pd.to_numeric(df.get("成交量"), errors="coerce") if "成交量" in df.columns else None,
            "amount": pd.to_numeric(df.get("成交额"), errors="coerce") if "成交额" in df.columns else None,
        }).dropna(subset=["date", "close"])

    @staticmethod
    def _parse_hist_sina(df: pd.DataFrame) -> Optional[pd.DataFrame]:
        need = {"date", "high", "low", "close"}
        if not need.issubset(df.columns):
            return None
        out = pd.DataFrame({
            "date": pd.to_datetime(df["date"]),
            "high": pd.to_numeric(df["high"], errors="coerce"),
            "low": pd.to_numeric(df["low"], errors="coerce"),
            "close": pd.to_numeric(df["close"], errors="coerce"),
            "volume": (pd.to_numeric(df.get("volume"), errors="coerce") / 100) if "volume" in df.columns else None,
            "amount": pd.to_numeric(df.get("amount"), errors="coerce") if "amount" in df.columns else None,
        })
        return out.dropna(subset=["date", "close"])

    @staticmethod
    def _parse_hist_tx(df: pd.DataFrame) -> Optional[pd.DataFrame]:
        need = {"date", "high", "low", "close"}
        if not need.issubset(df.columns):
            return None
        return pd.DataFrame({
            "date": pd.to_datetime(df["date"]),
            "high": pd.to_numeric(df["high"], errors="coerce"),
            "low": pd.to_numeric(df["low"], errors="coerce"),
            "close": pd.to_numeric(df["close"], errors="coerce"),
            "volume": pd.to_numeric(df.get("volume"), errors="coerce") if "volume" in df.columns else None,
            "amount": pd.to_numeric(df.get("amount"), errors="coerce") if "amount" in df.columns else None,
        }).dropna(subset=["date", "close"])

    # ------------------------------------------------------------------ 估值
    def fetch_valuation(self, code: str) -> Optional[pd.DataFrame]:
        """估值历史（PE TTM/PB/总市值），标准化为 date/pe_ttm/pb/mv_yi/close。"""
        raw = self._run(lambda: ak.stock_value_em(symbol=code), label="value_em")
        parsed = self._parse_value_em(raw)
        if parsed is not None and len(parsed):
            return parsed
        # 备选：百度股市通，按指标各拉一次
        pe_df = self._run(lambda: ak.stock_zh_valuation_baidu(symbol=code, indicator="市盈率(TTM)", period="近三年"), label="value_baidu_pe")
        pb_df = self._run(lambda: ak.stock_zh_valuation_baidu(symbol=code, indicator="市净率", period="近三年"), label="value_baidu_pb")
        return self._parse_baidu_valuation(pe_df, pb_df)

    @staticmethod
    def _parse_value_em(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        if df is None or df.empty or "数据日期" not in df.columns:
            return None
        out = pd.DataFrame({
            "date": pd.to_datetime(df["数据日期"]),
            "pe_ttm": pd.to_numeric(df.get("PE(TTM)"), errors="coerce"),
            "pb": pd.to_numeric(df.get("市净率"), errors="coerce"),
            "mv_yi": pd.to_numeric(df.get("总市值"), errors="coerce") / 1e8,
            "close": pd.to_numeric(df.get("当日收盘价"), errors="coerce"),
        })
        return out.dropna(subset=["date"])

    @staticmethod
    def _parse_baidu_valuation(pe_df: Optional[pd.DataFrame], pb_df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        frames = []
        for raw, key in ((pe_df, "pe_ttm"), (pb_df, "pb")):
            if raw is None or raw.empty:
                continue
            date_col = next((c for c in raw.columns if "日期" in c), None)
            val_col = next((c for c in raw.columns if c in ("值", "数值", "市盈率(TTM)", "市净率")), None)
            if not date_col or not val_col:
                continue
            tmp = pd.DataFrame({
                "date": pd.to_datetime(raw[date_col]),
                key: pd.to_numeric(raw[val_col], errors="coerce"),
            })
            frames.append(tmp)
        if not frames:
            return None
        merged = frames[0]
        for extra in frames[1:]:
            merged = merged.merge(extra, on="date", how="outer")
        merged = merged.sort_values("date")
        if "mv_yi" not in merged.columns:
            merged["mv_yi"] = None
        if "close" not in merged.columns:
            merged["close"] = None
        return merged

    # ------------------------------------------------------------------ 主力资金
    def fetch_fund_flow(self, code: str, market: str) -> Dict[str, Any]:
        """返回 {flow_1d_wan, flow_5d_wan, flow_20d_wan, source}，失败字段为 None。"""
        prefer = self.cfg.get("data", {}).get("preferred_fund_flow", "auto")
        result: Dict[str, Any] = {"flow_1d_wan": None, "flow_5d_wan": None, "flow_20d_wan": None, "source": ""}
        if prefer in ("auto", "em") and not self._em_flow_failed:
            daily = self._run(lambda: ak.stock_individual_fund_flow(stock=code, market=market),
                              label="fund_flow_em")
            if daily is not None and len(daily):
                from .indicators import summarize_em_fund_flow
                stats = summarize_em_fund_flow(daily)
                if stats:
                    result.update(stats)
                    result["source"] = "东财"
                    return result
            self._em_flow_failed = True
        if prefer in ("auto", "ths"):
            tables = self.fetch_ths_rank_tables()
            if tables:
                from .indicators import summarize_ths_rank
                stats = summarize_ths_rank(tables, code)
                if any(v is not None for v in stats.values()):
                    result.update({k: v for k, v in stats.items()})
                    result["source"] = "同花顺"
        return result

    def fetch_ths_rank_tables(self) -> Optional[Dict[str, Optional[pd.DataFrame]]]:
        """同花顺资金流排行（即时/5日/20日），全市场一次拉取并缓存。"""
        if self._ths_cache is not None:
            return self._ths_cache
        with self._ths_lock:
            if self._ths_cache is not None:
                return self._ths_cache
            tables: Dict[str, Optional[pd.DataFrame]] = {}
            for period in ("即时", "5日排行", "20日排行"):
                raw = self._run(lambda p=period: ak.stock_fund_flow_individual(symbol=p),
                                label=f"ths_{period}", timeout=150)
                tables[period] = raw if (raw is not None and len(raw)) else None
            self._ths_cache = tables
            return tables

    # ------------------------------------------------------------------ 机构持仓
    def fetch_institution(self, code: str) -> Optional[pd.DataFrame]:
        """新浪机构持股明细：从最新报告期往前找有数据的季度。"""
        for quarter in recent_quarters(date.today()):
            raw = self._run(lambda q=quarter: ak.stock_institute_hold_detail(stock=code, quarter=q),
                            label=f"inst_{quarter}")
            if raw is not None and len(raw):
                return raw
        return None

    # ------------------------------------------------------------------ 基本面
    def fetch_fundamentals(self, code: str, market: str) -> Optional[pd.DataFrame]:
        # 按单季度：营收/净利同比为单季同比，便于做"增速放缓"的趋势判断
        raw = self._run(
            lambda: ak.stock_financial_analysis_indicator_em(symbol=with_market_symbol(code, market),
                                                             indicator="按单季度"),
            label="fin_ind_q")
        if raw is None or len(raw) == 0:
            raw = self._run(
                lambda: ak.stock_financial_analysis_indicator_em(symbol=with_market_symbol(code, market),
                                                                 indicator="按报告期"),
                label="fin_ind")
        if raw is not None and len(raw):
            return raw
        return self._fetch_yjbb(code)

    def _fetch_yjbb(self, code: str) -> Optional[pd.DataFrame]:
        """备选：东财业绩报表（全市场表，按报告期过滤个股）。"""
        today = date.today()
        candidates = [
            f"{today.year}{m:02d}31" for m in (3, 6, 9, 12)
        ] + [
            f"{today.year - 1}{m:02d}31" for m in (12, 9, 6, 3)
        ]
        seen = set()
        frames = []
        for date_str in candidates:
            if date_str in seen:
                continue
            seen.add(date_str)
            raw = self._run(lambda d=date_str: ak.stock_yjbb_em(date=d), label=f"yjbb_{date_str}")
            if raw is None or raw.empty:
                continue
            row = raw[raw["股票代码"].astype(str).str.zfill(6) == code]
            if row.empty:
                continue
            row = row.copy()
            row["REPORT_DATE"] = pd.to_datetime(date_str)
            frames.append(row)
            if len(frames) >= 2:
                break
        if not frames:
            return None
        merged = pd.concat(frames, ignore_index=True)
        renamed = merged.rename(columns={
            "营业收入-同比增长": "TOTALOPERATEREVETZ",
            "净利润-同比增长": "PARENTNETPROFITTZ",
            "销售毛利率": "GROSS_PROFIT_RATIO",
            "净利润-净利润": "PARENTNETPROFIT",
        })
        return renamed
