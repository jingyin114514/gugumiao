# -*- coding: utf-8 -*-
"""灯号监控面板（本地面板版）

双击 启动面板.bat 即可：自动启动本地服务并在浏览器打开可视化界面。
数据仍全部本地处理、实时联网抓取，不上传任何内容。
"""

import json
import socket
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
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
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
        elif path.startswith("/static/"):
            self._serve_static(path)
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

    def _serve_static(self, path: str) -> None:
        root = (Path(__file__).resolve().parent / "static").resolve()
        rel = path[len("/static/"):]
        target = (root / rel).resolve()
        if not str(target).startswith(str(root) + "\\"):
            self._json({"error": "forbidden"}, status=403)
            return
        if not target.is_file():
            self._json({"error": "not found"}, status=404)
            return
        suffix = target.suffix.lower()
        if suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif suffix == ".html":
            ctype = "text/html; charset=utf-8"
        elif suffix == ".css":
            ctype = "text/css; charset=utf-8"
        else:
            ctype = "application/octet-stream"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>灯号监控面板</title>
<link rel="stylesheet" href="/static/splash.css">
<style>
:root{
  --bg0:#050403; --bg1:#0B0805; --bg2:#151009;
  --card:#0E0B07; --card2:#171109; --card3:#20180D;
  --ink:#F7EDD2; --ink2:#E0CDA4; --mut:#A58F63;
  --line:#3A2C16; --line2:#6B5223;
  --gold:#E3B341; --goldHi:#FFD97A; --goldDim:rgba(227,179,65,.16);
  --verm:#FF3B1F; --vermHi:#FF6B4A; --vermDim:rgba(255,59,31,.16);
  --green:#00E5A0; --greenHi:#54FFC8; --greenDim:rgba(0,229,160,.14);
  --font-serif:"STSong","Songti SC","SimSun","Noto Serif SC",serif;
  --font-sans:"Microsoft YaHei","PingFang SC","Segoe UI",sans-serif;
  --font-mono:"Cascadia Mono","JetBrains Mono",Consolas,monospace;
  --t-fast:140ms; --t-norm:260ms; --t-slow:720ms;
}
@property --ang{syntax:"<angle>";initial-value:0deg;inherits:false}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{
  font-family:var(--font-sans);font-weight:700;color:var(--ink);line-height:1.75;font-size:14px;min-height:100vh;overflow-x:hidden;
  background:
    radial-gradient(1100px 720px at 12% -10%, rgba(227,179,65,.20), transparent 60%),
    radial-gradient(900px 680px at 96% 4%, rgba(255,59,31,.16), transparent 55%),
    radial-gradient(1400px 900px at 50% 118%, rgba(0,229,160,.06), transparent 60%),
    var(--bg0);
}
body::before{
  content:"";position:fixed;inset:-60px;z-index:-1;pointer-events:none;
  background:
    repeating-linear-gradient(0deg,transparent 0 46px,rgba(227,179,65,.10) 46px 47px),
    repeating-linear-gradient(90deg,transparent 0 46px,rgba(227,179,65,.10) 46px 47px);
  animation:gridmove 9s linear infinite;
  -webkit-mask-image:radial-gradient(ellipse 90% 70% at 50% 30%,#000 30%,transparent 78%);
  mask-image:radial-gradient(ellipse 90% 70% at 50% 30%,#000 30%,transparent 78%);
}
body::after{
  content:"";position:fixed;inset:0;z-index:2000;pointer-events:none;
  background:repeating-linear-gradient(0deg,transparent 0 3px,rgba(0,0,0,.24) 3px 4px);
}
.scan-band{
  position:fixed;left:0;right:0;top:0;height:170px;z-index:2001;pointer-events:none;opacity:.55;
  background:linear-gradient(180deg,transparent,rgba(227,179,65,.12),transparent);
  animation:scan 7s linear infinite;
}
::selection{background:var(--gold);color:#170E02;text-shadow:none}
:focus-visible{outline:2px solid var(--gold);outline-offset:2px;border-radius:4px;box-shadow:0 0 18px rgba(227,179,65,.5)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:#0B0805}
::-webkit-scrollbar-thumb{background:linear-gradient(180deg,#6B5223,#3A2C16);border:2px solid #0B0805;border-radius:5px}
::-webkit-scrollbar-thumb:hover{background:linear-gradient(180deg,#E3B341,#6B5223)}
.skip-link{position:absolute;left:12px;top:-48px;z-index:2000;background:var(--ink);color:#fff;padding:8px 14px;border-radius:0 0 8px 8px;font-size:13px;transition:top .15s}
.skip-link:focus{top:0}
header{position:sticky;top:0;z-index:100;background:rgba(5,4,3,.88);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--line2)}
header::after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:2px;background:linear-gradient(90deg,transparent,var(--gold) 18%,var(--verm) 50%,var(--gold) 82%,transparent);background-size:220% 100%;animation:beam 5s ease-in-out infinite}
.head-inner{max-width:1280px;margin:0 auto;padding:14px 22px;display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:12px;font-size:18px;font-weight:900;letter-spacing:.6px}
.brand .logo{display:inline-flex;gap:5px;align-items:center;background:#0B0805;border:1px solid var(--line2);border-radius:4px;padding:6px 10px;box-shadow:0 0 14px rgba(227,179,65,.25),inset 0 0 8px rgba(227,179,65,.12)}
.brand .logo i{display:inline-block;width:9px;height:9px;border-radius:2px;animation:logoPulse 2.2s ease-in-out infinite}
.brand .logo i.g{background:var(--green);box-shadow:0 0 10px var(--green)}
.brand .logo i.y{background:var(--gold);box-shadow:0 0 10px var(--gold)}
.brand .logo i.r{background:var(--verm);box-shadow:0 0 10px var(--verm)}
.brand .logo i:nth-child(2){animation-delay:.35s}
.brand .logo i:nth-child(3){animation-delay:.7s}
.brand .sub{color:var(--mut);font-size:12px;letter-spacing:.4px;font-weight:800}
.brand .sub .ver{display:inline-block;margin-left:7px;padding:2px 10px;border:1px solid var(--gold);border-radius:4px;color:var(--goldHi);font-size:11px;font-weight:900;text-shadow:0 0 8px rgba(227,179,65,.5);box-shadow:0 0 12px rgba(227,179,65,.25)}
.actions{display:flex;gap:8px}
button{font:inherit;border:1px solid transparent;border-radius:4px;padding:8px 14px;cursor:pointer;transition:background var(--t-fast),border-color var(--t-fast),box-shadow var(--t-fast),transform .05s;min-height:36px}
button:active{transform:translateY(1px)}
.u-btn{
  --border-radius:24px;--padding:4px;--transition:.4s;
  --button-color:#101010;--highlight-color-hue:43deg;
  position:relative;z-index:0;user-select:none;display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:.5em .95em .5em 1.15em;font-family:"Poppins","Inter","Segoe UI",var(--font);
  font-size:13px;font-weight:500;letter-spacing:.3px;color:#fff;background:var(--button-color);
  border:1px solid #fff2;border-radius:var(--border-radius);cursor:pointer;min-height:38px;
  box-shadow:inset 0 1px 1px rgba(255,255,255,.2),inset 0 2px 2px rgba(255,255,255,.15),inset 0 4px 4px rgba(255,255,255,.1),inset 0 8px 8px rgba(255,255,255,.05),inset 0 16px 16px rgba(255,255,255,.05),0 -1px 1px rgba(0,0,0,.02),0 -2px 2px rgba(0,0,0,.03),0 -4px 4px rgba(0,0,0,.05),0 -8px 8px rgba(0,0,0,.06),0 -16px 16px rgba(0,0,0,.08);
  transition:box-shadow var(--transition),border var(--transition),background-color var(--transition),opacity .2s;
}
.u-btn::before{content:"";position:absolute;top:calc(0px - var(--padding));left:calc(0px - var(--padding));width:calc(100% + var(--padding)*2);height:calc(100% + var(--padding)*2);border-radius:calc(var(--border-radius) + var(--padding));pointer-events:none;background-image:linear-gradient(0deg,#0004,#000a);z-index:-1;transition:box-shadow var(--transition),filter var(--transition);box-shadow:0 -8px 8px -6px #0000 inset,0 -16px 16px -8px #00000000 inset,1px 1px 1px #fff2,2px 2px 2px #fff1,-1px -1px 1px #0002,-2px -2px 2px #0001}
.u-btn::after{content:"";position:absolute;top:0;left:0;width:100%;height:100%;border-radius:inherit;pointer-events:none;background-image:linear-gradient(to top,hsla(var(--highlight-color-hue),92%,44%,.80) 0%,hsla(var(--highlight-color-hue),94%,50%,.46) 26%,hsla(var(--highlight-color-hue),96%,58%,.22) 40%,transparent 52%);background-position:0 0;opacity:0;transition:opacity var(--transition),filter var(--transition)}
.u-btn .u-label{position:relative;display:inline-block;color:#fff;text-shadow:0 0 4px hsla(var(--highlight-color-hue),95%,58%,.45);transition:color var(--transition),text-shadow var(--transition)}
.u-btn .u-ico{height:16px;width:16px;fill:#fff;filter:drop-shadow(0 0 3px hsl(var(--highlight-color-hue),95%,58%));transition:fill var(--transition),filter var(--transition),opacity var(--transition);flex-shrink:0}
.u-btn:hover{border:1px solid hsla(var(--highlight-color-hue),90%,62%,45%)}
.u-btn:hover::before{box-shadow:0 -8px 8px -6px #fffa inset,0 -16px 16px -8px hsla(var(--highlight-color-hue),95%,55%,32%) inset,1px 1px 1px #fff2,2px 2px 2px #fff1,-1px -1px 1px #0002,-2px -2px 2px #0001}
.u-btn:hover::after{opacity:1}
.u-btn:hover .u-label{color:#fff;text-shadow:0 0 6px hsla(var(--highlight-color-hue),95%,65%,.9)}
.u-btn:hover .u-ico{fill:#fff;filter:drop-shadow(0 0 4px hsl(var(--highlight-color-hue),95%,65%)) drop-shadow(0 -4px 6px #0009)}
.u-btn:focus-visible{outline:2px solid hsla(var(--highlight-color-hue),95%,58%,.85);outline-offset:2px}
.u-btn:active{border:1px solid hsla(var(--highlight-color-hue),95%,62%,75%);background-color:hsla(var(--highlight-color-hue),60%,18%,.55)}
.u-btn:active::after{opacity:1;filter:brightness(160%)}
.u-btn:active .u-label{text-shadow:0 0 1px hsla(var(--highlight-color-hue),100%,80%,90%);animation:none}
.u-btn:disabled{opacity:.5;cursor:wait}
.u-btn--icon{padding:.3em;width:32px;height:32px;min-height:32px;border-radius:50%}
.u-btn--icon .u-label{animation:none;font-size:14px;line-height:1}
.u-btn--on-dark{--button-color:rgba(255,255,255,.10);border-color:#fff3}
.u-btn--on-dark:hover{--button-color:rgba(255,255,255,.18)}
main{max-width:1280px;margin:0 auto;padding:30px 22px 56px}
.meta-line{color:var(--mut);font-size:12.5px;margin-bottom:16px;display:flex;gap:14px;flex-wrap:wrap;font-weight:800}
.meta-line b{color:var(--goldHi);font-weight:900;font-variant-numeric:tabular-nums;text-shadow:0 0 10px rgba(227,179,65,.35)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:20px}
.stat{position:relative;border:1px solid var(--line);border-left:3px solid var(--line2);border-radius:4px;padding:14px 16px;background:linear-gradient(180deg,#171109,#100C06);box-shadow:0 12px 30px rgba(0,0,0,.4),inset 0 0 24px rgba(227,179,65,.04);transition:transform var(--t-fast),box-shadow var(--t-fast),border-color var(--t-fast)}
.stat:hover{transform:translateY(-3px);border-left-color:var(--gold);box-shadow:0 16px 34px rgba(0,0,0,.5),0 0 20px rgba(227,179,65,.16)}
.stat .num{font-size:24px;font-weight:900;font-variant-numeric:tabular-nums;line-height:1.2;color:var(--ink);text-shadow:0 0 12px rgba(227,179,65,.18)}
.stat .lbl{color:var(--mut);font-size:12px;margin-top:2px;font-weight:800}
.stat .num.green{color:var(--greenHi);text-shadow:0 0 10px rgba(0,229,160,.4)}
.stat .num.amber{color:var(--goldHi);text-shadow:0 0 10px rgba(227,179,65,.4)}
.stat .num.red{color:var(--vermHi);text-shadow:0 0 10px rgba(255,59,31,.4)}
.panel{position:relative;background:linear-gradient(180deg,#14100A,#0B0805 130%);border:1px solid var(--line);border-radius:6px;padding:24px 26px;margin-bottom:22px;box-shadow:0 24px 60px rgba(0,0,0,.5),inset 0 1px 0 rgba(227,179,65,.08)}
.panel::before{content:"";position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--gold),rgba(255,59,31,.6) 55%,transparent);background-size:220% 100%;box-shadow:0 0 20px rgba(227,179,65,.5);animation:beam 6s ease-in-out infinite}
.panel::after{content:"";position:absolute;top:12px;right:14px;width:22px;height:22px;border-top:2px solid var(--gold);border-right:2px solid var(--gold);opacity:.6;box-shadow:0 0 10px rgba(227,179,65,.35)}
.panel h2{display:flex;align-items:center;gap:12px;flex-wrap:wrap;font-size:17px;font-weight:900;margin-bottom:16px;letter-spacing:.6px;color:var(--ink)}
.panel h2 .no{display:inline-flex;align-items:center;justify-content:center;min-width:30px;height:30px;padding:0 8px;background:linear-gradient(180deg,var(--goldHi),var(--gold));color:#170E02;font-family:var(--font-mono);font-size:14px;font-weight:900;border-radius:4px;box-shadow:0 0 16px rgba(227,179,65,.55)}
.panel h2 .hint{color:var(--mut);font-size:12px;font-weight:700;letter-spacing:.3px}
.tbl-wrap{position:relative;overflow-x:auto;border:1px solid var(--line);border-radius:4px;background:rgba(0,0,0,.25);box-shadow:inset 0 0 30px rgba(0,0,0,.4)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:12px 14px;text-align:center;white-space:nowrap;font-weight:800}
th{font-weight:900;color:var(--goldHi);font-size:12.5px;letter-spacing:2px;background:rgba(227,179,65,.08);border-bottom:2px solid var(--line2);text-shadow:0 0 10px rgba(227,179,65,.4)}
tbody tr{transition:background var(--t-fast),box-shadow var(--t-fast)}
tbody tr:hover{background:rgba(227,179,65,.08);box-shadow:inset 3px 0 0 var(--gold)}
td.name-cell,th.name-cell{text-align:left}
td .nm{font-weight:900;color:var(--ink)}
td .cd{color:var(--muted);font-size:12px}
.light-cell{font-size:16px;line-height:1}
.up{color:var(--vermHi)} .down{color:var(--greenHi)} /* A股习惯：红涨绿跌 */
.flat{color:var(--mut)}
.badge{display:inline-block;padding:3px 12px;border-radius:4px;font-size:11.5px;font-weight:900;letter-spacing:.5px;background:rgba(227,179,65,.08);color:var(--ink2);border:1px solid var(--line2);white-space:nowrap}
.badge.ok{background:var(--greenDim);color:var(--greenHi);border:1px solid rgba(0,229,160,.5);box-shadow:0 0 12px rgba(0,229,160,.25)}
.badge.warn{background:var(--goldDim);color:var(--goldHi);border:1px solid rgba(227,179,65,.5);box-shadow:0 0 12px rgba(227,179,65,.25)}
.badge.bad{background:var(--vermDim);color:var(--vermHi);border:1px solid rgba(255,59,31,.55);box-shadow:0 0 12px rgba(255,59,31,.28)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:12px}
.card{
  position:relative;overflow:hidden;
  --ptn-c1:rgba(227,179,65,.04);--ptn-c2:rgba(227,179,65,.09);--ptn-c3:rgba(227,179,65,.16);--s:65px;
  --_c:75%,var(--ptn-c3) 52.72deg,#0000 0;
  --_g1:conic-gradient(from -116.36deg at 25% var(--_c));
  --_g2:conic-gradient(from 63.43deg at 75% var(--_c));
  border:1px solid var(--line);border-radius:6px;padding:12px 14px;cursor:pointer;
  background-color:#100C06;
  background-image:
    var(--_g1),var(--_g1) calc(3*var(--s)) calc(var(--s)/2),
    var(--_g2),var(--_g2) calc(3*var(--s)) calc(var(--s)/2),
    conic-gradient(var(--ptn-c2) 63.43deg,var(--ptn-c1) 0 116.36deg,var(--ptn-c2) 0 180deg,var(--ptn-c1) 0 243.43deg,var(--ptn-c2) 0 296.15deg,var(--ptn-c1) 0);
  background-size:calc(2*var(--s)) var(--s);
  box-shadow:0 12px 30px rgba(0,0,0,.4),inset 0 0 24px rgba(227,179,65,.03);
  transition:border-color var(--t-fast),box-shadow var(--t-fast),transform var(--t-fast);
}
.card::before{content:"";position:absolute;inset:0 0 auto 0;height:3px;background:var(--line2);transition:background var(--t-fast)}
.card:hover{border-color:var(--line2);box-shadow:0 16px 34px rgba(0,0,0,.5),0 0 20px rgba(227,179,65,.16);transform:translateY(-3px)}
.card--ok{--ptn-c1:rgba(0,229,160,.05);--ptn-c2:rgba(0,229,160,.10);--ptn-c3:rgba(0,229,160,.20);border-color:rgba(0,229,160,.45)}
.card--ok::before{background:var(--green);box-shadow:0 0 14px rgba(0,229,160,.7)}
.card--warn{--ptn-c1:rgba(227,179,65,.06);--ptn-c2:rgba(227,179,65,.12);--ptn-c3:rgba(227,179,65,.22);border-color:rgba(227,179,65,.45)}
.card--warn::before{background:var(--gold);box-shadow:0 0 14px rgba(227,179,65,.7)}
.card--bad{--ptn-c1:rgba(255,59,31,.05);--ptn-c2:rgba(255,59,31,.11);--ptn-c3:rgba(255,59,31,.20);border-color:rgba(255,59,31,.5)}
.card--bad::before{background:var(--verm);box-shadow:0 0 14px rgba(255,59,31,.7)}
.card .c-top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.card .who{font-size:14px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:var(--font-serif);letter-spacing:.4px;color:var(--ink)}
.card .who .cd{color:var(--mut);font-weight:700;font-size:11px;margin-left:5px}
.card .price{font-size:15px;font-weight:900;font-variant-numeric:tabular-nums;white-space:nowrap;text-shadow:0 0 10px rgba(227,179,65,.25)}
.card .c-lights{font-size:14px;letter-spacing:1px;margin:6px 0 7px;line-height:1}
.card .c-meta{display:flex;justify-content:space-between;align-items:center;font-size:11.5px;color:var(--mut);gap:6px;font-weight:800}
.card .c-meta .badge{font-size:10.5px;padding:2px 9px}
.c-chg{display:inline-flex;align-items:center;gap:4px;max-width:100%;margin-top:7px;padding:2px 9px;border-radius:4px;font-size:10.5px;font-weight:900;color:var(--goldHi);background:var(--goldDim);border:1px solid rgba(227,179,65,.45);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row{display:flex;justify-content:space-between;gap:14px;padding:8px 0;border-bottom:1px dashed var(--line2);font-size:13px;font-weight:800}
.row:last-child{border-bottom:none}
.row .lbl{color:var(--mut);flex-shrink:0}
.row .val{text-align:right;font-variant-numeric:tabular-nums;word-break:break-all;color:var(--ink)}
.lights-lg{font-size:17px;letter-spacing:2px}
.range{position:relative;height:12px;border-radius:3px;background:#080604;border:1px solid var(--line2);margin:9px 0 3px;overflow:hidden;box-shadow:inset 0 0 12px rgba(0,0,0,.6)}
.range .fill{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg,var(--verm),var(--gold) 60%,var(--goldHi));box-shadow:0 0 14px rgba(227,179,65,.6)}
.range .dot{position:absolute;top:50%;width:13px;height:13px;border-radius:50%;background:#fff;transform:translate(-50%,-50%);border:3px solid var(--gold);box-shadow:0 0 12px rgba(227,179,65,.8)}
.range-meta{display:flex;justify-content:space-between;color:var(--mut);font-size:11px;font-variant-numeric:tabular-nums;font-weight:800}
.note{font-size:12.5px;color:var(--mut);background:rgba(0,0,0,.35);border-left:4px solid var(--gold);padding:12px 15px;border-radius:0 4px 4px 0;margin-top:14px;font-weight:800;line-height:1.9;box-shadow:inset 0 0 20px rgba(227,179,65,.04)}
.warn-line{color:var(--goldHi);font-size:12.5px;padding:5px 0;font-weight:800}
.banner{position:relative;overflow:hidden;background:linear-gradient(90deg,rgba(255,59,31,.16),rgba(255,59,31,.06));color:var(--vermHi);border:1px solid rgba(255,59,31,.5);border-radius:4px;padding:12px 16px;margin-bottom:22px;font-size:12.5px;font-weight:900;line-height:1.7;box-shadow:0 0 26px rgba(255,59,31,.12),inset 0 0 30px rgba(255,59,31,.05)}
footer{max-width:1280px;margin:0 auto;padding:24px 22px 42px;color:var(--mut);font-size:12px;font-weight:800;text-align:center;border-top:1px solid var(--line2);position:relative}
footer::before{content:"";position:absolute;top:-1px;left:50%;transform:translateX(-50%);width:220px;height:2px;background:linear-gradient(90deg,transparent,var(--gold),transparent);box-shadow:0 0 16px rgba(227,179,65,.6)}
.footer-quote{color:var(--goldHi);font-weight:900;font-size:15px;margin-bottom:8px;font-family:var(--font-serif);letter-spacing:1px;text-shadow:0 0 14px rgba(227,179,65,.4);animation:quote-flicker 5s steps(1) infinite}
.footer-note{color:var(--mut);font-size:11px;opacity:.95}
.hidden{display:none!important}
.banner-info{background:linear-gradient(180deg,#14100A,#0B0805);color:#F9FAFB;border:1px solid var(--line2);border-radius:6px;padding:16px 56px 16px 18px;margin-bottom:22px;display:flex;align-items:center;gap:4px;font-size:13px;position:relative;overflow:hidden;box-shadow:0 24px 60px rgba(0,0,0,.5),inset 0 1px 0 rgba(227,179,65,.08)}
.u-loader{position:relative;width:80px;height:80px;flex-shrink:0;transform:translate(-12px,-12px) scale(.24);transform-origin:center}
.u-loader .u-bird{transform:translate(-25px,-54px)}
.u-loader svg{position:absolute;top:0;left:0}
.u-loader .u-head{translate:27px -30px;z-index:3;animation:u-bob 1s infinite ease-in}
.u-loader .u-bod{translate:0 30px;z-index:3;animation:u-bob 1s infinite ease-in-out}
.u-loader .u-legr{translate:75px 135px;z-index:0;animation:u-rstep 1s infinite ease-in;animation-delay:.45s}
.u-loader .u-legl{translate:30px 155px;z-index:3;animation:u-lstep 1s infinite ease-in}
@keyframes u-bob{0%{transform:translateY(0) rotate(3deg)}5%{transform:translateY(0) rotate(3deg)}25%{transform:translateY(5px) rotate(0)}50%{transform:translateY(0) rotate(-3deg)}70%{transform:translateY(5px) rotate(0)}100%{transform:translateY(0) rotate(3deg)}}
@keyframes u-lstep{0%{transform:translateY(0) rotate(-5deg)}33%{transform:translateY(-15px) translate(32px) rotate(35deg)}66%{transform:translateY(0) translate(25px) rotate(-25deg)}100%{transform:translateY(0) rotate(-5deg)}}
@keyframes u-rstep{0%{transform:translateY(0) translate(0) rotate(-5deg)}33%{transform:translateY(-10px) translate(30px) rotate(35deg)}66%{transform:translateY(0) translate(20px) rotate(-25deg)}100%{transform:translateY(0) translate(0) rotate(-5deg)}}
.u-loader #u-gnd{translate:-140px 0;rotate:10deg;z-index:-1;filter:blur(.5px) drop-shadow(1px 3px 5px #000);opacity:.35;animation:u-scroll 5s infinite linear}
@keyframes u-scroll{0%{transform:translateY(25px) translate(50px);opacity:0}33%{opacity:.35}66%{opacity:.35}to{transform:translateY(-50px) translate(-100px);opacity:0}}
.deep-title{font-size:16px;font-weight:900;color:var(--goldHi);letter-spacing:.6px;line-height:1.4;text-shadow:0 0 14px rgba(227,179,65,.45)}
.deep-sub{color:var(--mut);font-size:12px;margin-top:3px;line-height:1.5;font-weight:800}
.banner-info .dim{color:var(--mut)}
.deep-cancel{position:absolute;top:50%;right:10px;transform:translateY(-50%);font-size:13px}
#quoteStatus{margin-top:8px;font-size:12px;color:var(--mut);font-weight:800}
.modal-overlay{position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.7);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);display:flex;align-items:center;justify-content:center;padding:20px;opacity:0;visibility:hidden;transition:opacity .18s ease,visibility .18s}
.modal-overlay.open{opacity:1;visibility:visible}
.modal{position:relative;background:linear-gradient(180deg,#14100A,#0B0805 130%);border:1px solid var(--line2);border-radius:10px;box-shadow:0 24px 60px rgba(0,0,0,.6),0 0 40px rgba(227,179,65,.15);width:min(640px,94vw);max-height:86vh;overflow:auto;padding:24px 26px;transform:translateY(10px) scale(.985);transition:transform .18s ease}
.modal-overlay.open .modal{transform:translateY(0) scale(1)}
.modal::before{content:"";position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--gold),rgba(255,59,31,.6) 55%,transparent);box-shadow:0 0 20px rgba(227,179,65,.5)}
.m-head{display:flex;align-items:center;gap:10px;margin-bottom:12px;padding-right:44px}
.m-name{font-size:20px;font-weight:900;font-family:var(--font-serif);letter-spacing:.6px;color:var(--ink)}
.m-code{color:var(--mut);font-size:12px;font-weight:800}
.m-close{position:absolute;top:14px;right:14px}
.m-lights{font-size:20px;letter-spacing:3px;background:rgba(0,0,0,.35);border:1px solid var(--line2);border-radius:8px;padding:10px 12px;margin-bottom:14px;box-shadow:inset 0 0 20px rgba(227,179,65,.05)}
.m-body .row{font-size:13px}
@keyframes gridmove{to{background-position:46px 46px}}
@keyframes scan{0%{transform:translateY(-190px)}100%{transform:translateY(110vh)}}
@keyframes beam{0%,100%{background-position:0% 0}50%{background-position:100% 0}}
@keyframes logoPulse{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.35);opacity:.65}}
@keyframes quote-flicker{0%,100%{opacity:1}94%{opacity:1}95%{opacity:.4}96%{opacity:1}97%{opacity:.7}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}html{scroll-behavior:auto}}
@media (max-width:768px){
  .stats{grid-template-columns:repeat(2,1fr)}
  .panel{padding:18px 16px}
  .head-inner{padding:10px 14px}
  main{padding:18px 14px 40px}
  .actions{width:100%}
  .actions button{flex:1}
}
@media (max-width:640px){.cards{grid-template-columns:1fr}}
</style>
</head>
<body>
<div id="splash" class="splash" aria-hidden="true">
  <div class="bg-glow"></div>
  <div class="bg-noise"></div>
  <div class="dust-layer"></div>
  <div class="doodle-layer">
    <div class="doodle spark d1" style="top:15%;left:20%"></div>
    <div class="doodle scratch d2" style="top:75%;left:80%"></div>
    <div class="doodle dot d3" style="top:25%;left:75%"></div>
    <div class="doodle spark d4" style="top:80%;left:25%"></div>
    <div class="doodle scratch d5" style="top:45%;left:10%"></div>
    <div class="doodle dot d6" style="top:55%;left:90%"></div>
  </div>
  <div class="title-stage">
    <div class="neon-line top-line"><div class="neon-line-inner"></div></div>
    <div class="main-title">
      <span class="letter" data-char="T" style="--x:-8rem;--y:-7rem;--r:-45deg;--d:.1s">T</span>
      <span class="letter" data-char="H" style="--x:-4rem;--y:8rem;--r:30deg;--d:.4s">H</span>
      <span class="letter" data-char="I" style="--x:0rem;--y:-9rem;--r:-15deg;--d:.2s">I</span>
      <span class="letter" data-char="N" style="--x:4rem;--y:7rem;--r:60deg;--d:.6s">N</span>
      <span class="letter" data-char="K" style="--x:7rem;--y:-5rem;--r:-25deg;--d:.3s">K</span>
    </div>
    <div class="neon-line bottom-line"><div class="neon-line-inner"></div></div>
    <div class="sub-title">
      <span class="letter" data-char="S" style="--x:-6rem;--y:5rem;--r:20deg;--d:.8s">S</span>
      <span class="letter" data-char="E" style="--x:-4rem;--y:-4rem;--r:-30deg;--d:1.1s">E</span>
      <span class="letter" data-char="N" style="--x:-1rem;--y:6rem;--r:45deg;--d:.9s">N</span>
      <span class="letter" data-char="S" style="--x:2rem;--y:-6rem;--r:-15deg;--d:1.3s">S</span>
      <span class="letter" data-char="I" style="--x:4rem;--y:5rem;--r:35deg;--d:1.0s">I</span>
      <span class="letter" data-char="B" style="--x:6rem;--y:-5rem;--r:-40deg;--d:1.4s">B</span>
      <span class="letter" data-char="L" style="--x:8rem;--y:4rem;--r:10deg;--d:1.5s">L</span>
      <span class="letter" data-char="E" style="--x:9rem;--y:-3rem;--r:-20deg;--d:1.6s">E</span>
    </div>
  </div>
</div>
<div class="scan-band" aria-hidden="true"></div>
<a class="skip-link" href="#main">跳到主内容</a>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="u-spark" viewBox="0 0 24 24">
    <path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 0 0-2.456 2.456ZM16.894 20.567 16.5 21.75l-.394-1.183a2.25 2.25 0 0 0-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 0 0 1.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 0 0 1.423 1.423l1.183.394-1.183.394a2.25 2.25 0 0 0-1.423 1.423Z"></path>
  </symbol>
</svg>
<header>
  <div class="head-inner">
    <h1 class="brand"><span class="logo"><i class="g"></i><i class="y"></i><i class="r"></i></span>
      灯号监控 <span class="sub">自选股面板<span class="ver">v3.0</span></span></h1>
    <div class="actions">
      <button class="u-btn" onclick="window.open('/report')"><span class="u-label">查看报告</span></button>
      <button id="deepBtn" class="u-btn" onclick="deepRefresh()"><svg class="u-ico" aria-hidden="true"><use href="#u-spark"/></svg><span class="u-label">灯号分析</span></button>
      <button id="refreshBtn" class="u-btn" onclick="loadQuotes()"><svg class="u-ico" aria-hidden="true"><use href="#u-spark"/></svg><span class="u-label">刷新行情</span></button>
    </div>
  </div>
</header>
<main id="main">
  <div id="bannerWrap"></div>
  <div id="deepBanner" class="banner-info hidden" role="status" aria-live="polite">
    <div class="u-loader" id="uLoader" aria-hidden="true"></div>
    <div>
      <div class="deep-title">财富正顺着网线传过来</div>
      <div id="deepText" class="deep-sub">正在抓取灯号分析数据（约1~2分钟）…</div>
    </div>
    <button class="deep-cancel u-btn u-btn--icon u-btn--on-dark" onclick="cancelDeep()" aria-label="取消等待">×</button>
  </div>
  <section class="panel">
    <h2><span class="no">一</span>今日行情<span class="hint">实时 · 秒级刷新</span></h2>
    <div class="tbl-wrap"><table id="quoteTable"></table></div>
    <div id="quoteStatus"></div>
  </section>
  <div class="meta-line">
    <span>更新于 <b id="updated">--</b></span>
    <span>数据源 <b id="source">--</b></span>
    <span>共 <b id="count">0</b> 只</span>
  </div>
  <section class="stats" id="stats"></section>
  <section class="panel">
    <h2><span class="no">二</span>灯号总览<span class="hint">产业 · 基本面 · 估值 · 长期筹码 · 短期主力 · 边际变化</span></h2>
    <div class="tbl-wrap"><table id="matrix"></table></div>
  </section>
  <section class="panel">
    <h2><span class="no">三</span>个股明细<span class="hint">点击查看详情</span></h2>
    <div class="cards" id="cards"></div>
  </section>
  <section class="panel">
    <h2><span class="no">四</span>建仓清单<span class="hint">绿灯 ≥ 4 且无红灯</span></h2>
    <div id="buildable"><span class="warn-line">暂无。</span></div>
  </section>
  <section class="panel">
    <h2><span class="no">五</span>需要关注<span class="hint">红灯 ≥ 2</span></h2>
    <div id="watch"><span class="warn-line">暂无。</span></div>
  </section>
</main>
<div class="modal-overlay hidden" id="modal" role="dialog" aria-modal="true" aria-labelledby="mName" aria-hidden="true" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="m-head">
      <span class="m-name" id="mName"></span>
      <span class="m-code" id="mCode"></span>
      <span id="mBadge"></span>
      <button class="m-close u-btn u-btn--icon" id="mClose" onclick="closeModal()" aria-label="关闭详情">×</button>
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
  const statCls = {"good":"green","hot":"red","warn":"amber"};
  $("stats").innerHTML = [
    ["监控股票", stocks.length, ""],
    ["可建仓", buildable, buildable ? "good" : ""],
    ["需要关注", watch, watch ? "hot" : ""],
    ["数据提示", warns, warns ? "warn" : ""],
  ].map(([lbl,num,c]) => `<div class="stat ${c}"><div class="num ${statCls[c]||""}">${num}</div><div class="lbl">${lbl}</div></div>`).join("");

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

  const bHtml = buildable
    ? stocks.filter(x => x.buildable).map(x => `<div class="warn-line">→ ${esc(x.name)} ${esc(x.code)}${x.alert ? " · 状态变化 " + esc(x.alert) : ""}</div>`).join("")
    : '<span class="warn-line">暂无。</span>';
  $("buildable").innerHTML = bHtml;
  const wHtml = watch
    ? stocks.filter(x => x.red_count >= 2).map(x => `<div class="warn-line">→ ${esc(x.name)}（${x.red_count}红）：${esc(x.light_reasons ? Object.values(x.light_reasons).filter((_,i)=>x.lights && Object.values(x.lights)[i]==="red").join("，") : "")}</div>`).join("")
    : '<span class="warn-line">暂无。</span>';
  $("watch").innerHTML = wHtml;

  const banner = s.error ? `<div class="banner">抓取出错：${esc(s.error)}</div>` : "";
  $("bannerWrap").innerHTML = banner;
}

function cardTier(x){
  if (x.buildable) return "ok";
  if (x.red_count >= 2) return "bad";
  if (x.status === "接近可建仓") return "warn";
  return "";
}

function cardHtml(x, i){
  const lights = DIMS.map(d => LIGHT[x.lights[d[0]]] || "🟡").join("");
  const tier = cardTier(x);
  return `
  <div class="card${tier ? " card--" + tier : ""}" tabindex="0" role="button"
       aria-label="${esc(x.name)} ${esc(x.code)}，${lights}，涨跌${fmtPct(x.pct_chg)}，${badge(x).replace(/<[^>]+>/g,"")}"
       onclick="showDetail(${i})" onkeydown="cardKey(event,${i})">
    <div class="c-top">
      <span class="who">${esc(x.name)}<span class="cd">${esc(x.code)}</span></span>
      <span class="price ${cls(x.pct_chg)}">${x.price == null ? "--" : x.price.toFixed(2)}</span>
    </div>
    <div class="c-lights">${lights}</div>
    <div class="c-meta">
      <span class="${cls(x.pct_chg)}">${fmtPct(x.pct_chg)}</span>
      ${badge(x)}
    </div>
    ${x.light_changes ? `<div class="c-chg">灯号变化 · ${esc(x.light_changes)}</div>` : ""}
  </div>`;
}

function cardKey(e, i){
  if (e.key === "Enter" || e.key === " "){ e.preventDefault(); showDetail(i); }
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

let lastFocus = null;
let modalCloseTimer = null;

function showDetail(i){
  const x = STOCKS[i];
  if (!x) return;
  lastFocus = document.activeElement;
  $("mName").textContent = x.name;
  $("mCode").textContent = x.code;
  $("mBadge").innerHTML = badge(x);
  $("mLights").textContent = DIMS.map(d => LIGHT[x.lights[d[0]]] || "🟡").join("");
  $("mBody").innerHTML = detailRowsHtml(x);
  const ov = $("modal");
  clearTimeout(modalCloseTimer);
  ov.classList.remove("hidden");
  ov.setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
  requestAnimationFrame(() => requestAnimationFrame(() => ov.classList.add("open")));
  $("mClose").focus();
}

function closeModal(){
  const ov = $("modal");
  if (ov.classList.contains("hidden")) return;
  ov.classList.remove("open");
  ov.setAttribute("aria-hidden", "true");
  document.body.style.overflow = "";
  if (lastFocus && lastFocus.focus) lastFocus.focus();
  modalCloseTimer = setTimeout(() => ov.classList.add("hidden"), 180);
}

document.addEventListener("keydown", e => {
  if (e.key === "Escape"){ closeModal(); return; }
  const ov = $("modal");
  if (e.key === "Tab" && ov && !ov.classList.contains("hidden")){
    const focusables = ov.querySelectorAll('button, [href], input, [tabindex]:not([tabindex="-1"])');
    if (focusables.length){ e.preventDefault(); focusables[0].focus(); }
  }
});

function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }

async function ensureLoader(){
  const host = $("uLoader");
  if (!host || host.children.length) return;
  try {
    const r = await fetch("/static/kiwi-loader.html");
    if (r.ok) host.innerHTML = await r.text();
  } catch (e) {}
}

function showDeep(on){
  if (on) ensureLoader();
  $("deepBanner").classList.toggle("hidden", !on);
}

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
  const splashEl = $("splash");
  if (splashEl){
    const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced || location.search.indexOf("nosplash=1") !== -1){
      splashEl.remove();
    } else {
      setTimeout(() => {
        splashEl.classList.add("done");
        setTimeout(() => splashEl.remove(), 700);
      }, 3400);
    }
  }
  ensureLoader();
  if (location.search.indexOf("loader=1") !== -1) showDeep(true);
  loadQuotes();
  const s = await (await fetch("/api/status")).json();
  render(s);
  if (s.running || s.pending){
    busy = true;
    $("deepBtn").disabled = true;
    showDeep(true);
    pollDeep();
  }
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
        with socket.create_connection((HOST, PORT), timeout=0.5):
            print(f"面板已经在运行中（端口 {PORT} 已被占用），无需重复启动，直接刷新浏览器即可。")
            webbrowser.open(url)
            return
    except OSError:
        pass
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
