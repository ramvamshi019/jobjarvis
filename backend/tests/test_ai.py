"""Tests for AI classification and decision modules."""
import pytest
from app.ai.role_classifier import classify_role
from app.ai.skill_extractor import extract_skills, match_skills_to_resume
from app.ai.spam_detector import detect_spam
from app.ai.source_classifier import classify_source
from app.ai.work_auth_detector import detect_work_auth
from app.ai.resume_matcher import compute_match
from app.ai.decision_agent import make_decision
from app.ai.interview_probability import estimate_interview_probability


class TestRoleClassifier:
    def test_data_engineer_detection(self):
        result = classify_role("Senior Data Engineer", "Build ETL pipelines with PySpark and Airflow")
        assert result.role_category == "Data Engineer"
        assert result.confidence_score > 0.5

    def test_ml_engineer_detection(self):
        result = classify_role("ML Engineer", "Train and deploy machine learning models with PyTorch")
        assert result.role_category == "ML Engineer"

    def test_ai_engineer_detection(self):
        result = classify_role("AI Engineer", "Build LLM applications with RAG and LangChain")
        assert result.role_category == "AI Engineer"

    def test_mlops_detection(self):
        result = classify_role("MLOps Engineer", "Build model serving with Kubeflow and MLflow")
        assert result.role_category == "MLOps Engineer"

    def test_sdet_detection(self):
        result = classify_role("SDET", "Test automation with Selenium and pytest")
        assert result.role_category == "QA/SDET"

    def test_analytics_engineer(self):
        result = classify_role("Analytics Engineer", "Build dbt models and data warehouse")
        assert result.role_category == "Analytics Engineer"

    def test_not_relevant(self):
        result = classify_role("Marketing Manager", "Lead digital marketing campaigns")
        assert result.role_category == "Not Relevant"

    def test_backend_engineer(self):
        result = classify_role("Backend Engineer", "Build REST APIs with FastAPI and microservices")
        assert result.role_category == "Backend Engineer"

    def test_returns_confidence(self):
        result = classify_role("Senior Data Engineer", "PySpark ETL pipelines")
        assert 0.0 <= result.confidence_score <= 1.0

    def test_returns_reason(self):
        result = classify_role("Data Engineer", "ETL with Airflow")
        assert result.reason != ""


class TestSkillExtractor:
    def test_extracts_python(self):
        result = extract_skills("Data Engineer", "Required: Python, SQL, PySpark")
        assert "Python" in result.required_skills or "Python" in result.all_skills

    def test_extracts_cloud_tools(self):
        result = extract_skills("", "Experience with AWS, Azure, GCP required")
        all_s = result.all_skills
        assert any(s in all_s for s in ["AWS", "Azure", "GCP"])

    def test_extracts_ai_tools(self):
        result = extract_skills("AI Engineer", "Build RAG pipelines with LangChain and pgvector")
        assert "RAG" in result.all_skills or "LangChain" in result.all_skills

    def test_preferred_vs_required(self):
        desc = "Required: Python, Spark. Nice to have: Kafka, Terraform"
        result = extract_skills("DE", desc)
        assert "Python" in result.required_skills
        # Kafka should be in preferred
        assert "Kafka" not in result.required_skills

    def test_empty_returns_empty(self):
        result = extract_skills("", "")
        assert result.all_skills == []


class TestSkillMatching:
    def test_exact_match(self):
        matched, missing = match_skills_to_resume(["Python", "Spark"], ["Python", "Spark", "SQL"])
        assert "Python" in matched
        assert "Spark" in matched
        assert missing == []

    def test_missing_detection(self):
        matched, missing = match_skills_to_resume(["Python", "Kafka"], ["Python"])
        assert "Kafka" in missing

    def test_case_insensitive(self):
        matched, missing = match_skills_to_resume(["python"], ["Python"])
        assert "python" in matched


class TestSpamDetector:
    def test_legitimate_job(self):
        job = {
            "title": "Senior Data Engineer",
            "description": "We are a Series B startup building real-time data infrastructure. " * 20,
            "company_domain": "datacompany.com",
        }
        result = detect_spam(job)
        assert result.spam_score < 0.4
        assert not result.is_spam

    def test_vendor_spam_detection(self):
        job = {
            "title": "Data Engineer - W2 only, C2C not allowed",
            "description": "Kindly share your resume. Our client is looking for a resource.",
            "company_domain": None,
        }
        result = detect_spam(job)
        assert result.spam_score > 0.3
        assert len(result.spam_flags) > 0

    def test_empty_description_flagged(self):
        job = {"title": "Engineer", "description": "", "company_domain": "co.com"}
        result = detect_spam(job)
        assert "no_description" in result.spam_flags or "extremely_short_description" in result.spam_flags


class TestWorkAuthDetector:
    def test_no_sponsorship_detected(self):
        result = detect_work_auth("We do not sponsor work visas. No sponsorship available.")
        assert "no_sponsorship" in result.work_auth_flags
        assert result.eligibility_risk_score >= 0.8

    def test_us_citizen_only(self):
        result = detect_work_auth("Must be a US citizen or permanent resident. US Citizens only.")
        assert "us_citizen_only" in result.work_auth_flags
        assert result.disqualified

    def test_clearance_required(self):
        result = detect_work_auth("TS/SCI clearance required.")
        assert "security_clearance_required" in result.work_auth_flags
        assert result.eligibility_risk_score >= 0.9

    def test_clean_job(self):
        result = detect_work_auth("Equal opportunity employer. All backgrounds welcome.")
        assert result.work_auth_flags == []
        assert result.eligibility_risk_score == 0.0

    def test_w2_only_lower_risk(self):
        result = detect_work_auth("W2 only, no 1099 or corp-to-corp.")
        assert "w2_only" in result.work_auth_flags
        assert result.eligibility_risk_score < 0.5  # W2 is lower risk than no-sponsorship


class TestSourceClassifier:
    def test_direct_company_greenhouse(self):
        result = classify_source("OpenAI", "We are building safe AI...", ats_type="greenhouse")
        assert result.source_type == "DIRECT_COMPANY"

    def test_staffing_agency_detected(self):
        result = classify_source("Staffing Solutions Inc", "C2C preferred. Corp-to-corp OK.")
        assert result.source_type == "STAFFING_AGENCY"

    def test_vendor_keywords(self):
        result = classify_source("TekSystems", "Our client is looking for a resource. No H1.")
        assert result.source_type == "STAFFING_AGENCY"


class TestResumeMatcher:
    def test_high_fit_senior_data_engineer(self, sample_job_dict, sample_resume):
        match = compute_match(sample_job_dict, sample_resume)
        assert match.fit_score > 50  # should have decent fit

    def test_matched_skills_populated(self, sample_job_dict, sample_resume):
        match = compute_match(sample_job_dict, sample_resume)
        assert len(match.matched_skills) > 0

    def test_missing_skills_detected(self, sample_job_dict):
        empty_resume = {"skills": {"all": []}, "target_roles": [], "experience_level": "mid",
                        "tools": [], "cloud_platforms": []}
        match = compute_match(sample_job_dict, empty_resume)
        assert len(match.missing_skills) > 0

    def test_fit_score_range(self, sample_job_dict, sample_resume):
        match = compute_match(sample_job_dict, sample_resume)
        assert 0.0 <= match.fit_score <= 100.0

    def test_risk_score_for_restrictive_job(self):
        risky_job = {
            "title": "Data Engineer",
            "role_category": "Data Engineer",
            "required_skills": ["Python"],
            "preferred_skills": [],
            "eligibility_risk_score": 0.9,
            "remote_type": "onsite",
            "salary_min": None,
            "salary_max": None,
        }
        resume = {"skills": {"all": ["Python"]}, "target_roles": ["Data Engineer"],
                  "experience_level": "mid", "tools": [], "cloud_platforms": []}
        match = compute_match(risky_job, resume)
        assert match.risk_score >= 0.8


class TestDecisionAgent:
    def test_apply_now_high_fit(self, sample_job_dict, sample_resume):
        from app.ai.resume_matcher import compute_match
        sample_job_dict["role_category"] = "Data Engineer"
        sample_job_dict["freshness_label"] = "new_today"
        sample_job_dict["spam_score"] = 0.0
        sample_job_dict["eligibility_risk_score"] = 0.0
        sample_job_dict["source_type"] = "DIRECT_COMPANY"
        sample_job_dict["work_auth_flags_json"] = {}

        match = compute_match(sample_job_dict, sample_resume)
        decision = make_decision(match, sample_job_dict)

        assert decision.decision in ("APPLY_NOW", "TAILOR_RESUME_FIRST", "SAVE_FOR_LATER")

    def test_high_risk_disqualifies(self):
        from app.ai.resume_matcher import MatchResult
        match = MatchResult(
            fit_score=80.0, role_match_score=0.9, skill_match_score=0.8,
            seniority_match_score=1.0, domain_match_score=0.7, location_match_score=1.0,
            compensation_match_score=0.9, risk_score=0.9,
        )
        job = {
            "role_category": "Data Engineer",
            "spam_score": 0.0,
            "eligibility_risk_score": 0.9,
            "work_auth_flags_json": {"flags": ["us_citizen_only"]},
            "freshness_label": "new_today",
            "source_type": "DIRECT_COMPANY",
        }
        decision = make_decision(match, job)
        assert decision.decision == "HIGH_RISK"

    def test_spam_job_skipped(self):
        from app.ai.resume_matcher import MatchResult
        match = MatchResult(
            fit_score=50.0, role_match_score=0.5, skill_match_score=0.5,
            seniority_match_score=0.5, domain_match_score=0.5, location_match_score=0.5,
            compensation_match_score=0.5, risk_score=0.0,
        )
        job = {
            "role_category": "Data Engineer",
            "spam_score": 0.8,
            "eligibility_risk_score": 0.0,
            "work_auth_flags_json": {},
            "freshness_label": "new_today",
            "source_type": "STAFFING_AGENCY",
        }
        decision = make_decision(match, job)
        assert decision.decision == "SKIP"

    def test_decision_has_fit_score(self, sample_job_dict, sample_resume):
        from app.ai.resume_matcher import compute_match
        sample_job_dict.update({"role_category": "Data Engineer", "spam_score": 0.0,
                                "eligibility_risk_score": 0.0, "freshness_label": "new_today",
                                "source_type": "DIRECT_COMPANY", "work_auth_flags_json": {}})
        match = compute_match(sample_job_dict, sample_resume)
        decision = make_decision(match, sample_job_dict)
        assert decision.fit_score is not None
        assert decision.confidence is not None


class TestInterviewProbability:
    def test_high_fit_higher_probability(self):
        high = estimate_interview_probability(fit_score=90.0)
        low = estimate_interview_probability(fit_score=30.0)
        assert high.interview_probability > low.interview_probability

    def test_probability_range(self):
        result = estimate_interview_probability(fit_score=50.0)
        assert 0.0 <= result.interview_probability <= 1.0

    def test_fresh_job_higher_probability(self):
        fresh = estimate_interview_probability(fit_score=70.0, application_timing_score=1.0)
        stale = estimate_interview_probability(fit_score=70.0, application_timing_score=0.0)
        assert fresh.interview_probability >= stale.interview_probability
