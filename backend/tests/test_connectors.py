"""Tests for ATS connectors (unit tests, no network)."""
import pytest
from datetime import datetime
from app.connectors.greenhouse import GreenhouseConnector
from app.connectors.lever import LeverConnector
from app.connectors.ashby import AshbyConnector
from app.connectors.smartrecruiters import SmartRecruitersConnector
from app.connectors.workday import WorkdayConnector
from app.connectors.icims import ICIMSConnector
from app.connectors.base import RawJob


class TestGreenhouseConnector:
    def test_parse_valid_job(self):
        connector = GreenhouseConnector()
        raw = {
            "id": 123456,
            "title": "Senior Data Engineer",
            "absolute_url": "https://boards.greenhouse.io/testco/jobs/123456",
            "content": "Build data pipelines with PySpark and Airflow.",
            "updated_at": "2024-01-15T10:00:00Z",
            "offices": [{"name": "San Francisco, CA"}],
        }
        job = connector._parse_job(raw, "testco")
        assert job is not None
        assert job.external_id == "123456"
        assert job.title == "Senior Data Engineer"
        assert "San Francisco" in job.location
        assert job.source == "greenhouse"

    def test_parse_missing_id_returns_none(self):
        connector = GreenhouseConnector()
        assert connector._parse_job({"title": "Test"}, "co") is None

    def test_parse_remote_detection(self):
        connector = GreenhouseConnector()
        raw = {
            "id": 1, "title": "Data Engineer",
            "offices": [{"name": "Remote - US"}],
            "absolute_url": "https://boards.greenhouse.io/co/jobs/1",
        }
        job = connector._parse_job(raw, "co")
        assert job.remote == True

    def test_ats_type(self):
        assert GreenhouseConnector.ats_type == "greenhouse"


class TestLeverConnector:
    def test_parse_valid_job(self):
        connector = LeverConnector()
        raw = {
            "id": "abc-def-123",
            "text": "ML Engineer",
            "hostedUrl": "https://jobs.lever.co/testco/abc-def-123",
            "applyUrl": "https://jobs.lever.co/testco/abc-def-123/apply",
            "categories": {"location": "New York, NY", "commitment": "Full-time"},
            "createdAt": 1705305600000,
            "descriptionPlain": "Build ML models with PyTorch and TensorFlow.",
        }
        job = connector._parse_job(raw, "testco")
        assert job is not None
        assert job.external_id == "abc-def-123"
        assert job.title == "ML Engineer"
        assert job.source == "lever"

    def test_parse_missing_fields_returns_none(self):
        connector = LeverConnector()
        assert connector._parse_job({}, "co") is None

    def test_ats_type(self):
        assert LeverConnector.ats_type == "lever"


class TestAshbyConnector:
    def test_parse_valid_job(self):
        connector = AshbyConnector()
        raw = {
            "id": "ashby-job-456",
            "title": "Analytics Engineer",
            "locationName": "Remote",
            "employmentType": "FullTime",
            "publishedAt": "2024-01-20T12:00:00Z",
            "descriptionHtml": "<p>Build dbt models and data warehouse.</p>",
        }
        job = connector._parse_job(raw, "testco")
        assert job is not None
        assert job.title == "Analytics Engineer"
        assert job.remote == True
        assert job.source == "ashby"


class TestSmartRecruitersConnector:
    def test_parse_valid_job(self):
        connector = SmartRecruitersConnector()
        raw = {
            "id": "sr-job-789",
            "name": "Backend Engineer",
            "location": {"city": "Austin", "country": "US", "remote": False},
            "releasedDate": "2024-01-18T09:00:00Z",
            "company": {"name": "SmartTestCo"},
        }
        job = connector._parse_job(raw, "smarttestco")
        assert job is not None
        assert job.title == "Backend Engineer"
        assert "Austin" in job.location
        assert job.source == "smartrecruiters"


class TestConnectorRegistry:
    def test_all_connectors_registered(self):
        from app.connectors import ATS_REGISTRY
        expected = {"greenhouse", "lever", "ashby", "smartrecruiters", "workday", "icims"}
        assert expected.issubset(set(ATS_REGISTRY.keys()))

    def test_get_connector_returns_class(self):
        from app.connectors import get_connector
        cls = get_connector("greenhouse")
        assert cls == GreenhouseConnector

    def test_get_unknown_connector_returns_none(self):
        from app.connectors import get_connector
        assert get_connector("unknown_ats") is None
