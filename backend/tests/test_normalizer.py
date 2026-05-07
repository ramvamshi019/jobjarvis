"""Tests for the Silver layer normalizer."""
import pytest
from app.services.normalizer import (
    normalize_title, normalize_company, normalize_location, extract_country,
    classify_experience_level, classify_employment_type, extract_salary,
    detect_remote_type, clean_description,
)


class TestNormalizeTitle:
    def test_removes_urgent_flag(self):
        assert "Urgent" not in normalize_title("Urgent! Data Engineer Opening")

    def test_preserves_role_name(self):
        result = normalize_title("  Senior Data Engineer  ")
        assert "Data Engineer" in result

    def test_handles_empty(self):
        assert normalize_title("") == ""

    def test_title_case(self):
        result = normalize_title("machine learning engineer")
        assert result[0].isupper()


class TestExperienceLevel:
    def test_intern_detection(self):
        assert classify_experience_level("Data Engineering Intern") == "intern"

    def test_internship_detection(self):
        assert classify_experience_level("Summer Internship - ML") == "intern"

    def test_new_grad_is_entry(self):
        assert classify_experience_level("New Grad - Data Engineer") == "entry"

    def test_junior_is_entry(self):
        assert classify_experience_level("Junior Data Engineer") == "entry"

    def test_senior_detection(self):
        assert classify_experience_level("Senior Data Engineer") == "senior"

    def test_staff_is_senior(self):
        assert classify_experience_level("Staff Data Engineer") == "senior"

    def test_principal_is_senior(self):
        assert classify_experience_level("Principal Engineer") == "senior"

    def test_entry_checked_before_senior(self):
        # "new grad" in title should be entry, even if "senior" is in description
        result = classify_experience_level("New Grad Data Engineer", "senior experience preferred")
        assert result == "entry"

    def test_specialist_not_auto_senior(self):
        # "specialist" should NOT auto-classify as senior
        result = classify_experience_level("Data Specialist")
        assert result != "senior"

    def test_consultant_not_auto_senior(self):
        result = classify_experience_level("Data Consultant")
        assert result != "senior"


class TestSalaryExtraction:
    def test_range_extraction(self):
        min_s, max_s, period = extract_salary("$150,000 - $200,000 per year")
        assert min_s == 150000
        assert max_s == 200000

    def test_k_notation(self):
        min_s, max_s, period = extract_salary("$150k - $200k")
        assert min_s == 150000
        assert max_s == 200000

    def test_hourly_detection(self):
        _, _, period = extract_salary("$50 - $75 per hour")
        assert period == "hourly"

    def test_empty_returns_none(self):
        min_s, max_s, period = extract_salary("")
        assert min_s is None
        assert max_s is None


class TestLocationNormalization:
    def test_remote_detection(self):
        assert normalize_location("Work From Home") == "Remote"

    def test_remote_keyword(self):
        assert normalize_location("Fully Remote") == "Remote"

    def test_state_abbreviation_expansion(self):
        result = normalize_location("San Francisco, CA")
        assert "California" in result

    def test_empty(self):
        assert normalize_location("") == ""


class TestCountryExtraction:
    def test_us_detection(self):
        assert extract_country("San Francisco, USA") == "US"

    def test_remote_is_us(self):
        assert extract_country("Remote") == "US"

    def test_india(self):
        assert extract_country("Bangalore, India") == "India"

    def test_unknown(self):
        assert extract_country("") == "Unknown"


class TestRemoteType:
    def test_fully_remote(self):
        assert detect_remote_type("", "Fully Remote", "") == "remote"

    def test_hybrid(self):
        assert detect_remote_type("", "Hybrid - 2 days NYC", "") == "hybrid"

    def test_onsite(self):
        assert detect_remote_type("", "New York City, NY", "") == "onsite"
