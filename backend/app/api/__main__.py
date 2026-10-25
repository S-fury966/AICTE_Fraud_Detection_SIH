"""python -m app.api [--host H] [--port P] - serve the reviewer API on localhost."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    from ..core.config import get, load_config
    from .main import create_app

    cfg = load_config("api")
    parser = argparse.ArgumentParser(prog="python -m app.api", description="Serve the reviewer API.")
    parser.add_argument("--host", default=str(get(cfg, "server.host", "127.0.0.1")))
    parser.add_argument("--port", type=int, default=int(get(cfg, "server.port", 8765)))
    args = parser.parse_args(argv)
    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
