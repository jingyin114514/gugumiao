# -*- coding: utf-8 -*-
"""灯号监控面板（本地面板版）

双击 启动面板.bat 即可：自动启动本地服务并在浏览器打开可视化界面。
数据仍全部本地处理、实时联网抓取，不上传任何内容。
"""

import json
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

from lightmon.config import load_config
from lightmon.config import normalize_code
from lightmon.fetcher import DataFetcher, quiet_stderr
from lightmon.models import stock_to_dict
from lightmon.pipeline import run_pipeline

HOST = "127.0.0.1"
PORT = 8765

STATE: Dict[str, Any] = {
    "running": False,
    "pending": False,
    "done": False,
    "error": "",
    "stocks": [],
    "meta": {},
    "report_path": "",
    "log": [],
}
LOCK = threading.Lock()
LAST_STATE_PATH = Path("data/last_state.json")


def _restore_last_state() -> None:
    """服务启动时恢复上次成功结果，保证打开界面即显示上次数据。"""
    try:
        if LAST_STATE_PATH.exists():
            data = json.loads(LAST_STATE_PATH.read_text(encoding="utf-8"))
            with LOCK:
                STATE["stocks"] = data.get("stocks", [])
                STATE["meta"] = data.get("meta", {})
                STATE["report_path"] = data.get("report_path", "")
                STATE["done"] = True
    except Exception:
        pass


def _save_last_state() -> None:
    """把最近一次成功结果保存到本地，供下次启动恢复。"""
    try:
        LAST_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOCK:
            payload = {k: STATE[k] for k in ("stocks", "meta", "report_path")}
        LAST_STATE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass


_restore_last_state()


def _log(line: str) -> None:
    with LOCK:
        STATE["log"] = (STATE["log"] + [line])[-30:]


def _start_refresh() -> bool:
    """统一入口：仅允许一条分析线程运行；运行中再来请求则排队，完成后自动再跑一轮。"""
    with LOCK:
        if STATE["running"] or STATE["pending"]:
            STATE["pending"] = True
            return False
        # 保留上次成功数据（stocks/meta/report_path），抓取期间界面继续显示上次结果
        STATE.update(running=True, pending=False, done=False, error="", log=[])
    cfg = load_config("config.json")
    thread = threading.Thread(target=refresh_background, args=(cfg,), daemon=True)
    thread.start()
    return True


def refresh_background(initial_cfg: Dict[str, Any]) -> None:
    """后台跑灯号分析；期间若有新的刷新请求（如刚添加了股票），自动排队再跑一轮。"""
    cfg = initial_cfg
    while True:
        _log("开始抓取数据 …")
        try:
            result = run_pipeline(cfg, save_history=True, save_report=True, progress=_log)
            with LOCK:
                STATE["error"] = result.get("error", "")
                STATE["stocks"] = [stock_to_dict(s) for s in result.get("stocks", [])]
                STATE["meta"] = result.get("meta", {})
                STATE["report_path"] = str(result.get("report_path") or "")
                STATE["done"] = True
            _save_last_state()
            _log("数据抓取完成。")
        except Exception as exc:  # noqa: BLE001
            with LOCK:
                STATE["error"] = str(exc)
                STATE["done"] = True
            _log(f"出错: {exc}")
        with LOCK:
            if not STATE["pending"]:
                STATE["running"] = False
                return
            # 有排队请求：重新加载配置（包含刚添加的股票）再跑一轮
            STATE["pending"] = False
            STATE["done"] = False
            cfg = load_config("config.json")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默访问日志
        pass

    def _json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    @staticmethod
    def _parse_float(value: Any) -> Any:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            with LOCK:
                payload = {k: STATE[k] for k in ("running", "pending", "done", "error", "stocks", "meta", "report_path", "log")}
                rt = STATE["meta"].get("run_time", "")
                payload["updated"] = rt.strftime("%Y-%m-%d %H:%M:%S") \
                    if hasattr(rt, "strftime") else str(rt or "")
                payload["data_sources"] = STATE["meta"].get("data_sources", "")
            self._json(payload)
        elif path == "/report":
            report_path = STATE.get("report_path") or self._latest_report()
            if report_path and Path(report_path).exists():
                body = Path(report_path).read_bytes()
                query = parse_qs(urlparse(self.path).query)
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                if query.get("dl"):
                    self.send_header("Content-Disposition", f'attachment; filename="{Path(report_path).name}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "暂无报告，请先刷新一次"}, status=404)
        elif path == "/api/watchlist":
            cfg = load_config("config.json")
            self._json({"watchlist": cfg.get("watchlist", [])})
        elif path == "/api/quote":
            cfg = load_config("config.json")
            codes = [str(w.get("code", "")).zfill(6) for w in cfg.get("watchlist", [])]
            fetcher = DataFetcher(cfg)
            with quiet_stderr():
                quotes = fetcher.fetch_quotes(codes)
            self._json({
                "quotes": quotes,
                "time": datetime.now().strftime("%H:%M:%S"),
                "source": quotes[0]["source"] if quotes else "",
                "ok": bool(quotes),
            })
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self._json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/refresh":
            started = _start_refresh()
            self._json({"started": started, "queued": not started})
        elif path == "/api/watchlist/add":
            self._json(self._add_watchlist(self._read_json_body()))
        elif path == "/api/watchlist/remove":
            self._json(self._remove_watchlist(self._read_json_body()))
        else:
            self._json({"error": "not found"}, status=404)

    def _add_watchlist(self, body: Dict[str, Any]) -> Dict[str, Any]:
        cfg_path = Path("config.json")
        if not cfg_path.exists():
            return {"error": "缺少 config.json"}
        code, market = normalize_code(str(body.get("code") or ""))
        if len(code) != 6 or not code.isdigit():
            return {"error": "股票代码格式不正确，应为 6 位数字，如 600519"}
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        watchlist = raw.setdefault("watchlist", [])
        if any(str(w.get("code", "")).zfill(6) == code for w in watchlist):
            return {"error": f"{code} 已在自选股中"}
        cost = self._parse_float(body.get("cost"))
        weight = self._parse_float(body.get("target_weight"))
        item = {
            "code": code,
            "market": market,
            "name": str(body.get("name") or "").strip(),
            "cost": cost,
            "target_weight": weight,
            "industry_light": "yellow",
            "industry_note": "",
            "margin_note": "",
        }
        watchlist.append(item)
        cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "watchlist": watchlist}

    def _remove_watchlist(self, body: Dict[str, Any]) -> Dict[str, Any]:
        cfg_path = Path("config.json")
        if not cfg_path.exists():
            return {"error": "缺少 config.json"}
        code, _ = normalize_code(str(body.get("code") or ""))
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        watchlist = raw.get("watchlist", [])
        keep = [w for w in watchlist if str(w.get("code", "")).zfill(6) != code]
        if len(keep) == len(watchlist):
            return {"error": f"{code} 不在自选股中"}
        raw["watchlist"] = keep
        cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "watchlist": keep}

    def _latest_report(self) -> str:
        cfg = load_config("config.json")
        reports_dir = Path(cfg["data"]["reports_dir"])
        if reports_dir.exists():
            files = sorted(reports_dir.glob("report_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            if files:
                return str(files[0])
        return ""


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>灯号监控面板</title>
<style>
:root{
  --ink:#1F2937; --muted:#6B7280; --line:#E9E2D0; --bg:#FAF5E6;
  --green:#16A34A; --green-bg:#ECFDF5; --green-deep:#15803D;
  --amber:#D97706; --amber-bg:#FFFBEB; --amber-deep:#B45309;
  --red:#DC2626; --red-bg:#FEF2F2; --red-deep:#B91C1C;
  --card:#FFFFFF;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink);line-height:1.6;font-size:14px}
header{position:sticky;top:0;z-index:10;background:rgba(255,255,255,.92);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.head-inner{max-width:1080px;margin:0 auto;padding:12px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px;font-size:17px;font-weight:650;letter-spacing:.2px}
.brand .logo{display:inline-flex;gap:3px}
.brand .logo i{width:9px;height:9px;border-radius:50%;display:inline-block}
.brand .logo i.g{background:var(--green)} .brand .logo i.y{background:var(--amber)} .brand .logo i.r{background:var(--red)}
.brand .sub{color:var(--muted);font-weight:400;font-size:13px}
.actions{display:flex;gap:8px}
button{font:inherit;border:1px solid transparent;border-radius:8px;padding:8px 16px;cursor:pointer;transition:background .15s,transform .05s}
button:active{transform:translateY(1px)}
button.primary{background:var(--ink);color:#fff;font-weight:600}
button.primary:hover{background:#111827}
button.primary:disabled{opacity:.55;cursor:wait}
button.ghost{background:#fff;border-color:var(--line);color:var(--ink)}
button.ghost:hover{background:#F3F4F6}
main{max-width:1080px;margin:0 auto;padding:20px}
.meta-line{color:var(--muted);font-size:13px;margin-bottom:14px;display:flex;gap:14px;flex-wrap:wrap}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.stat .num{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.2}
.stat .lbl{color:var(--muted);font-size:12px;margin-top:2px}
.stat .num.green{color:var(--green-deep)} .stat .num.amber{color:var(--amber-deep)} .stat .num.red{color:var(--red-deep)}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-bottom:16px}
.panel h2{font-size:14px;font-weight:650;margin-bottom:12px;color:var(--ink);letter-spacing:.2px}
.panel h2 .hint{color:var(--muted);font-weight:400;font-size:12px;margin-left:8px}
.tbl-wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:9px 10px;text-align:center;white-space:nowrap}
th{font-size:12px;color:var(--muted);font-weight:500;background:#F6F1E0}
td.name-cell,th.name-cell{text-align:left}
td .nm{font-weight:650}
td .cd{color:var(--muted);font-size:12px}
.light-cell{font-size:16px;line-height:1}
.up{color:var(--red-deep)} .down{color:var(--green-deep)} /* A股习惯：红涨绿跌 */
.flat{color:var(--muted)}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600;background:#F3F4F6;color:#4B5563}
.badge.ok{background:var(--green-bg);color:var(--green-deep)}
.badge.warn{background:var(--amber-bg);color:var(--amber-deep)}
.badge.bad{background:var(--red-bg);color:var(--red-deep)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.card{border:1px solid var(--line);border-radius:10px;background:var(--card);padding:10px 12px;cursor:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='26' height='26' viewBox='0 0 26 26'%3E%3Cpath d='M4 24 L15 9 L19 13 L6 26 Z' fill='%23181818'/%3E%3Cpath d='M15 9 Q18 4 23 3 Q24 8 19 13 Z' fill='%23282828'/%3E%3C/svg%3E") 5 12,pointer;transition:box-shadow .15s,transform .15s,border-color .15s}
.card:hover{border-color:#9CA3AF;box-shadow:0 4px 14px rgba(0,0,0,.07);transform:translateY(-1px)}
.card .c-top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.card .who{font-size:13.5px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:"STKaiti","KaiTi","Microsoft YaHei",serif}
.card .who .cd{color:var(--muted);font-weight:400;font-size:11px;margin-left:5px}
.card .price{font-size:15px;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap}
.card .c-lights{font-size:14px;letter-spacing:1px;margin:5px 0 6px;line-height:1}
.card .c-meta{display:flex;justify-content:space-between;align-items:center;font-size:11.5px;color:var(--muted)}
.card .c-meta .badge{font-size:10.5px;padding:1px 8px}
.card-head .who{font-size:15px;font-weight:650}
.card-head .who .cd{color:var(--muted);font-weight:400;font-size:12px;margin-left:6px}
.card-body{padding:6px 16px 12px}
.row{display:flex;justify-content:space-between;gap:14px;padding:6px 0;border-bottom:1px dashed #F0F1F3;font-size:13px}
.row:last-child{border-bottom:none}
.row .lbl{color:var(--muted);flex-shrink:0}
.row .val{text-align:right;font-variant-numeric:tabular-nums;word-break:break-all}
.lights-lg{font-size:17px;letter-spacing:2px}
.range{position:relative;height:5px;border-radius:3px;background:#E8EBEE;margin:8px 0 3px}
.range .fill{position:absolute;left:0;top:0;bottom:0;border-radius:3px;background:#D1D5DB}
.range .dot{position:absolute;top:50%;width:9px;height:9px;border-radius:50%;background:var(--ink);transform:translate(-50%,-50%);border:2px solid #fff;box-shadow:0 0 0 1px var(--line)}
.range-meta{display:flex;justify-content:space-between;color:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.note{font-size:12px;color:var(--muted);background:#F9FAFB;border-radius:8px;padding:8px 10px;margin-top:8px}
.warn-line{color:var(--amber-deep);font-size:12px;padding:4px 0}
.add-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-bottom:12px}
.add-form input{border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit;color:var(--ink);background:#fff;min-width:0}
.add-form input:focus{outline:none;border-color:#9CA3AF;box-shadow:0 0 0 2px #E5E7EB}
.add-form button{white-space:nowrap}
#wlMsg{color:var(--red-deep);font-size:12px;margin-bottom:8px;min-height:0}
.wl-item{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;margin-bottom:6px;background:#FAFAFB;font-size:13px}
.wl-item .info{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.wl-item .code{font-weight:650;font-variant-numeric:tabular-nums}
.wl-item .meta{color:var(--muted);font-size:12px}
.wl-item .del{background:none;border:none;color:var(--red);cursor:pointer;font-size:15px;padding:2px 7px;border-radius:6px}
.wl-item .del:hover{background:var(--red-bg)}
.banner{background:var(--red-bg);color:var(--red-deep);border:1px solid #FECACA;border-radius:10px;padding:12px 16px;margin-bottom:16px}
footer{max-width:1080px;margin:0 auto;padding:22px 20px 34px;color:var(--muted);font-size:12px;text-align:center}
.footer-quote{color:var(--amber-deep);font-weight:600;font-size:13px;margin-bottom:6px;font-family:"STKaiti","KaiTi","Microsoft YaHei",serif}
.footer-note{color:var(--muted);font-size:11px;opacity:.8}
.hidden{display:none}
.banner-info{background:var(--ink);color:#F9FAFB;border-radius:10px;padding:10px 44px 10px 16px;margin-bottom:16px;display:flex;align-items:center;gap:10px;font-size:13px;position:relative}
.banner-info .spinner-sm{width:14px;height:14px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0}
.banner-info .dim{color:#9CA3AF}
.deep-cancel{position:absolute;top:50%;right:10px;transform:translateY(-50%);width:24px;height:24px;border-radius:50%;border:none;background:rgba(255,255,255,.16);color:#D1D5DB;cursor:pointer;font-size:13px;line-height:1;padding:0;display:flex;align-items:center;justify-content:center;transition:background .15s}
.deep-cancel:hover{background:rgba(255,255,255,.3);color:#fff}
#quoteStatus{margin-top:8px}
@keyframes spin{to{transform:rotate(360deg)}}
@media (max-width:640px){.cards{grid-template-columns:1fr}}
.modal-overlay{position:fixed;inset:0;z-index:1000;background:rgba(17,24,39,.45);display:flex;align-items:center;justify-content:center;padding:20px}
.modal-overlay.hidden{display:none}
.modal{position:relative;background:#fff;border:1px solid var(--line);border-radius:16px;box-shadow:0 24px 60px rgba(0,0,0,.22);width:min(620px,94vw);max-height:86vh;overflow:auto;padding:20px 22px}
.m-head{display:flex;align-items:center;gap:10px;margin-bottom:10px;padding-right:40px}
.m-name{font-size:18px;font-weight:700}
.m-code{color:var(--muted);font-size:12px}
.m-close{position:absolute;top:14px;right:14px;width:30px;height:30px;border-radius:50%;border:none;background:#F3F4F6;color:#6B7280;font-size:15px;line-height:1;padding:0;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s}
.m-close:hover{background:#E5E7EB;color:#111827}
.m-lights{font-size:20px;letter-spacing:3px;background:#F9FAFB;border-radius:10px;padding:8px 12px;margin-bottom:12px}
.m-body .row{font-size:13px}
.view-tabs{display:inline-flex;gap:4px;background:#EEF0F3;border-radius:999px;padding:3px;margin-bottom:12px}
.vt{border:none;background:transparent;border-radius:999px;padding:6px 16px;font:inherit;font-size:13px;color:var(--muted);cursor:pointer;transition:all .15s}
.vt:hover{color:var(--ink)}
.vt.on{background:#fff;color:var(--ink);box-shadow:0 1px 4px rgba(0,0,0,.1);font-weight:600}
.fan-view .legend{display:flex;gap:16px;font-size:12px;color:var(--muted)}
.fan-view .legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:-1px}
.fan-view .search-row{display:flex;gap:10px;align-items:center;margin:12px 0 4px}
.fan-view .search-row input{flex:1;max-width:300px;background:#fff;border:1px solid var(--line);border-radius:999px;padding:7px 16px;color:var(--ink);font:inherit;outline:none}
.fan-view .search-row input:focus{border-color:var(--amber)}
.fan-view .search-row .count{color:var(--muted);font-size:12px}
.stage{position:relative;width:100%;max-width:660px;height:440px;margin:8px auto 0;background:radial-gradient(120% 90% at 50% 105%,#FBF6E9 0%,#F4ECD6 55%,#EADFC2 100%);border-radius:22px;border:1px solid rgba(184,134,11,.28);box-shadow:0 18px 45px rgba(120,90,30,.12),inset 0 1px 0 rgba(255,255,255,.6);overflow:hidden;user-select:none}
.stage::after{content:'';position:absolute;inset:0;pointer-events:none;background:radial-gradient(60% 45% at 50% 78%,rgba(255,255,255,.35),transparent 70%)}
.stage svg{position:absolute;inset:0;width:100%;height:100%}
.names{position:absolute;inset:0}
.nm{position:absolute;writing-mode:vertical-rl;text-orientation:upright;font-family:"STKaiti","KaiTi","Microsoft YaHei",serif;font-size:14px;font-weight:600;letter-spacing:2px;padding:3px;border-radius:6px;line-height:1.25;cursor:pointer;transition:transform .18s ease,box-shadow .18s ease;transform-origin:center}
.nm.g{color:var(--green-deep)} .nm.y{color:var(--amber-deep)} .nm.r{color:var(--red-deep)}
.nm:hover{transform:scale(1.12);z-index:5;background:rgba(255,255,255,.75);box-shadow:0 0 0 1px rgba(184,134,11,.25),0 8px 22px rgba(120,90,30,.16)}
.pages{display:flex;justify-content:center;gap:8px;margin:14px 0 4px;flex-wrap:wrap}
.pg{border:1px solid var(--line);background:#fff;color:var(--muted);border-radius:999px;padding:6px 16px;font:inherit;font-size:13px;cursor:pointer;transition:all .18s ease}
.pg:hover{background:#F3EAD2;border-color:var(--amber);transform:translateY(-1px)}
.pg.on{background:linear-gradient(135deg,#C9A24B,#A67C2E);color:#fff;border-color:transparent;box-shadow:0 6px 18px rgba(166,124,46,.3)}
.page-note{width:100%;text-align:center;font-size:12px;color:var(--muted);margin-top:6px}
.c-chg{font-size:10.5px;color:var(--amber-deep);margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
</style>
</head>
<body>
<header>
  <div class="head-inner">
    <div class="brand"><span class="logo"><i class="g"></i><i class="y"></i><i class="r"></i></span>
      灯号监控 <span class="sub">自选股面板</span></div>
    <div class="actions">
      <button class="ghost" onclick="window.open('/report')">查看报告</button>
      <button id="deepBtn" class="ghost" onclick="deepRefresh()">灯号分析</button>
      <button id="refreshBtn" class="primary" onclick="loadQuotes()">刷新行情</button>
    </div>
  </div>
</header>
<main>
  <div id="bannerWrap"></div>
  <div id="deepBanner" class="banner-info hidden">
    <span class="spinner-sm"></span>
    <span id="deepText">正在抓取灯号分析数据（约1~2分钟）…</span>
    <button class="deep-cancel" onclick="cancelDeep()" title="取消等待">×</button>
  </div>
  <section class="panel">
    <h2>今日行情<span class="hint">实时 · 秒级刷新</span></h2>
    <div class="tbl-wrap"><table id="quoteTable"></table></div>
    <div id="quoteStatus"></div>
  </section>
  <section class="panel">
    <h2>自选股管理<span class="hint">输入代码添加 → 自动跑灯号分析</span></h2>
    <div class="add-form">
      <input id="wlCode" placeholder="股票代码，如 600519" maxlength="8">
      <input id="wlName" placeholder="名称（可留空自动识别）">
      <input id="wlCost" placeholder="成本价（选填）" type="number" step="0.01" min="0">
      <input id="wlWeight" placeholder="目标仓位 %（选填）" type="number" step="0.5" min="0">
      <button class="primary" onclick="addStock()">＋ 添加</button>
    </div>
    <div id="wlMsg"></div>
    <div id="wlList"></div>
  </section>
  <div class="meta-line">
    <span>更新于 <b id="updated">--</b></span>
    <span>数据源 <b id="source">--</b></span>
    <span>共 <b id="count">0</b> 只</span>
  </div>
  <section class="stats" id="stats"></section>
  <section class="panel">
    <h2>灯号总览<span class="hint">产业 · 基本面 · 估值 · 长期筹码 · 短期主力 · 边际变化</span></h2>
    <div class="tbl-wrap"><table id="matrix"></table></div>
  </section>
  <section class="panel">
    <h2>个股明细<span class="hint">点击股票名查看详情</span></h2>
    <div class="view-tabs">
      <button class="vt on" data-view="fan" onclick="switchView('fan')">折骨扇</button>
      <button class="vt" data-view="cards" onclick="switchView('cards')">卡片</button>
    </div>
    <div id="fanView" class="fan-view">
      <div class="fan-head" style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
        <span class="legend">
          <span><i style="background:var(--green)"></i>建仓</span>
          <span><i style="background:var(--amber)"></i>观察</span>
          <span><i style="background:var(--red)"></i>危险</span>
        </span>
      </div>
      <div class="search-row">
        <input id="fanSearch" placeholder="搜索名称 / 代码…" autocomplete="off">
        <span class="count" id="fanCount"></span>
      </div>
      <div class="stage" id="stage">
        <svg viewBox="0 0 660 440" xmlns="http://www.w3.org/2000/svg" id="svgFan">
          <defs>
            <radialGradient id="paper" cx="50%" cy="100%" r="82%">
              <stop offset="0%" stop-color="#F7EED9"/>
              <stop offset="58%" stop-color="#EFE1BF"/>
              <stop offset="100%" stop-color="#DCC79B"/>
            </radialGradient>
            <linearGradient id="rib" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" stop-color="#D9BE80"/>
              <stop offset="50%" stop-color="#F0E0B4"/>
              <stop offset="100%" stop-color="#C3A25C"/>
            </linearGradient>
            <linearGradient id="handle" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" stop-color="#6F4A1F"/>
              <stop offset="45%" stop-color="#96662B"/>
              <stop offset="100%" stop-color="#573A16"/>
            </linearGradient>
            <filter id="shadow" x="-30%" y="-30%" width="160%" height="160%">
              <feDropShadow dx="4" dy="10" stdDeviation="10" flood-color="#000000" flood-opacity="0.5"/>
            </filter>
          </defs>
          <g id="fanG" filter="url(#shadow)"></g>
          <g id="ribG"></g>
          <g id="accG"></g>
        </svg>
        <div class="names" id="names"></div>
      </div>
      <div class="pages" id="pages"></div>
    </div>
    <div id="cardsView" class="cards-view hidden">
      <div class="cards" id="cards"></div>
    </div>
  </section>
  <section class="panel">
    <h2>建仓清单<span class="hint">绿灯 ≥ 4 且无红灯</span></h2>
    <div id="buildable"><span class="warn-line">暂无。</span></div>
  </section>
  <section class="panel">
    <h2>需要关注<span class="hint">红灯 ≥ 2</span></h2>
    <div id="watch"><span class="warn-line">暂无。</span></div>
  </section>
</main>
<div class="modal-overlay hidden" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="m-head">
      <span class="m-name" id="mName"></span>
      <span class="m-code" id="mCode"></span>
      <span id="mBadge"></span>
      <button class="m-close" onclick="closeModal()">×</button>
    </div>
    <div class="m-lights" id="mLights"></div>
    <div class="m-body" id="mBody"></div>
  </div>
</div>
<footer>
  <div class="footer-quote">不怕新人入行就亏钱，就怕新人入行就挣钱，却误把运气当成实力</div>
  <div class="footer-note">数据来自公开接口，仅供个人研究参考，不构成投资建议 · 灯号框架</div>
</footer>
<script>
const DIMS = [
  ["industry","产业"],["fundamental","基本面"],["valuation","估值"],
  ["chips","筹码"],["capital","主力"],["margin","边际"]
];
const LIGHT = {green:"🟢", yellow:"🟡", red:"🔴"};
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtWan = v => {
  if (v == null) return "--";
  const s = Math.abs(v) >= 10000 ? (v/10000).toFixed(2) + "亿" : Math.round(v).toLocaleString("zh-CN") + "万";
  return (v > 0 ? "+" : "") + s;
};
const fmtPct = (v,d=2) => v == null ? "--" : (v > 0 ? "+" : "") + v.toFixed(d) + "%";
const cls = v => v > 0 ? "up" : (v < 0 ? "down" : "flat");
const $ = id => document.getElementById(id);
let busy = false;
let pollCancelled = false;
let STOCKS = [];
let fanPage = 0;
const PAGE_N = 10;

/* ---------- 折骨扇 ---------- */
const CX=330, CY=402, R=292, R0=46, ANG=84;
const TAU = Math.PI/180;
function pt(r,deg){ const a=(90-deg)*TAU; return [CX+r*Math.cos(a), CY-r*Math.sin(a)]; }
function arcPath(r,deg0,deg1,inner){
  const [x0,y0]=pt(r,-deg0), [x1,y1]=pt(r,deg1);
  const [a0,b0]=pt(inner,-deg0), [a1,b1]=pt(inner,deg1);
  const big=(deg0+deg1)>180?1:0;
  return `M ${x0.toFixed(1)} ${y0.toFixed(1)} A ${r} ${r} 0 0 1 ${x1.toFixed(1)} ${y1.toFixed(1)} L ${a1.toFixed(1)} ${b1.toFixed(1)} A ${inner} ${inner} 0 0 0 ${a0.toFixed(1)} ${b0.toFixed(1)} Z`;
}
function tierOf(s){ if (s.buildable) return "g"; if (s.red_count >= 2) return "r"; return "y"; }
function filteredStocks(){
  const q = ($("fanSearch").value || "").trim().toLowerCase();
  if (!q) return STOCKS;
  return STOCKS.filter(s => (s.name||"").toLowerCase().includes(q) || (s.code||"").includes(q));
}
function drawFan(){
  const items = filteredStocks();
  $("fanCount").textContent = `共 ${items.length} 只`;
  $("names").innerHTML = ""; $("pages").innerHTML = ""; $("fanG").innerHTML = ""; $("ribG").innerHTML = "";
  if (!items.length){
    $("pages").innerHTML = `<span class="page-note">暂无数据。点击右上角「灯号分析」开始抓取，或稍后刷新页面。</span>`;
    return;
  }
  const pages = Math.ceil(items.length / PAGE_N);
  if (fanPage >= pages) fanPage = pages - 1;
  const slice = items.slice(fanPage*PAGE_N, (fanPage+1)*PAGE_N);
  const n = slice.length;
  const step = n > 1 ? (2*ANG)/(n-1) : 0;
  const angles = slice.map((_,i) => -ANG + i*step);
  const d0=-ANG, d1=ANG;
  let fan = `<path d="${arcPath(R,d0,d1,R0)}" fill="url(#paper)" stroke="#C3A25C" stroke-width="1.5"/>`;
  for(let rr=R0+55; rr<R; rr+=48){ fan += `<path d="${arcPath(rr,d0,d1,rr)}" fill="none" stroke="#C3A25C" stroke-width=".8" opacity=".22"/>`; }
  $("fanG").innerHTML = fan;
  let ribs = "";
  angles.forEach(a=>{ const [x0,y0]=pt(R0,a), [x1,y1]=pt(R,a); ribs += `<line x1="${x0.toFixed(1)}" y1="${y0.toFixed(1)}" x2="${x1.toFixed(1)}" y2="${y1.toFixed(1)}" stroke="url(#rib)" stroke-width="2.4"/>`; });
  const [bx0,by0]=pt(R,-ANG), [bx1,by1]=pt(R,ANG);
  ribs += `<line x1="${CX}" y1="${CY}" x2="${bx0.toFixed(1)}" y2="${by0.toFixed(1)}" stroke="url(#rib)" stroke-width="3"/>`;
  ribs += `<line x1="${CX}" y1="${CY}" x2="${bx1.toFixed(1)}" y2="${by1.toFixed(1)}" stroke="url(#rib)" stroke-width="3"/>`;
  $("ribG").innerHTML = ribs;
  $("accG").innerHTML = `<rect x="${CX-13}" y="${CY}" width="26" height="34" rx="6" fill="url(#handle)" stroke="#3E2A10"/><circle cx="${CX}" cy="${CY}" r="6.5" fill="#2E1F0B" stroke="#C3A25C"/>`;
  const mid = 172;
  $("names").innerHTML = slice.map((s,i)=>{
    const a = angles[i];
    const [x,y] = pt(mid,a);
    const t = tierOf(s);
    return `<div class="nm ${t}" style="left:${x}px;top:${y}px;transform:translate(-50%,-50%) rotate(${a}deg)" onclick="showDetail(${STOCKS.indexOf(s)})">${esc(s.name)}</div>`;
  }).join("");
  $("pages").innerHTML =
    `<button class="pg" onclick="fanGo(-1)">‹</button>` +
    Array.from({length:pages},(_,i)=>`<button class="pg ${i===fanPage?'on':''}" onclick="fanGoTo(${i})">扇面 ${i+1}</button>`).join("") +
    `<button class="pg" onclick="fanGo(1)">›</button>` +
    `<span class="page-note">每面最多 ${PAGE_N} 只 · 当前第 ${fanPage+1}/${pages} 面</span>`;
}
function fanGo(d){ const items=filteredStocks(); const pages=Math.ceil(items.length/PAGE_N)||1; fanPage=Math.max(0,Math.min(pages-1,fanPage+d)); drawFan(); }
function fanGoTo(i){ fanPage=i; drawFan(); }
function switchView(name){
  document.querySelectorAll(".vt").forEach(b => b.classList.toggle("on", b.dataset.view === name));
  $("fanView").classList.toggle("hidden", name !== "fan");
  $("cardsView").classList.toggle("hidden", name !== "cards");
}

function badge(s){
  if (s.buildable) return '<span class="badge ok">可建仓</span>';
  if (s.red_count >= 2) return '<span class="badge bad">关注</span>';
  if (s.status === "接近可建仓") return '<span class="badge warn">接近可建仓</span>';
  return '<span class="badge">暂不可建仓</span>';
}

function render(s){
  const stocks = s.stocks || [];
  $("updated").textContent = s.updated || "--";
  $("source").textContent = s.data_sources || "--";
  $("count").textContent = stocks.length;

  const buildable = stocks.filter(x => x.buildable).length;
  const watch = stocks.filter(x => x.red_count >= 2).length;
  const warns = stocks.reduce((n,x) => n + (x.warnings||[]).length, 0);
  $("stats").innerHTML = [
    ["监控股票", stocks.length, ""],
    ["可建仓", buildable, buildable ? "green" : ""],
    ["需要关注", watch, watch ? "red" : ""],
    ["数据提示", warns, warns ? "amber" : ""],
  ].map(([lbl,num,c]) => `<div class="stat"><div class="num ${c}">${num}</div><div class="lbl">${lbl}</div></div>`).join("");

  let rows = '<tr><th class="name-cell">股票</th>';
  DIMS.forEach(d => rows += `<th>${d[1]}</th>`);
  rows += '<th>绿</th><th>红</th><th>综合</th></tr>';
  stocks.forEach(x => {
    rows += `<tr><td class="name-cell"><span class="nm">${esc(x.name)}</span> <span class="cd">${esc(x.code)}</span></td>`;
    DIMS.forEach(d => rows += `<td class="light-cell">${LIGHT[x.lights[d[0]]] || "🟡"}</td>`);
    rows += `<td>${x.green_count}</td><td>${x.red_count}</td><td>${badge(x)}</td></tr>`;
  });
  $("matrix").innerHTML = rows;
  STOCKS = stocks;
  $("cards").innerHTML = stocks.map((x,i) => cardHtml(x,i)).join("");
  drawFan();

  const bHtml = buildable
    ? stocks.filter(x => x.buildable).map(x => `<div class="warn-line">→ ${esc(x.name)} ${esc(x.code)}${x.alert ? " · 🔥 " + esc(x.alert) : ""}</div>`).join("")
    : '<span class="warn-line">暂无。</span>';
  $("buildable").innerHTML = bHtml;
  const wHtml = watch
    ? stocks.filter(x => x.red_count >= 2).map(x => `<div class="warn-line">→ ${esc(x.name)}（${x.red_count}红）：${esc(x.light_reasons ? Object.values(x.light_reasons).filter((_,i)=>x.lights && Object.values(x.lights)[i]==="red").join("，") : "")}</div>`).join("")
    : '<span class="warn-line">暂无。</span>';
  $("watch").innerHTML = wHtml;

  const banner = s.error ? `<div class="banner">抓取出错：${esc(s.error)}</div>` : "";
  $("bannerWrap").innerHTML = banner;
}

function cardHtml(x, i){
  const lights = DIMS.map(d => LIGHT[x.lights[d[0]]] || "🟡").join("");
  return `
  <div class="card" onclick="showDetail(${i})">
    <div class="c-top">
      <span class="who">${esc(x.name)}<span class="cd">${esc(x.code)}</span></span>
      <span class="price ${cls(x.pct_chg)}">${x.price == null ? "--" : x.price.toFixed(2)}</span>
    </div>
    <div class="c-lights">${lights}</div>
    <div class="c-meta">
      <span class="${cls(x.pct_chg)}">${fmtPct(x.pct_chg)}</span>
      ${badge(x)}
    </div>
    ${x.light_changes ? `<div class="c-chg">⇄ ${esc(x.light_changes)}</div>` : ""}
  </div>`;
}

function detailRowsHtml(x){
  const rows = [];
  if (x.light_changes) rows.push(["灯号变化", esc(x.light_changes)]);
  rows.push(["估值",
    `PE ${x.pe_ttm == null ? "--" : x.pe_ttm.toFixed(1)}倍<span class="dim">（分位 ${x.pe_pct == null ? "--" : x.pe_pct.toFixed(0)}%）</span> · ` +
    `PB ${x.pb == null ? "--" : x.pb.toFixed(1)}倍<span class="dim">（分位 ${x.pb_pct == null ? "--" : x.pb_pct.toFixed(0)}%）</span>`]);
  const c5 = x.lights.capital === "green" ? "up" : (x.lights.capital === "red" ? "down" : "");
  rows.push(["主力资金",
    `1日 ${fmtWan(x.flow_1d_wan)} · 5日 <span class="${c5}">${fmtWan(x.flow_5d_wan)}</span> · 20日 ${fmtWan(x.flow_20d_wan)}`]);
  if (x.inst_count != null){
    let chg = "";
    if (x.inst_count_chg != null || x.inst_ratio_chg != null){
      const bits = [];
      if (x.inst_count_chg != null) bits.push(`家数${x.inst_count_chg > 0 ? "+" : ""}${x.inst_count_chg}`);
      if (x.inst_ratio_chg != null) bits.push(`比例${x.inst_ratio_chg > 0 ? "+" : ""}${x.inst_ratio_chg.toFixed(2)}pp`);
      chg = `（${bits.join("，")}）`;
    }
    rows.push(["机构持仓", `${x.inst_count}家 · 占流通 ${x.inst_ratio_pct.toFixed(2)}%${chg}`]);
  } else rows.push(["机构持仓", "数据缺失"]);
  if (x.high52 != null && x.low52 != null){
    const pos = Math.max(0, Math.min(100, x.pos52_pct || 0));
    rows.push(["52周位置",
      `<div class="range"><div class="fill" style="width:${pos.toFixed(1)}%"></div><div class="dot" style="left:${pos.toFixed(1)}%"></div></div>` +
      `<div class="range-meta"><span>低 ${x.low52.toFixed(2)}</span><span>${pos.toFixed(0)}%</span><span>高 ${x.high52.toFixed(2)}</span></div>` +
      `<div class="range-meta" style="margin-top:2px"><span>距低 ${fmtPct(x.dist_low_pct,1)}</span><span>距高 ${fmtPct(x.dist_high_pct,1)}</span></div>`]);
  }
  if (x.report_date) rows.push(["最新财报",
    `营收同比 ${fmtPct(x.rev_yoy,1)} · 净利同比 ${fmtPct(x.profit_yoy,1)}` +
    (x.gross_margin != null ? ` · 毛利率 ${x.gross_margin.toFixed(1)}%` : "") + ` <span class="dim">（${esc(x.report_date)}）</span>`]);
  if (x.cost != null){
    rows.push(["我的仓位",
      `成本 ${x.cost.toFixed(2)} · 盈亏 <span class="${cls(x.pnl_pct)}">${fmtPct(x.pnl_pct)}</span>` +
      (x.target_weight != null ? ` · 目标 ${x.target_weight.toFixed(1)}%` : "")]);
  }
  if (x.industry_note) rows.push(["产业逻辑", esc(x.industry_note)]);
  if (x.margin_note) rows.push(["边际备注", esc(x.margin_note)]);
  let h = rows.map(([l,v]) => `<div class="row"><span class="lbl">${l}</span><span class="val">${v}</span></div>`).join("");
  if ((x.warnings || []).length) h += `<div class="note">⚠ ${(x.warnings||[]).map(esc).join("；")}</div>`;
  return h;
}

function showDetail(i){
  const x = STOCKS[i];
  if (!x) return;
  $("mName").textContent = x.name;
  $("mCode").textContent = x.code;
  $("mBadge").innerHTML = badge(x);
  $("mLights").textContent = DIMS.map(d => LIGHT[x.lights[d[0]]] || "🟡").join("");
  $("mBody").innerHTML = detailRowsHtml(x);
  $("modal").classList.remove("hidden");
}

function closeModal(){
  $("modal").classList.add("hidden");
}

document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });
$("fanSearch").addEventListener("input", () => { fanPage = 0; drawFan(); });

function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }

function showDeep(on){ $("deepBanner").classList.toggle("hidden", !on); }

function fmtAmount(v){
  if (v == null) return "--";
  return v >= 1e8 ? (v / 1e8).toFixed(2) + "亿" : (v / 1e4).toFixed(0) + "万";
}

function renderQuotes(q){
  const rows = (q.quotes || []).map(x => `
    <tr>
      <td class="name-cell"><span class="nm">${esc(x.name)}</span> <span class="cd">${esc(x.code)}</span></td>
      <td class="${cls(x.pct_chg)}">${x.price == null ? "--" : x.price.toFixed(2)}</td>
      <td class="${cls(x.pct_chg)}">${fmtPct(x.pct_chg)}</td>
      <td>${fmtAmount(x.amount)}</td>
      <td>${x.turnover_rate == null ? "--" : x.turnover_rate.toFixed(2) + "%"}</td>
      <td>${x.mv_yi == null ? "--" : Math.round(x.mv_yi) + "亿"}</td>
    </tr>`).join("");
  $("quoteTable").innerHTML =
    `<tr><th class="name-cell">股票</th><th>最新价</th><th>涨跌幅</th><th>成交额</th><th>换手率</th><th>总市值</th></tr>` + rows;
  const ok = q.quotes && q.quotes.length;
  $("quoteStatus").textContent = ok ? `更新于 ${q.time} · 数据源 ${q.source}` : "行情加载失败，或自选股暂无行情";
}

async function loadQuotes(){
  $("quoteStatus").textContent = "行情加载中…";
  try {
    const q = await (await fetch("/api/quote")).json();
    renderQuotes(q);
  } catch (e){
    $("quoteStatus").textContent = "行情加载失败：" + e;
  }
}

async function loadWatchlist(){
  const r = await (await fetch("/api/watchlist")).json();
  const list = r.watchlist || [];
  $("wlList").innerHTML = list.length
    ? list.map(w => `
      <div class="wl-item">
        <span class="info">
          <span class="code">${esc(w.code)}</span>
          <span>${esc(w.name || "")}</span>
          <span class="meta">成本 ${w.cost == null ? "--" : w.cost} · 仓位 ${w.target_weight == null ? "--" : w.target_weight}%</span>
        </span>
        <button class="del" title="移出自选股" onclick="removeStock('${esc(w.code)}')">✕</button>
      </div>`).join("")
    : '<span class="warn-line">尚未添加自选股。</span>';
}

async function addStock(){
  const code = $("wlCode").value.trim();
  if (!code){ $("wlMsg").textContent = "请填写股票代码"; return; }
  const body = {
    code,
    name: $("wlName").value.trim(),
    cost: $("wlCost").value,
    target_weight: $("wlWeight").value,
  };
  const res = await (await fetch("/api/watchlist/add", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  })).json();
  if (res.error){ $("wlMsg").textContent = res.error; return; }
  $("wlMsg").textContent = `已添加 ${code}，正在抓取灯号分析数据（约1~2分钟），稍后即可在下方看到分析结果…`;
  ["wlCode","wlName","wlCost","wlWeight"].forEach(id => $(id).value = "");
  await loadWatchlist();
  await loadQuotes();
  deepRefresh();
}

async function removeStock(code){
  if (!confirm(`确定把 ${code} 移出自选股？`)) return;
  const res = await (await fetch("/api/watchlist/remove", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({code}),
  })).json();
  $("wlMsg").textContent = res.error || `已移除 ${code}，正在重新分析…`;
  await loadWatchlist();
  await loadQuotes();
  deepRefresh();
}

async function pollDeep(){
  pollCancelled = false;
  for (;;){
    await sleep(2000);
    if (pollCancelled) return;
    const s = await (await fetch("/api/status")).json();
    const last = (s.log || []).slice(-1)[0] || "";
    $("deepText").textContent = "正在抓取灯号分析数据（约1~2分钟）… " + last;
    if (!s.running && !s.pending){
      busy = false;
      $("deepBtn").disabled = false;
      showDeep(false);
      render(s);
      return;
    }
  }
}

async function deepRefresh(){
  pollCancelled = false;
  await fetch("/api/refresh", {method:"POST"});
  if (!busy){
    busy = true;
    $("deepBtn").disabled = true;
    showDeep(true);
    $("deepText").textContent = "正在启动灯号分析…";
    pollDeep();
  }
}

function cancelDeep(){
  pollCancelled = true;
  busy = false;
  $("deepBtn").disabled = false;
  showDeep(false);
  $("quoteStatus").textContent = "已取消等待。分析在后台继续，完成后点「灯号分析」查看结果。";
}

window.addEventListener("load", async () => {
  loadQuotes();
  loadWatchlist();
  const s = await (await fetch("/api/status")).json();
  render(s);
});
</script>
</body>
</html>
"""


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cfg = load_config("config.json")
    server = None
    url = f"http://{HOST}:{PORT}"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"面板已经在运行中（端口 {PORT} 已被占用），无需重复启动，直接刷新浏览器即可。")
        webbrowser.open(url)
        return
    print(f"灯号监控面板已启动: {url}")
    webbrowser.open(url)
    if server:
        _start_refresh()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n面板已关闭。")


if __name__ == "__main__":
    main()
