"""``python -m worker``: poll the jobs table until SIGINT/SIGTERM."""
from __future__ import annotations

import signal
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.logging import configure_logging  # noqa: E402
from db.session import make_engine, make_sessionmaker  # noqa: E402
from worker.runner import Worker  # noqa: E402


def main() -> None:
    configure_logging()
    stopping = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True  # finish the current job, then exit

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    Worker(make_sessionmaker(make_engine())).run_forever(stop=lambda: stopping)


if __name__ == "__main__":
    main()
