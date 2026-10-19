"""Entry point: ``python -m suppression``.

uvicorn's access log is disabled on purpose: it prints the query string, and
the query string carries the phone number. The app writes its own access line
without it.
"""

import logging
import os
import sys

import uvicorn

from .app import create_app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    try:
        app = create_app()
    except RuntimeError as exc:
        logging.getLogger("suppression").critical("%s", exc)
        sys.exit(2)
    uvicorn.run(
        app,
        host=os.environ.get("SUPPRESSION_HOST", "0.0.0.0"),
        port=int(os.environ.get("SUPPRESSION_PORT", "8080")),
        access_log=False,
        server_header=False,
        proxy_headers=False,
        log_config=None,
        timeout_keep_alive=5,
    )


if __name__ == "__main__":
    main()
