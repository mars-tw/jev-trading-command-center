# -*- coding: utf-8 -*-
"""JEV Trading Command Center 主入口。預設就是零金鑰、零網路的離線 DEMO。

常用指令：
  python -B run_app.py                        # DEMO（預設）：http://127.0.0.1:8775/
  python -B run_app.py --demo --port 8775     # 明確指定 DEMO 與連接埠
  python -B run_app.py --check                # 不開服務，只印一個合成 frame 摘要
  python -B run_app.py --real-paper --port 8765
      # 真實公開行情 + 紙上交易（需要 TYPESAFE_API_KEY 環境變數；需要 requests）

安全界線：
- DEMO 路徑在匯入任何引擎／行情／原生 CLI 模組之前就分流，因此不可能連線、
  不讀金鑰、不啟動任何子行程，所有資金數字都是合成的。
- --real-paper 仍然只走 PaperBroker（模擬成交），沒有交易所私有下單 API。
- 原生 CLI 伴跑程式是另一支程式（run_workguild.py）且必須另外明示同意。
"""
import argparse
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DEMO_PORT = 8775
DEFAULT_PAPER_PORT = 8765


def build_parser():
    ap = argparse.ArgumentParser(
        prog="run_app.py",
        description="JEV Trading Command Center：預設離線 DEMO（合成資料、零金鑰、零網路）")
    ap.add_argument("--demo", action="store_true",
                    help="離線 DEMO（預設行為）：合成資料、不連線、不呼叫任何模型")
    ap.add_argument("--real-paper", action="store_true",
                    help="真實公開行情 + 紙上交易（手動啟用；需要 TYPESAFE_API_KEY 與 requests）")
    ap.add_argument("--port", type=int, default=None,
                    help="HTTP 連接埠（DEMO 預設 %d，紙上交易預設 %d）"
                         % (DEFAULT_DEMO_PORT, DEFAULT_PAPER_PORT))
    ap.add_argument("--scenario", default="auto", choices=("auto", "success", "wait", "risk_reject"),
                    help="DEMO 起始情境；auto 會自動輪播 PASS / WAIT / 風控拒絕")
    ap.add_argument("--seed", type=int, default=20261019, help="DEMO 合成種子（可重現）")
    ap.add_argument("--check", action="store_true",
                    help="只做自我檢查：印出一個合成 frame 摘要就結束，不開服務")
    ap.add_argument("--config", default=os.path.join(BASE, "config.json"),
                    help="紙上交易模式的設定檔（可選）")
    ap.add_argument("--native-companion", action="store_true",
                    help="僅紙上交易模式：告知前端已另行手動啟動原生 CLI 伴跑程式（不會由本程式啟動）")
    return ap


def run_demo(args):
    """DEMO 分流：這裡只匯入合成模組，絕不觸碰引擎／行情／金鑰。"""
    sys.path.insert(0, BASE)
    from scanner.demo_engine import DemoEngine
    from scanner import demo_server

    port = args.port or DEFAULT_DEMO_PORT
    engine = DemoEngine(seed=args.seed, port=port, scenario=args.scenario)
    if args.check:
        frame = engine.status()
        health = engine.health()
        print("DEMO 自我檢查：mode=%s synthetic=%s network_allowed=%s native_cli_count=%d "
              "jev_api_called=%s" % (health["mode"], health["synthetic"],
                                     health["network_allowed"], health["native_cli_count"],
                                     health["jev_api_called"]))
        print("frame：scan=%s 決策=%s 標的=%s 列數=%d 事件=%d 寵物=%d"
              % (frame["scan_count"], frame["decisions"][0]["status"],
                 frame["decisions"][0]["symbol"], len(frame["live_rows"]),
                 len(frame["events"]), len(engine.guild()["workers"])))
        return 0
    demo_server.serve(BASE, port, engine=engine)
    return 0


def run_real_paper(args):
    """真實公開行情 + 紙上交易：缺金鑰直接拒絕（fail-closed），不做任何連線。"""
    if not (os.environ.get("TYPESAFE_API_KEY") or "").strip():
        print("拒絕啟動：--real-paper 需要環境變數 TYPESAFE_API_KEY（只接受環境變數）。")
        print("沒有金鑰時請用預設的離線 DEMO：python -B run_app.py --demo --port %d"
              % DEFAULT_DEMO_PORT)
        return 2
    sys.path.insert(0, BASE)
    try:
        from scanner.config import load_config
        from scanner.engine import ScanEngine
        from scanner.server import serve
    except ImportError:
        print("缺少正式模式依賴（requests）。請先安裝：pip install -r requirements.txt")
        print("離線 DEMO 不需要任何安裝：python -B run_app.py --demo")
        return 2
    port = args.port or DEFAULT_PAPER_PORT
    cfg = load_config(args.config if os.path.exists(args.config) else None)
    engine = ScanEngine(cfg, BASE)
    print("紙上交易模式：所有成交都由 PaperBroker 模擬，沒有交易所私有下單 API。")
    serve(engine, cfg, BASE, port, native_optin=bool(args.native_companion))
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.demo and args.real_paper:
        print("--demo 與 --real-paper 不能同時指定。")
        return 2
    if args.port is not None and not (1024 <= args.port <= 65535):
        print("--port 必須在 1024-65535。")
        return 2
    if args.real_paper:
        return run_real_paper(args)
    return run_demo(args)


if __name__ == "__main__":
    sys.exit(main())
