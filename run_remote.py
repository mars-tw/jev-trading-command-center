# -*- coding: utf-8 -*-
"""遠端存取入口：普通公開 HTTPS 反向代理 + 本機應用程式帳號密碼登入。

常用指令：
  python -B run_remote.py --setup-owner --auth-store "C:/Users/<你>/jev-secrets/owner.json"
      # 第一次用：只在本機 127.0.0.1:8778 開一個表單，輸入帳號密碼建立存放區。
      # 這個模式「不需要」--public-origin。

  python -B run_remote.py --public-origin https://public.example.invalid \
      --auth-store "C:/Users/<你>/jev-secrets/owner.json"
      # 正式（密碼模式）：只綁 127.0.0.1:8777，預設自帶離線 DEMO 資料。

  python -B run_remote.py --public-origin https://public.example.invalid \
      --auth-store ... --upstream http://127.0.0.1:8765 --companion http://127.0.0.1:8766
      # 唯讀真實模式：只讀四條固定 GET，不轉送任何財務／排程 POST。

安全界線：
- 只綁 127.0.0.1。TLS、公開 DNS 與公開連接埠都交給同一台主機上的受信任
  HTTPS 反向代理；根使用者之後自己選實際的代理與網域。程式與文件只用
  https://public.example.invalid 這個保留示範來源，沒有任何真實 DNS。
- --public-origin 與 --auth-store 都是必填；缺少、空字串、畸形，或存放區
  落在版本庫裡面，一律啟動失敗（fail-closed）。
- 沒有公開註冊端點、沒有預設帳號、不會自動建立擁有者。
- Tailscale / X-Forwarded-User / Forwarded 之類的身分標頭永遠被忽略，
  在密碼模式下絕對不能用來認證。
- 不讀真實 TLS／Tailscale／供應商設定，也不讀任何金鑰環境變數。
"""
import argparse
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8777
DEFAULT_SETUP_PORT = 8778
EXAMPLE_ORIGIN = "https://public.example.invalid"


def build_parser():
    ap = argparse.ArgumentParser(
        prog="run_remote.py",
        description="JEV 遠端存取：公開 HTTPS 反向代理 + 應用程式帳號密碼（預設離線 DEMO 資料）")
    ap.add_argument("--public-origin", default=None,
                    help="公開 HTTPS 來源，完全比對，例如 https://public.example.invalid"
                         "（不可含路徑／查詢／片段／使用者資訊）")
    ap.add_argument("--auth-store", default=None,
                    help="帳號存放區的明確路徑，必須在版本庫之外（只存雜湊）")
    ap.add_argument("--port", type=int, default=None,
                    help="loopback 連接埠（預設 %d）；永遠只綁 127.0.0.1"
                         % DEFAULT_PORT)
    ap.add_argument("--upstream", default=None,
                    help="可選，必須剛好是 http://127.0.0.1:8765（唯讀真實模式）")
    ap.add_argument("--companion", default=None,
                    help="可選，必須剛好是 http://127.0.0.1:8766（唯讀真實模式）")
    ap.add_argument("--market-analysis", action="store_true",
                    help="僅唯讀真實模式：明確啟用公開市場的多週期研究資料；DEMO 不會連網")
    ap.add_argument("--enable-antigravity", action="store_true",
                    help="只顯示 AGY 研究連接器狀態與白名單脈絡；不會啟動原生程式或模型")
    ap.add_argument("--setup-owner", action="store_true",
                    help="本機初始擁有者設定；不需要 --public-origin，只在 127.0.0.1 開表單")
    ap.add_argument("--setup-port", type=int, default=None,
                    help="設定模式的 loopback 連接埠（預設 %d）"
                         % DEFAULT_SETUP_PORT)
    ap.add_argument("--terminal", action="store_true",
                    help="僅設定模式：改用終端機 getpass 輸入（不回顯、不印出密碼）")
    return ap


def _gateway():
    sys.path.insert(0, BASE)
    from scanner import remote_gateway
    return remote_gateway


def _setup():
    sys.path.insert(0, BASE)
    from scanner import remote_setup
    return remote_setup


def run_setup(args):
    """本機設定模式：不需要公開來源，而且只有存放區不存在時才會啟動。"""
    remote_setup = _setup()
    if not (args.auth_store or "").strip():
        print("拒絕啟動：--setup-owner 需要 --auth-store <版本庫之外的明確路徑>。")
        return 2
    port = args.setup_port if args.setup_port is not None else DEFAULT_SETUP_PORT
    if port != 0 and not (1024 <= port <= 65535):
        print("--setup-port 必須在 1024-65535。")
        return 2
    if args.terminal:
        try:
            return remote_setup.terminal_setup(args.auth_store)
        except Exception as exc:                        # 只印固定代碼，不印路徑或密碼
            print("拒絕啟動：%s" % _code(exc))
            return 2
    try:
        return remote_setup.serve(args.auth_store, port=port)
    except Exception as exc:
        print("拒絕啟動：%s（沒有建立任何檔案）。" % _code(exc))
        return 2


def run_gateway(args):
    """密碼模式：缺少公開來源或帳號存放區都直接 fail-closed。"""
    if not (args.public_origin or "").strip():
        print("拒絕啟動：密碼模式必須指定 --public-origin"
              " <完全比對的 HTTPS 來源，例如 %s>。" % EXAMPLE_ORIGIN)
        return 2
    if not (args.auth_store or "").strip():
        print("拒絕啟動：密碼模式必須指定 --auth-store <版本庫之外的明確路徑>。")
        return 2
    gateway = _gateway()
    port = args.port if args.port is not None else DEFAULT_PORT
    try:
        feature_options = {}
        if getattr(args, "market_analysis", False):
            feature_options["market_analysis"] = True
        if getattr(args, "enable_antigravity", False):
            feature_options["enable_antigravity"] = True
        config = gateway.GatewayConfig(
            args.public_origin, args.auth_store, port=port,
            upstream=args.upstream, companion=args.companion,
            **feature_options)
    except gateway.ConfigError as exc:
        print("拒絕啟動：%s" % exc.code)
        return 2
    try:
        return gateway.serve(config, webroot=os.path.join(BASE, "web"))
    except gateway.ConfigError as exc:
        if exc.code == "auth_store_missing":
            print("拒絕啟動：auth_store_missing。請先在本機跑一次："
                  "python -B run_remote.py --setup-owner --auth-store <路徑>")
        else:
            print("拒絕啟動：%s" % exc.code)
        return 2


def _code(exc):
    return getattr(exc, "code", exc.__class__.__name__)


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.setup_owner:
        return run_setup(args)
    if args.terminal:
        print("--terminal 只能和 --setup-owner 一起用。")
        return 2
    return run_gateway(args)


if __name__ == "__main__":
    sys.exit(main())
