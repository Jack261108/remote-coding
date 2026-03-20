from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramNetworkError

from app.bootstrap import AppContainer
from app.config.settings import Settings
from app.infra.logging import configure_logging

logger = logging.getLogger(__name__)


async def run() -> None:
    configure_logging()
    settings = Settings()
    container = AppContainer(settings=settings)
    container.wire()

    logger.info("bot starting with polling")

    while True:
        try:
            await container.dispatcher.start_polling(container.bot)
            return
        except TelegramNetworkError as exc:
            logger.warning(
                "telegram network error, will retry",
                extra={"error": str(exc), "retry_delay_sec": settings.tg_polling_retry_delay_sec},
            )
            await asyncio.sleep(settings.tg_polling_retry_delay_sec)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
