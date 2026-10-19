# -*- coding: utf-8 -*-
"""啟動工作公會 sidecar（獨立行程，與瀏覽器／對話無關）。

用法（必須明示 --enable-native-cli；預設不探測、不連線、不啟動 CLI）：
  python -B run_workguild.py --enable-native-cli --port 8766 --source-url http://127.0.0.1:8765 --capacity 8
  python -B run_workguild.py --enable-native-cli --once                 # 只跑一輪（有界）就收工
  python -B run_workguild.py --enable-native-cli --cycle-seconds 60     # 最短 60 秒（測試才會更低）

安全界線：只讀 127.0.0.1:8765 的公開狀態，只寫自己的 SQLite 帳本與自有暫存目錄；
不碰任何交易／帳戶／JEV 物件，不繼承金鑰環境變數，不刪除任何檔案（只回收自有暫存）。
重複啟動時會查既有實例的 /api/health 後直接退出，不殺任何行程。
"""
import argparse
import json
import os
import signal
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scanner import agent_cli, agent_guild as guild  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "output", "workguild", "workguild.sqlite")


def default_temp_root():
    """自有執行時暫存根目錄（短路徑、本機磁碟，方便安全回收）。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.path.expanduser("~")
    return os.path.join(base, "Temp", "workguild-runtime")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="工作公會 sidecar")
    ap.add_argument("--enable-native-cli", action="store_true", help="Explicitly opt into native provider CLI calls")
    ap.add_argument("--port", type=int, default=guild.DEFAULT_PORT)
    ap.add_argument("--source-url", default=guild.DEFAULT_SOURCE_URL)
    ap.add_argument("--capacity", type=int, default=guild.DEFAULT_CAPACITY,
                    choices=list(guild.CAPACITY_CHOICES))
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--cycle-seconds", type=int, default=guild.DEFAULT_CYCLE_SECONDS)
    ap.add_argument("--temp-dir", default=None)
    ap.add_argument("--timeout-seconds", type=int, default=agent_cli.DEFAULT_TIMEOUT_SECONDS)
    ap.add_argument("--once", action="store_true", help="只跑一輪（有界）就結束")
    args = ap.parse_args(argv)
    if args.cycle_seconds < guild.DEFAULT_CYCLE_SECONDS:
        # 正式啟動不接受低於 60 秒；測試才會用更低的值（直接呼叫排程器）
        args.cycle_seconds = guild.DEFAULT_CYCLE_SECONDS
    if not (1024 <= args.port <= 65535):
        ap.error("port 必須在 1024-65535")
    if not (10 <= args.timeout_seconds <= 600):
        ap.error("timeout-seconds 必須在 10-600")
    return args


def probe_existing(port, timeout=2.0):
    """查既有實例的健康狀態；查不到回 None。永不殺任何行程。"""
    try:
        from urllib.request import Request, urlopen
        req = Request("http://127.0.0.1:%d/api/health" % port, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read(8192)
        doc = agent_cli.loads_strict(raw.decode("utf-8", "replace"))
        return doc if isinstance(doc, dict) else None
    except Exception:
        return None


def port_in_use(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def main(argv=None):
    args = parse_args(argv)
    if not args.enable_native_cli:
        print("Native companion is disabled. Explicit --enable-native-cli opt-in is required.")
        return 2
    db_path = os.path.abspath(args.db)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    temp_root = os.path.abspath(args.temp_dir or default_temp_root())
    os.makedirs(temp_root, exist_ok=True)

    lock = guild.InstanceLock(db_path + ".lock")
    if not lock.acquire():
        existing = probe_existing(args.port)
        print("已有協調者在跑（PID %s），不重複啟動也不終止任何行程。"
              % (existing.get("pid") if existing else lock.read_owner_pid() or "未知"))
        if existing:
            print("既有實例：%s" % json.dumps(existing, ensure_ascii=False))
        return 0
    if not args.once and port_in_use(args.port):
        existing = probe_existing(args.port)
        print("連接埠 %d 已被占用，不啟動第二份服務。" % args.port)
        if existing:
            print("既有實例：%s" % json.dumps(existing, ensure_ascii=False))
        lock.release()
        return 0

    ledger = guild.GuildLedger(db_path)
    runner = agent_cli.NativeCliRunner(temp_root, timeout_seconds=args.timeout_seconds)
    scheduler = guild.GuildScheduler(ledger, runner, capacity=args.capacity,
                                     cycle_seconds=args.cycle_seconds,
                                     source_url=args.source_url)
    disc = scheduler.discovery(force=True)
    for alias in agent_cli.ALIAS_ORDER + agent_cli.DISABLED_ALIASES:
        info = disc.get(alias) or {}
        state = "可用" if info.get("available") else ("已關閉" if info.get("disabled") else "不可用")
        extra = info.get("reason") or info.get("notes") or ""
        print("CLI %-6s %s %s" % (alias, state, extra))
    print("帳本：%s" % db_path)
    print("自有暫存：%s（只回收，不刪除）" % temp_root)
    print("復原：%s" % json.dumps(scheduler.recovered, ensure_ascii=False))

    if args.once:
        state = scheduler.run_once()
        sched = state["scheduler"]
        print("單輪結束：cycle=%s 完成=%s 失敗=%s 中斷=%s 原因=%s"
              % (sched["cycle"], sched["completed"], sched["failed"], sched["interrupted"],
                 sched["reason"]))
        res = scheduler.shutdown()
        for warn in res.get("cleanup_warnings", []):
            print("清理警告：%s" % warn)
        ledger.close()
        lock.release()
        return 0

    try:
        httpd, _thread = guild.serve(scheduler, args.port)
    except OSError:
        print("無法綁定 127.0.0.1:%d，服務未啟動。" % args.port)
        ledger.close()
        lock.release()
        return 1
    scheduler.start()
    print("工作公會：http://127.0.0.1:%d/api/guild/status （Ctrl+C 停止）" % args.port)
    sys.stdout.flush()

    stop = threading.Event()

    def _handler(_sig, _frm):
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass
    try:
        while not stop.is_set():
            stop.wait(1.0)
    except KeyboardInterrupt:
        pass
    print("停止中：終止自有工作行程樹並標記中斷…")
    try:
        httpd.shutdown()
    except Exception:
        pass
    res = scheduler.shutdown()
    for warn in res.get("cleanup_warnings", []):
        print("清理警告：%s" % warn)
    ledger.close()
    lock.release()
    print("已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
