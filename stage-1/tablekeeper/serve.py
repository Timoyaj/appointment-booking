"""Entry point: listen on 0.0.0.0:$PORT (default 8080)."""

from __future__ import annotations

import logging
import os

import uvicorn

from .api import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    database_path = os.environ.get(
        "TABLEKEEPER_DB", "/tmp/tablekeeper/tablekeeper.db"
    )
    app = create_app(database_path=database_path)
    # Access logs off: under 50 concurrent requests they are pure overhead.
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
