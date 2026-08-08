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


def _log(line: str) -> None:
    with LOCK:
        STATE["log"] = (STATE["log"] + [line])[-30:]


def _start_refresh() -> bool:
    """统一入口：仅允许一条分析线程运行；运行中再来请求则排队，完成后自动再跑一轮。"""
    with LOCK:
        if STATE["running"] or STATE["pending"]:
            STATE["pending"] = True
            return False
        STATE.update(running=True, pending=False, done=False, error="", stocks=[],
                     meta={}, report_path="", log=[])
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
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
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
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            with LOCK:
                payload = {k: STATE[k] for k in ("running", "pending", "done", "error", "stocks", "meta", "report_path", "log")}
                payload["updated"] = STATE["meta"].get("run_time", "").strftime("%Y-%m-%d %H:%M:%S") \
                    if STATE["meta"].get("run_time") else ""
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
<title>灯号监控 · 折骨扇</title>
<style>
:root{
  --bg:#0E0C08; --panel:#17130C; --panel2:#14110A;
  --line:rgba(205,170,90,.22); --line2:rgba(205,170,90,.14);
  --ink:#EDE6D6; --muted:#A99E86; --dim:#8C8268;
  --gold:#C9A24B; --gold-hi:#E3C87E; --gold-deep:#A67C2E;
  --green:#8FC3A2; --yellow:#D9B45B; --red:#D0836F;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font-family:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.6}

header{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:13px 22px;background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:50}
.brand{display:flex;align-items:center;gap:10px;font-size:17px;font-weight:650;letter-spacing:.3px}
.brand .logo{display:flex;gap:3px}
.brand .logo i{width:8px;height:8px;border-radius:50%}
.brand .logo i.g{background:var(--green)} .brand .logo i.y{background:var(--yellow)} .brand .logo i.r{background:var(--red)}
.brand .sub{color:var(--muted);font-weight:400;font-size:13px}
.actions{display:flex;gap:8px;flex-wrap:wrap}
button{font:inherit;border:1px solid var(--line);border-radius:10px;padding:8px 16px;cursor:pointer;transition:all .15s;background:transparent;color:var(--ink)}
button:hover{border-color:var(--gold);color:var(--gold-hi)}
button.primary{background:linear-gradient(135deg,#D4B35E,#A67C2E);color:#221A0A;border-color:transparent;font-weight:600}
button.primary:hover{filter:brightness(1.08);color:#221A0A}
button:disabled{opacity:.45;cursor:default}

main{max-width:1180px;margin:0 auto;padding:20px 22px 40px}
.hidden{display:none!important}
.banner-info{display:flex;align-items:center;gap:10px;background:rgba(201,162,75,.08);border:1px solid var(--line);border-radius:12px;padding:10px 48px 10px 14px;margin-bottom:16px;color:var(--muted);position:relative}
.deep-cancel{position:absolute;top:50%;right:10px;transform:translateY(-50%);width:26px;height:26px;border-radius:50%;border:none;background:rgba(227,200,126,.12);color:var(--muted);cursor:pointer;font-size:14px;line-height:1;padding:0;display:flex;align-items:center;justify-content:center;transition:all .15s}
.deep-cancel:hover{background:rgba(227,200,126,.24);color:var(--ink)}
.spinner-sm{width:14px;height:14px;border:2px solid rgba(201,162,75,.25);border-top-color:var(--gold);border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
.meta-line{display:flex;gap:18px;color:var(--dim);font-size:12.5px;margin-bottom:14px;flex-wrap:wrap}

.fan-panel{background:linear-gradient(180deg,#16130C,#0F0D08);border:1px solid var(--line);border-radius:22px;padding:20px 22px 16px;margin-bottom:16px;box-shadow:0 18px 50px rgba(0,0,0,.45)}
.fan-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:2px}
.fan-head h2{font-size:17px;font-weight:650;letter-spacing:.5px}
.fan-head .hint{color:var(--muted);font-weight:400;font-size:12.5px;margin-left:8px}
.legend{display:flex;gap:16px;font-size:12px;color:var(--muted)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:-1px}
.search-row{display:flex;gap:10px;align-items:center;margin:12px 0 4px}
.search-row input{flex:1;max-width:300px;background:rgba(227,200,126,.05);border:1px solid var(--line);border-radius:999px;padding:7px 16px;color:var(--ink);font:inherit;outline:none}
.search-row input:focus{border-color:var(--gold)}
.search-row .count{color:var(--dim);font-size:12px}

.stage{position:relative;width:100%;max-width:660px;height:440px;margin:8px auto 0;background:radial-gradient(120% 90% at 50% 105%,#221D12 0%,#141108 60%,#0C0A06 100%);border-radius:22px;border:1px solid rgba(205,170,90,.26);box-shadow:0 24px 60px rgba(0,0,0,.55),inset 0 1px 0 rgba(227,200,126,.07);overflow:hidden;user-select:none}
.stage::after{content:'';position:absolute;inset:0;pointer-events:none;background:radial-gradient(60% 45% at 50% 78%,rgba(227,200,126,.05),transparent 70%)}
.stage svg{position:absolute;inset:0;width:100%;height:100%}
.names{position:absolute;inset:0}
.nm{position:absolute;writing-mode:vertical-rl;text-orientation:upright;font-family:"STKaiti","KaiTi","Microsoft YaHei",serif;font-size:14px;font-weight:600;letter-spacing:2px;padding:3px;border-radius:6px;line-height:1.25;cursor:pointer;transition:transform .18s ease,box-shadow .18s ease,text-shadow .18s ease;transform-origin:center}
.nm.g{color:var(--green);text-shadow:0 0 12px rgba(143,195,162,.28)}
.nm.y{color:var(--yellow);text-shadow:0 0 12px rgba(217,180,91,.28)}
.nm.r{color:var(--red);text-shadow:0 0 12px rgba(208,131,111,.26)}
.nm:hover{transform:scale(1.12);z-index:5;text-shadow:0 0 16px currentColor;background:rgba(227,200,126,.08);box-shadow:0 0 0 1px rgba(227,200,126,.16),0 8px 22px rgba(0,0,0,.4)}
.pages{display:flex;justify-content:center;gap:8px;margin:14px 0 4px;flex-wrap:wrap}
.pg{border:1px solid rgba(205,170,90,.3);background:rgba(227,200,126,.05);color:#CBBF9E;border-radius:999px;padding:6px 16px;font:inherit;font-size:13px;cursor:pointer;transition:all .18s ease}
.pg:hover{background:rgba(227,200,126,.12);border-color:rgba(205,170,90,.55);transform:translateY(-1px)}
.pg.on{background:linear-gradient(135deg,#D4B35E,#A67C2E);color:#221A0A;border-color:transparent;box-shadow:0 6px 18px rgba(180,140,60,.35)}
.page-note{width:100%;text-align:center;font-size:12px;color:var(--dim);margin-top:6px}

.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin-bottom:16px}
.panel h2{font-size:14.5px;font-weight:650;margin-bottom:12px;letter-spacing:.3px}
.panel h2 .hint{color:var(--muted);font-weight:400;font-size:12px;margin-left:8px}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 16px}
.stat .num{font-size:23px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.2}
.stat .num.green{color:var(--green)} .stat .num.red{color:var(--red)} .stat .num.amber{color:var(--yellow)}
.stat .lbl{color:var(--muted);font-size:12px;margin-top:2px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th{font-size:12px;color:var(--muted);font-weight:500;background:rgba(227,200,126,.05);text-align:left;padding:8px;border-bottom:1px solid var(--line2)}
td{padding:8px;border-bottom:1px solid rgba(205,170,90,.08)}
td .nm{font-weight:650}
td .cd{color:var(--dim);font-size:12px}
.up{color:var(--red)} .down{color:var(--green)} .flat{color:var(--muted)}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600;background:rgba(227,200,126,.1);color:var(--yellow)}
.badge.ok{background:rgba(143,195,162,.14);color:var(--green)}
.badge.bad{background:rgba(208,131,111,.14);color:var(--red)}
.warn-line{color:var(--yellow);font-size:13px;padding:3px 0}
.row{display:flex;justify-content:space-between;gap:14px;padding:6px 0;border-bottom:1px dashed rgba(205,170,90,.12);font-size:13px}
.row .lbl{color:var(--muted);flex-shrink:0} .row .val{text-align:right;font-variant-numeric:tabular-nums;word-break:break-all}
.add-form{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.add-form input{flex:1;min-width:110px;border:1px solid var(--line2);border-radius:10px;padding:8px 10px;font:inherit;color:var(--ink);background:rgba(227,200,126,.04);outline:none}
.add-form input:focus{border-color:var(--gold)}
.wl-item{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 12px;border:1px solid var(--line2);border-radius:10px;margin-bottom:6px;background:rgba(227,200,126,.03);font-size:13px}
.wl-item .code{font-weight:650;font-variant-numeric:tabular-nums}
.wl-item .meta{color:var(--dim);font-size:12px}
.wl-item .del{border:none;background:transparent;color:var(--dim);cursor:pointer;font-size:14px;padding:4px 6px}
.wl-item .del:hover{color:var(--red)}
.note{font-size:12px;color:var(--muted);background:rgba(227,200,126,.05);border-radius:8px;padding:8px 10px;margin-top:8px}
footer{text-align:center;color:var(--dim);font-size:12px;padding:20px}

.modal-overlay{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;background:rgba(12,10,6,.6);backdrop-filter:blur(6px);padding:20px}
.modal{position:relative;width:min(560px,94vw);max-height:88vh;overflow:auto;background:rgba(28,23,14,.97);border:1px solid rgba(205,170,90,.28);border-radius:20px;padding:22px;color:#E9E1CD;box-shadow:0 30px 80px rgba(0,0,0,.75);backdrop-filter:blur(20px);animation:pop .22s cubic-bezier(.2,.9,.3,1.2)}
@keyframes pop{from{transform:scale(.92) translateY(8px);opacity:0}to{transform:none;opacity:1}}
.m-head{display:flex;align-items:center;gap:10px;margin-bottom:12px;padding-right:36px}
.m-name{font-size:19px;font-weight:700;color:#F0E9D6}
.m-code{font-size:12.5px;color:var(--dim)}
.m-close{position:absolute;top:14px;right:14px;border:none;background:rgba(227,200,126,.1);color:var(--muted);width:30px;height:30px;border-radius:50%;cursor:pointer;font-size:16px;line-height:1;padding:0;display:flex;align-items:center;justify-content:center;transition:all .15s}
.m-close:hover{background:rgba(227,200,126,.2);color:#F0E9D6}
.m-lights{font-size:22px;letter-spacing:4px;background:rgba(227,200,126,.06);border-radius:12px;padding:9px 12px;margin-bottom:14px}
.m-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px 18px;margin-bottom:12px}
@media(max-width:560px){.m-grid{grid-template-columns:1fr}}
.m-note{font-size:12.5px;line-height:1.75;color:#D8CFB8;background:rgba(227,200,126,.05);border-left:3px solid var(--gold);border-radius:0 10px 10px 0;padding:10px 13px;margin-bottom:12px}
.m-reasons{font-size:12px;color:var(--muted)}
.m-reasons div{padding:3px 0;border-bottom:1px dashed rgba(205,170,90,.12)}
.m-reasons b{color:var(--ink);font-weight:600}
</style>
</head>
<body>
<header>
  <div class="brand"><span class="logo"><i class="g"></i><i class="y"></i><i class="r"></i></span>
    灯号监控 <span class="sub">折骨扇 · 自选股面板</span></div>
  <div class="actions">
    <button class="ghost" onclick="window.open('/report')">查看报告</button>
    <button id="deepBtn" class="ghost" onclick="deepRefresh()">灯号分析</button>
    <button id="refreshBtn" class="primary" onclick="loadQuotes()">刷新行情</button>
  </div>
</header>
<main>
  <div id="bannerWrap"></div>
  <div id="deepBanner" class="banner-info hidden">
    <span class="spinner-sm"></span>
    <span id="deepText">正在抓取灯号分析数据（约1~2分钟）…</span>
    <button class="deep-cancel" id="deepCancel" onclick="cancelDeep()" title="取消等待">×</button>
  </div>

  <section class="fan-panel">
    <div class="fan-head">
      <h2>折骨扇<span class="hint">每根折痕一只股票 · 10 只一面 · 点击看详情</span></h2>
      <div class="legend">
        <span><i style="background:var(--green)"></i>建仓</span>
        <span><i style="background:var(--yellow)"></i>观察</span>
        <span><i style="background:var(--red)"></i>危险</span>
      </div>
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
  </section>

  <div class="meta-line">
    <span>分析更新于 <b id="updated">--</b></span>
    <span>数据源 <b id="source">--</b></span>
  </div>
  <section class="stats" id="stats"></section>

  <div class="two-col">
    <section class="panel">
      <h2>自选股管理<span class="hint">添加后自动跑灯号分析</span></h2>
      <div class="add-form">
        <input id="wlCode" placeholder="代码，如 600519" maxlength="8">
        <input id="wlName" placeholder="名称（可留空）">
        <input id="wlCost" placeholder="成本价" type="number" step="0.01" min="0">
        <input id="wlWeight" placeholder="目标仓位%" type="number" step="0.5" min="0">
        <button class="primary" onclick="addStock()">＋ 添加</button>
      </div>
      <div id="wlMsg"></div>
      <div id="wlList"></div>
    </section>
    <section class="panel">
      <h2>今日行情<span class="hint">实时</span></h2>
      <div class="tbl-wrap"><table id="quoteTable"></table></div>
      <div id="quoteStatus" class="note" style="margin-top:8px"></div>
    </section>
  </div>

  <section class="panel">
    <h2>建仓清单<span class="hint">绿灯 ≥ 4 且无红灯</span></h2>
    <div id="buildable"><span class="warn-line">暂无。</span></div>
  </section>
  <section class="panel">
    <h2>需要关注<span class="hint">红灯 ≥ 2</span></h2>
    <div id="watch"><span class="warn-line">暂无。</span></div>
  </section>
</main>
<footer>数据来自公开接口，仅供个人研究参考，不构成投资建议 · 灯号框架</footer>

<div class="modal-overlay" id="modal" hidden onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="m-head">
      <span class="m-name" id="mName"></span>
      <span class="m-code" id="mCode"></span>
      <span class="badge" id="mBadge"></span>
      <button class="m-close" onclick="closeModal()">×</button>
    </div>
    <div class="m-lights" id="mLights"></div>
    <div class="m-grid" id="mGrid"></div>
    <div class="m-note" id="mNote"></div>
    <div class="m-reasons" id="mReasons"></div>
  </div>
</div>

<script>
const DIMS = [["industry","产业"],["fundamental","基本面"],["valuation","估值"],["chips","筹码"],["capital","主力"],["margin","边际"]];
const LIGHT = {green:"🟢", yellow:"🟡", red:"🔴"};
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtWan = v => { if (v == null) return "--"; const s = Math.abs(v) >= 10000 ? (v/10000).toFixed(2) + "亿" : Math.round(v).toLocaleString("zh-CN") + "万"; return (v > 0 ? "+" : "") + s; };
const fmtPct = (v,d=2) => v == null ? "--" : (v > 0 ? "+" : "") + v.toFixed(d) + "%";
const cls = v => v > 0 ? "up" : (v < 0 ? "down" : "flat");
const $ = id => document.getElementById(id);
let busy = false;
let pollCancelled = false;
let STOCKS = [];
let fanPage = 0;
const PAGE_N = 10;

function tierOf(s){ if (s.buildable) return "g"; if (s.red_count >= 2) return "r"; return "y"; }
const TIER = { g:{label:"建仓",cls:"ok"}, y:{label:"观察",cls:""}, r:{label:"危险",cls:"bad"} };

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
    return `<div class="nm ${t}" style="left:${x}px;top:${y}px;transform:translate(-50%,-50%) rotate(${a}deg)" onclick="showModal(${STOCKS.indexOf(s)})">${esc(s.name)}</div>`;
  }).join("");
  $("pages").innerHTML =
    `<button class="pg" onclick="fanGo(-1)">‹</button>` +
    Array.from({length:pages},(_,i)=>`<button class="pg ${i===fanPage?'on':''}" onclick="fanGoTo(${i})">扇面 ${i+1}</button>`).join("") +
    `<button class="pg" onclick="fanGo(1)">›</button>` +
    `<span class="page-note">每面最多 ${PAGE_N} 只 · 当前第 ${fanPage+1}/${pages} 面</span>`;
}
function fanGo(d){ const items=filteredStocks(); const pages=Math.ceil(items.length/PAGE_N)||1; fanPage=Math.max(0,Math.min(pages-1,fanPage+d)); drawFan(); }
function fanGoTo(i){ fanPage=i; drawFan(); }

/* ---------- 弹窗 ---------- */
function showModal(idx){
  const s = STOCKS[idx]; if (!s) return;
  $("mName").textContent = s.name;
  $("mCode").textContent = s.code;
  const t = tierOf(s);
  const b = $("mBadge");
  b.textContent = TIER[t].label + "区";
  b.className = "badge " + TIER[t].cls;
  $("mLights").textContent = DIMS.map(d => LIGHT[s.lights[d[0]]] || "🟡").join("");
  const grid = [];
  grid.push(["PE(TTM)", s.pe_ttm == null ? "--" : s.pe_ttm.toFixed(1) + " 倍" + (s.pe_pct != null ? " · 分位 " + s.pe_pct.toFixed(0) + "%" : "")]);
  grid.push(["PB", s.pb == null ? "--" : s.pb.toFixed(2) + " 倍" + (s.pb_pct != null ? " · 分位 " + s.pb_pct.toFixed(0) + "%" : "")]);
  grid.push(["主力 1 日", fmtWan(s.flow_1d_wan)]);
  grid.push(["主力 5 日", fmtWan(s.flow_5d_wan)]);
  grid.push(["主力 20 日", fmtWan(s.flow_20d_wan)]);
  grid.push(["档位阈值", s.threshold_wan == null ? "--" : (s.threshold_wan/10000).toFixed(0) + " 亿 · " + (s.tier_name||"")]);
  grid.push(["机构持仓", s.inst_count == null ? "数据缺失" : s.inst_count + " 家" + (s.inst_count_chg != null ? "（环比 " + (s.inst_count_chg>0?"+":"") + s.inst_count_chg + "）" : "")]);
  grid.push(["占流通股", s.inst_ratio_pct == null ? "--" : s.inst_ratio_pct.toFixed(2) + "%" + (s.inst_ratio_chg != null ? "（环比 " + (s.inst_ratio_chg>0?"+":"") + s.inst_ratio_chg.toFixed(2) + "pp）" : "")]);
  grid.push(["52 周位置", s.pos52_pct == null ? "--" : s.pos52_pct.toFixed(0) + "%"]);
  grid.push(["距高点 / 低点", (s.dist_high_pct == null ? "--" : fmtPct(s.dist_high_pct,1)) + " / " + (s.dist_low_pct == null ? "--" : fmtPct(s.dist_low_pct,1))]);
  if (s.report_date) grid.push(["最新财报", "营收 " + fmtPct(s.rev_yoy,1) + " · 净利 " + fmtPct(s.profit_yoy,1) + "（" + esc(s.report_date) + "）"]);
  if (s.cost != null) grid.push(["我的仓位", "成本 " + s.cost.toFixed(2) + " · 盈亏 " + fmtPct(s.pnl_pct) + (s.target_weight != null ? " · 目标 " + s.target_weight + "%" : "")]);
  $("mGrid").innerHTML = grid.map(([l,v]) => `<div class="row"><span class="lbl">${l}</span><span class="val">${v}</span></div>`).join("");
  $("mNote").textContent = s.analysis_text || "暂无分析数据。";
  const rs = DIMS.map(d => s.light_reasons && s.light_reasons[d[0]] ? `<div><b>${d[1]}</b> ${esc(s.light_reasons[d[0]])}</div>` : "").filter(Boolean).join("");
  $("mReasons").innerHTML = rs ? '<div style="color:var(--dim);font-size:11px;margin-bottom:4px">灯号依据</div>' + rs : "";
  $("modal").hidden = false;
}
function closeModal(){ $("modal").hidden = true; }
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });
$("fanSearch").addEventListener("input", () => { fanPage = 0; drawFan(); });

/* ---------- 统计与清单 ---------- */
function render(s){
  STOCKS = s.stocks || [];
  const stocks = STOCKS;
  $("updated").textContent = s.updated || "--";
  $("source").textContent = s.data_sources || "--";
  const buildable = stocks.filter(x => x.buildable).length;
  const watch = stocks.filter(x => x.red_count >= 2).length;
  const warns = stocks.reduce((n,x) => n + (x.warnings||[]).length, 0);
  $("stats").innerHTML = [
    ["监控股票", stocks.length, ""],
    ["可建仓", buildable, buildable ? "green" : ""],
    ["需要关注", watch, watch ? "red" : ""],
    ["数据提示", warns, warns ? "amber" : ""],
  ].map(([lbl,num,c]) => `<div class="stat"><div class="num ${c}">${num}</div><div class="lbl">${lbl}</div></div>`).join("");
  drawFan();
  $("buildable").innerHTML = buildable ? stocks.filter(x => x.buildable).map(x => `<div class="warn-line">→ ${esc(x.name)} ${esc(x.code)}${x.alert ? " · 🔥 " + esc(x.alert) : ""}</div>`).join("") : '<span class="warn-line">暂无。</span>';
  $("watch").innerHTML = watch ? stocks.filter(x => x.red_count >= 2).map(x => {
    const reds = DIMS.filter(d => x.lights[d[0]] === "red").map(d => d[1]).join("，");
    return `<div class="warn-line">→ ${esc(x.name)}（${x.red_count}红）：${reds ? esc(reds) : "多维度红灯"}</div>`;
  }).join("") : '<span class="warn-line">暂无。</span>';
  $("bannerWrap").innerHTML = s.error ? `<div class="banner" style="background:rgba(208,131,111,.1);border:1px solid rgba(208,131,111,.3);border-radius:12px;padding:10px 14px;margin-bottom:14px;color:var(--red)">抓取出错：${esc(s.error)}</div>` : "";
}

/* ---------- 行情 / 自选股 / 深度分析 ---------- */
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }
function showDeep(on){ $("deepBanner").classList.toggle("hidden", !on); }
function fmtAmount(v){ if (v == null) return "--"; return v >= 1e8 ? (v/1e8).toFixed(2) + "亿" : (v/1e4).toFixed(0) + "万"; }
function renderQuotes(q){
  const rows = (q.quotes || []).map(x => `
    <tr>
      <td><span class="nm">${esc(x.name)}</span> <span class="cd">${esc(x.code)}</span></td>
      <td class="${cls(x.pct_chg)}">${x.price == null ? "--" : x.price.toFixed(2)}</td>
      <td class="${cls(x.pct_chg)}">${fmtPct(x.pct_chg)}</td>
      <td>${fmtAmount(x.amount)}</td>
      <td>${x.turnover_rate == null ? "--" : x.turnover_rate.toFixed(2) + "%"}</td>
      <td>${x.mv_yi == null ? "--" : Math.round(x.mv_yi) + "亿"}</td>
    </tr>`).join("");
  $("quoteTable").innerHTML = `<tr><th>股票</th><th>最新价</th><th>涨跌幅</th><th>成交额</th><th>换手率</th><th>总市值</th></tr>` + rows;
  if (!(q.quotes && q.quotes.length)) $("quoteStatus").textContent = "行情加载失败，或自选股暂无行情";
}
async function loadQuotes(){
  $("quoteStatus").textContent = "行情加载中…";
  try { renderQuotes(await (await fetch("/api/quote")).json()); }
  catch (e){ $("quoteStatus").textContent = "行情加载失败：" + e; }
}
async function loadWatchlist(){
  const r = await (await fetch("/api/watchlist")).json();
  const list = r.watchlist || [];
  $("wlList").innerHTML = list.length ? list.map(w => `
    <div class="wl-item">
      <span class="info"><span class="code">${esc(w.code)}</span> <span>${esc(w.name || "")}</span>
      <span class="meta">成本 ${w.cost == null ? "--" : w.cost} · 仓位 ${w.target_weight == null ? "--" : w.target_weight}%</span></span>
      <button class="del" title="移出自选股" onclick="removeStock('${esc(w.code)}')">✕</button>
    </div>`).join("") : '<span class="warn-line">尚未添加自选股。</span>';
}
async function addStock(){
  const code = $("wlCode").value.trim();
  if (!code){ $("wlMsg").textContent = "请填写股票代码"; return; }
  const body = { code, name: $("wlName").value.trim(), cost: $("wlCost").value, target_weight: $("wlWeight").value };
  const res = await (await fetch("/api/watchlist/add", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body) })).json();
  if (res.error){ $("wlMsg").textContent = res.error; return; }
  $("wlMsg").textContent = `已添加 ${code}，正在抓取灯号分析数据（约1~2分钟），稍后即可看到分析结果…`;
  ["wlCode","wlName","wlCost","wlWeight"].forEach(id => $(id).value = "");
  await loadWatchlist(); await loadQuotes(); deepRefresh();
}
async function removeStock(code){
  if (!confirm(`确定把 ${code} 移出自选股？`)) return;
  const res = await (await fetch("/api/watchlist/remove", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({code}) })).json();
  $("wlMsg").textContent = res.error || `已移除 ${code}，正在重新分析…`;
  await loadWatchlist(); await loadQuotes(); deepRefresh();
}
async function pollDeep(){
  pollCancelled = false;
  for (;;){
    await sleep(2000);
    if (pollCancelled) return;
    const s = await (await fetch("/api/status")).json();
    const last = (s.log || []).slice(-1)[0] || "";
    $("deepText").textContent = "正在抓取灯号分析数据（约1~2分钟）… " + last;
    if (!s.running && !s.pending){ busy = false; $("deepBtn").disabled = false; showDeep(false); render(s); return; }
  }
}
async function deepRefresh(){
  pollCancelled = false;
  await fetch("/api/refresh", {method:"POST"});
  if (!busy){ busy = true; $("deepBtn").disabled = true; showDeep(true); $("deepText").textContent = "正在启动灯号分析…"; pollDeep(); }
}
function cancelDeep(){
  pollCancelled = true;
  busy = false;
  $("deepBtn").disabled = false;
  showDeep(false);
  $("quoteStatus").textContent = "已取消等待。分析在后台继续，完成后点「灯号分析」查看结果。";
}
window.addEventListener("load", async () => {
  loadQuotes(); loadWatchlist();
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
