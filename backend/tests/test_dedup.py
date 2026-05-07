"""Tests for deduplication engine."""
import pytest
from app.services.dedup import DedupEngine, compute_fingerprint
from app.services.normalizer import normalize_title, normalize_location


class TestFingerprint:
    def test_same_job_same_fingerprint(self):
        fp1 = compute_fingerprint("Senior Data Engineer", 1, "Remote")
        fp2 = compute_fingerprint("Senior Data Engineer", 1, "Remote")
        assert fp1 == fp2

    def test_different_location_different_fingerprint(self):
        """CRITICAL: Different cities must NOT merge."""
        fp_sf = compute_fingerprint("Data Engineer", 1, "San Francisco, CA")
        fp_ny = compute_fingerprint("Data Engineer", 1, "New York, NY")
        assert fp_sf != fp_ny

    def test_different_company_different_fingerprint(self):
        fp1 = compute_fingerprint("Data Engineer", 1, "Remote")
        fp2 = compute_fingerprint("Data Engineer", 2, "Remote")
        assert fp1 != fp2

    def test_fingerprint_is_64_chars(self):
        fp = compute_fingerprint("Data Engineer", 1, "Remote")
        assert len(fp) == 64

    def test_case_insensitive(self):
        fp1 = compute_fingerprint("DATA ENGINEER", 1, "remote")
        fp2 = compute_fingerprint("data engineer", 1, "Remote")
        assert fp1 == fp2


class TestDedupEngine:
    def test_engine_instantiates(self):
        engine = DedupEngine()
        assert engine is not None
