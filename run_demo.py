# -*- coding: utf-8 -*-
"""最短啟動路徑：離線 DEMO（合成資料、零金鑰、零網路、零安裝）。

  python -B run_demo.py              # http://127.0.0.1:8775/
  python -B run_demo.py --port 8775

這支只是 run_app.py 的便利包裝，永遠不會進入真實模式。
"""
import sys

import run_app


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--real-paper" in argv:
        print("run_demo.py 只跑離線 DEMO；真實紙上交易請用 run_app.py --real-paper。")
        return 2
    if "--demo" not in argv:
        argv.insert(0, "--demo")
    return run_app.main(argv)


if __name__ == "__main__":
    sys.exit(main())
