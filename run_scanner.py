"""Compatibility entry: offline DEMO by default; --once means the offline --check."""
import sys
from run_app import main as app_main

def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    args = ["--check" if arg == "--once" else arg for arg in args if arg != "--serve"]
    return app_main(args)

if __name__ == "__main__":
    raise SystemExit(main())
