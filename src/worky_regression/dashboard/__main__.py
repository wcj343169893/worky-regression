"""python -m worky_regression.dashboard [--host H] [--port P]"""
from __future__ import annotations

import argparse

from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Worky 承攬制任務看板")
    parser.add_argument("--host", default="127.0.0.1", help="預設 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="預設 8765")
    args = parser.parse_args()
    serve(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
