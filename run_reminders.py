"""Entry point for the readinessos-reminders systemd timer -- one run per
invocation, exits cleanly. Loads env from /opt/readinessos/.env same as
main.py (systemd unit sets EnvironmentFile= to that path)."""
from __future__ import annotations

import asyncio
import logging

from services.database import close_pool, init_pool
from services.reminder_job import run_reminder_cycle

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def main():
    pool = await init_pool()
    try:
        result = await run_reminder_cycle(pool)
        logging.getLogger("rxos.run_reminders").info("cycle complete: %s", result)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
