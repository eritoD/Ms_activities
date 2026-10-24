from contextlib import asynccontextmanager
import logging
import os
from threading import Event, Thread

import httpx
from fastapi import FastAPI

from app.api.v1.activities import ratings_router, router
from app.core.config import get_settings
from app.database.session import create_pool
from app.repositories.postgres_activities_repository import PostgresActivitiesRepository
from app.services.activities_service import ActivitiesService
from app.services.matching_client import MatchingClient
from app.services.users_client import UsersClient

logger = logging.getLogger(__name__)


def _chat_purge_worker(repository, stop_event, interval=3600):
    while not stop_event.wait(interval):
        try:
            repository.purge_old_chats()
        except Exception:
            logger.exception("No se pudieron purgar los chats vencidos")


def create_app(service=None, settings=None):
    @asynccontextmanager
    async def lifespan(app):
        if service is not None:
            yield
            return
        config = get_settings()
        pool = create_pool(config.database_url)
        try:
            repository = PostgresActivitiesRepository(pool)
            repository.migrate()
            purge_thread = None
            stop_purge = Event()
            purge_enabled = os.environ.get("ACTIVITIES_CHAT_PURGE_ENABLED", "true").strip().lower() not in {
                "0", "false", "no", "off"}
            if purge_enabled:
                purge_thread = Thread(target=_chat_purge_worker, args=(repository, stop_purge),
                                      name="activity-chat-purge", daemon=True)
                purge_thread.start()
            try:
                with httpx.Client(base_url=config.users_url, timeout=10, trust_env=False) as client,                         httpx.Client(base_url=config.matching_url, timeout=10, trust_env=False) as matching:
                    app.state.settings = config
                    app.state.service = ActivitiesService(repository, UsersClient(client), MatchingClient(matching))
                    yield
            finally:
                if purge_thread:
                    stop_purge.set()
                    purge_thread.join(timeout=5)
        finally:
            pool.close()

    app = FastAPI(title="SportMatch Activities", lifespan=lifespan)
    if service is not None:
        app.state.service = service
        app.state.settings = settings
    app.include_router(router)
    app.include_router(ratings_router)
    return app


app = create_app()
