"""Lazy async MongoDB client (motor). The password comes from the MONGO_PASSWORD
env var when set, otherwise from CyberArk at first connect — so the service can
boot in environments without vault access as long as nothing touches the DB."""

from motor.motor_asyncio import AsyncIOMotorClient

from clause_splitter.utils import get_logger
from config.config_mongo import CONFIG_MONGO, MONGO_PASSWORD_OBJ

logger = get_logger()

_client: AsyncIOMotorClient | None = None


def _resolve_password() -> str:
    if CONFIG_MONGO.password:
        return CONFIG_MONGO.password
    from common.cyberark import Cyberark

    return Cyberark().get_password_mongo(MONGO_PASSWORD_OBJ)


def get_mongo_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(CONFIG_MONGO.uri(_resolve_password()))
        logger.info("MongoDB client created for %s:%s/%s", CONFIG_MONGO.host, CONFIG_MONGO.port, CONFIG_MONGO.database)
    return _client


def get_mongo_db():
    return get_mongo_client()[CONFIG_MONGO.database]


async def close_mongo() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
        logger.info("MongoDB client closed")
