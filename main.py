# -*- coding: utf-8 -*-
"""灯号框架 · 自选股监控工具（命令行版）

用法:
    python main.py                 # 运行一次监控
    python main.py --init          # 生成默认 config.json 后退出
    python main.py --codes 002837  # 只看指定代码
    python main.py --color         # 强制开启终端颜色

更友好的打开方式：双击 启动面板.bat（浏览器可视化面板）。
"""

import argparse
import sys
from pathlib import Path

from lightmon.config import load_config
from lightmon.pipeline import run_pipeline
from lightmon.reporter import render_cli


def _setup_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> None:
    _setup_stdout()
    parser = argparse.ArgumentParser(description="灯号框架 · 自选股监控工具（命令行版）")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--init", action="store_true", help="生成默认配置文件并退出")
    parser.add_argument("--codes", default="", help="只监控指定代码，逗号分隔")
    parser.add_argument("--no-report", action="store_true", help="不生成 Markdown 报告")
    parser.add_argument("--no-history", action="store_true", help="不写入历史 CSV")
    parser.add_argument("--color", action="store_true", help="强制开启终端颜色")
    parser.add_argument("--max-workers", type=int, default=None, help="并发抓数线程数")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.init:
        cfg_path = Path(args.config)
        if cfg_path.exists():
            print(f"配置文件已存在，未覆盖: {cfg_path.resolve()}")
            print("如需重建默认模板，请先备份后删除该文件再运行 --init")
        else:
            print(f"已生成默认配置: {cfg_path.resolve()}")
            print("请编辑 watchlist（代码/名称/成本价/目标仓位/产业与边际备注）后运行 python main.py")
        return

    def progress(text: str) -> None:
        print(text)

    result = run_pipeline(cfg, codes=args.codes, max_workers=args.max_workers,
                          save_history=not args.no_history, save_report=not args.no_report,
                          progress=progress)
    if result.get("error"):
        print(result["error"])
        return
    if not args.no_report and result.get("report_path"):
        print(f"报告已保存: {result['report_path']}")
    if not args.no_history:
        print(f"历史已追加: {cfg['data']['history_file']}")

    use_color = args.color or sys.stdout.isatty()
    print("")
    print(render_cli(result["stocks"], cfg, result["meta"], color=use_color))


if __name__ == "__main__":
    main()
