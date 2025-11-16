import asyncio

from loguru import logger

import bot_app
from load_config import load_avito_config
from scheduler import FiltersScheduler
from user_filters import UserFiltersStorage


def main():
    config = load_avito_config("config.toml")
    storage = UserFiltersStorage()
    scheduler = FiltersScheduler(config, storage)
    scheduler.start()
    logger.info("Бот и планировщик готовы к работе")
    try:
        asyncio.run(bot_app.main())
    finally:
        logger.info("Останавливаю планировщик")
        scheduler.stop()


if __name__ == "__main__":
    main()
