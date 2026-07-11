import logging
import signal

from notepatch.platform.database import SessionLocal
from notepatch.modules.ai.services.supervisor import OpenClawSupervisor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
_stopping = False


def _handle_stop(signum, frame) -> None:
    global _stopping
    _stopping = True
    logger.info("Received signal %s, stopping OpenClaw supervisor", signum)


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    logger.info("OpenClaw supervisor started")
    OpenClawSupervisor(db_factory=SessionLocal).run_forever(should_stop=lambda: _stopping)
    logger.info("OpenClaw supervisor stopped")


if __name__ == "__main__":
    main()
