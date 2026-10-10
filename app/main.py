from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.v1.activities import ratings_router, router
from app.core.config import get_settings
from app.database.session import create_pool
from app.repositories.postgres_activities_repository import PostgresActivitiesRepository
from app.services.activities_service import ActivitiesService
from app.services.matching_client import MatchingClient
from app.services.users_client import UsersClient


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
            with httpx.Client(base_url=config.users_url, timeout=10, trust_env=False) as client,                     httpx.Client(base_url=config.matching_url, timeout=10, trust_env=False) as matching:
                app.state.settings = config
                app.state.service = ActivitiesService(repository, UsersClient(client), MatchingClient(matching))
                yield
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
