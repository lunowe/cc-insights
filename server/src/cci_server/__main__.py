"""`cci-server` -- run it, or apply its migrations and stop.

Two subcommands and no more. Anything else this needs is a deployment
concern, and a server that grows a CLI grows two places where the database
URL can be wrong.
"""

from __future__ import annotations

import argparse
import sys

from cci_server import config
from cci_server.db import Database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cci-server")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="serve the API")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8788)

    sub.add_parser("migrate", help="apply pending migrations and exit")

    args = parser.parse_args(argv)
    settings = config.from_env()

    if args.cmd == "migrate":
        db = Database(settings.database_url)
        try:
            applied = db.migrate()
        finally:
            db.close()
        print(f"migrations applied: {applied}" if applied else "already up to date")
        return 0

    import uvicorn

    from cci_server.app import create_app

    # Binds to localhost by default. A server holding other people's
    # filesystem paths should require somebody to type the address it listens
    # on rather than defaulting to every interface.
    uvicorn.run(create_app(settings), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
