#!/usr/bin/env python3
"""Production entrypoint for the lab-butler server.

Reads BUTLER_PORT at runtime, so changing it in butler.env actually takes
effect (OpenRC expands command_args at parse time, before start_pre sources
the env file — see mesh-flux's serve.py, same constraint here).

Serves through waitress when available. Flask's built-in server is
single-threaded, which would queue the poller's and the dashboard's requests
behind one another; it remains as a fallback only.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.app import app  # noqa: E402
from app import config  # noqa: E402


def _start_background_services():
    """Start the syslog listener and poller, if their modules exist yet.

    Both land in later build phases (syslog_server.py in phase 4, poller.py
    in phase 3). Importing here rather than unconditionally at module load
    keeps this file stable across phases — no edit needed when either module
    is added, since both expose an idempotent start() that returns quietly
    on failure (unavailable port, no devices yet), matching mesh-flux's
    syslog_server.start() contract: a missing background service must never
    stop the server from serving requests, which is the job that matters.
    """
    try:
        from app import syslog_server
    except ImportError:
        pass
    else:
        syslog_server.start()

    try:
        from app import poller
    except ImportError:
        pass
    else:
        poller.start()


def main():
    host = os.environ.get("BUTLER_HOST", "0.0.0.0")
    port = config.PORT

    _start_background_services()

    try:
        from waitress import serve
    except ImportError:
        sys.stderr.write(
            "waitress not installed; falling back to the Flask development "
            "server (single-threaded, not recommended)\n"
        )
        app.run(host=host, port=port, threaded=True)
        return

    sys.stderr.write(f"lab-butler listening on {host}:{port}\n")
    serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
