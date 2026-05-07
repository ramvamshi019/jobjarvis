"""Pytest fixtures for JobJarvis tests."""
import asyncio
import pytest
import pytest_asyncio
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from httpx import AsyncClient, ASGITransport

from app.database import Base
from app.main import app
from app.config import settings

# Use in-memory SQLite for tests
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db(test_engine) -> AsyncGenerator[AsyncSession, None]:
    async_session = async_sessionmaker(test_engine, expire_on_commit=False)
    async with async_session() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client(db) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client with overridden DB dependency."""
    from app.database import get_db

    async def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def sample_job_dict():
    return {
        "title": "Senior Data Engineer",
        "company_name": "TestCo",
        "description": (
            "We are looking for a Senior Data Engineer with 5+ years experience. "
            "Required: Python, Spark, PySpark, Airflow, dbt, Snowflake, AWS. "
            "Nice to have: Kafka, Databricks, Delta Lake, Terraform. "
            "No sponsorship available. Remote-friendly."
        ),
        "location": "San Francisco, CA",
        "employment_type": "full_time",
        "salary_min": 160000,
        "salary_max": 200000,
    }


@pytest.fixture
def sample_resume():
    return {
        "skills": {"all": ["Python", "Spark", "Airflow", "dbt", "Snowflake", "AWS", "PostgreSQL"]},
        "tools": ["Spark", "Airflow", "dbt", "Snowflake", "PostgreSQL"],
        "cloud_platforms": ["AWS"],
        "target_roles": ["Data Engineer", "Analytics Engineer"],
        "experience_level": "senior",
    }
