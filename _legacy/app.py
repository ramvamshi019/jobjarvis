"""
Streamlit Dashboard — Job Automation System.

Professional dashboard with:
  - Analytics charts (score distribution, jobs over time, source breakdown)
  - Multi-tab layout (Dashboard, Jobs, Analytics, Pipeline History)
  - Full-text search with instant results
  - Status management with bulk actions
  - Resume/cover letter download
  - Responsive, modern UI

Run with:  streamlit run app.py
"""

import os
import sys
import re
import streamlit as st
import pandas as pd
from datetime import datetime

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import (
    init_db,
    get_all_jobs,
    get_job_count,
    search_jobs,
    update_applied_status,
    get_jobs_by_status,
    get_score_distribution,
    get_jobs_by_source,
    get_jobs_by_company,
    get_jobs_over_time,
    get_status_breakdown,
    get_top_scoring_jobs,
    get_recent_jobs,
    get_pipeline_history,
    update_job_notes,
)
from pipeline import run_pipeline_sync
from generate_resume import generate_tailored_resume, generate_cover_letter
from pdf_generator import generate_pdf, generate_cover_letter_pdf
from db import update_resume_path, update_cover_letter


# ─── Page Config ────────────────────────────────────────────────
st.set_page_config(
    page_title="Job Automation System",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Custom CSS ─────────────────────────────────────────────────
st.markdown("""
<style>
    /* Global */
    .main-header {
        font-size: 1.8rem;
        font-weight: 700;
        color: #1a1a2e;
        margin-bottom: 0.25rem;
    }
    .sub-header {
        font-size: 0.9rem;
        color: #6b7280;
        margin-bottom: 1rem;
    }

    /* Metric cards */
    [data-testid="stMetric"] {
        background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 12px 16px;
    }
    [data-testid="stMetricValue"] {
        font-size: 1.8rem;
        font-weight: 700;
    }

    /* Score badges */
    .score-high { color: #059669; font-weight: 700; }
    .score-med  { color: #d97706; font-weight: 700; }
    .score-low  { color: #dc2626; font-weight: 600; }

    /* Buttons */
    .stButton > button {
        border-radius: 8px;
        font-weight: 500;
    }

    /* Expanders */
    .streamlit-expanderHeader {
        font-size: 0.95rem;
    }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px 8px 0 0;
        padding: 8px 20px;
    }

    /* Hide Streamlit branding */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
</style>
""", unsafe_allow_html=True)


# ─── Initialize ─────────────────────────────────────────────────
init_db()


# ─── Sidebar ────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🎯 Job Automation")
    st.markdown("---")

    # Pipeline controls
    st.markdown("### ⚡ Run Pipeline")
    col1, col2 = st.columns(2)
    with col1:
        gen_resumes = st.checkbox("Generate Resumes", value=False)
    with col2:
        send_alerts = st.checkbox("Send Alerts", value=False)

    max_resumes = st.slider("Max resumes per run", 1, 50, 10)
    min_score = st.slider("Min score for resume", 0, 100, 50)

    if st.button("🚀 Run Full Pipeline", type="primary", use_container_width=True):
        with st.spinner("Running pipeline... This may take a few minutes."):
            try:
                stats = run_pipeline_sync(
                    generate_resumes=gen_resumes,
                    send_alerts=send_alerts,
                    max_resumes=max_resumes,
                    min_score=min_score,
                )
                st.success(
                    f"Done! {stats['new_jobs']} new jobs, "
                    f"{stats['resumes_generated']} resumes generated."
                )
                if stats.get("errors"):
                    st.warning(f"{len(stats['errors'])} errors occurred. Check logs.")
            except Exception as e:
                st.error(f"Pipeline error: {e}")

    st.markdown("---")

    # Search
    st.markdown("### 🔍 Search")
    search_query = st.text_input(
        "Search jobs",
        placeholder="e.g. data engineer, stripe, remote",
        label_visibility="collapsed",
    )

    st.markdown("---")

    # Filter
    st.markdown("### 📊 Filter")
    status_filter = st.selectbox(
        "Status",
        ["all", "new", "saved", "applied", "interviewing", "offer", "rejected", "archived"],
    )

    score_range = st.slider("Score range", 0, 100, (0, 100))

    st.markdown("---")

    # Stats
    counts = get_job_count()
    st.markdown("### 📈 Quick Stats")
    st.markdown(f"**Total:** {counts['total']} jobs")
    st.markdown(f"**New:** {counts['new']} | **Saved:** {counts['saved']}")
    st.markdown(f"**Applied:** {counts['applied']} | **Interviewing:** {counts['interviewing']}")
    if counts.get("offers"):
        st.markdown(f"**Offers:** {counts['offers']}")


# ─── Main Content ───────────────────────────────────────────────
st.markdown('<div class="main-header">🎯 Job Automation Dashboard</div>', unsafe_allow_html=True)
st.markdown(
    f'<div class="sub-header">Last refreshed: {datetime.now().strftime("%Y-%m-%d %H:%M")} · '
    f'Respects rate limits · Does not auto-apply</div>',
    unsafe_allow_html=True,
)

# ─── Tabs ───────────────────────────────────────────────────────
tab_jobs, tab_analytics, tab_history = st.tabs(["📋 Jobs", "📊 Analytics", "🔄 Pipeline History"])


# ═══════════════════════════════════════════════════════════════
# TAB: Jobs
# ═══════════════════════════════════════════════════════════════
with tab_jobs:
    # Metrics row
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    with m1:
        st.metric("Total", counts["total"])
    with m2:
        st.metric("New", counts["new"])
    with m3:
        st.metric("Saved", counts["saved"])
    with m4:
        st.metric("Resumes", counts["with_resume"])
    with m5:
        st.metric("Applied", counts["applied"])
    with m6:
        st.metric("Interviewing", counts["interviewing"])

    st.markdown("---")

    # Get jobs based on search/filter
    if search_query:
        jobs = search_jobs(search_query)
        jobs = [j for j in jobs if score_range[0] <= j.get("match_score", 0) <= score_range[1]]
        st.markdown(f"### 🔍 Search: *{search_query}* ({len(jobs)} results)")
    elif status_filter != "all":
        jobs = get_jobs_by_status(status_filter)
        jobs = [j for j in jobs if score_range[0] <= j.get("match_score", 0) <= score_range[1]]
        st.markdown(f"### 📋 Status: *{status_filter}* ({len(jobs)} jobs)")
    else:
        jobs = get_all_jobs(limit=200)
        jobs = [j for j in jobs if score_range[0] <= j.get("match_score", 0) <= score_range[1]]
        st.markdown(f"### 📋 All Jobs ({len(jobs)} shown)")

    if not jobs:
        st.info("No jobs found. Run the pipeline to fetch new jobs, or adjust your filters.")
    else:
        for i, job in enumerate(jobs):
            score = job.get("match_score", 0)
            if score >= 75:
                score_emoji = "🔥"
            elif score >= 50:
                score_emoji = "⭐"
            else:
                score_emoji = "📋"

            has_resume = bool(job.get("resume_path") and os.path.exists(job["resume_path"]))
            has_cover = bool(job.get("cover_letter") and os.path.exists(job["cover_letter"]))
            resume_badge = "✅" if has_resume else "⏳"

            salary_text = ""
            if job.get("salary_min"):
                salary_text = f"  |  💰 ${job['salary_min']:,.0f}"
                if job.get("salary_max"):
                    salary_text += f"-${job['salary_max']:,.0f}"

            with st.expander(
                f"{score_emoji} **[{score:.0f}]** {job['company']} — {job['title']}  |  "
                f"📍 {job.get('location', 'N/A')}{salary_text}  |  {resume_badge} Resume",
                expanded=False,
            ):
                col_info, col_actions = st.columns([3, 1])

                with col_info:
                    detail_cols = st.columns(3)
                    with detail_cols[0]:
                        st.markdown(f"**Source:** {job['source'].title()}")
                        st.markdown(f"**Status:** `{job.get('applied_status', 'new')}`")
                    with detail_cols[1]:
                        st.markdown(f"**Score:** `{score:.1f}/100`")
                        st.markdown(f"**Added:** {job.get('created_at', 'N/A')[:10]}")
                    with detail_cols[2]:
                        if job.get("experience_min"):
                            st.markdown(f"**Experience:** {job['experience_min']}-{job.get('experience_max', '?')} yrs")
                        if job.get("salary_min"):
                            s_text = f"${job['salary_min']:,.0f}"
                            if job.get("salary_max"):
                                s_text += f" - ${job['salary_max']:,.0f}"
                            st.markdown(f"**Salary:** {s_text}")

                    # Description preview
                    desc = job.get("description", "")
                    if desc:
                        clean = re.sub(r"<[^>]+>", " ", desc)
                        clean = re.sub(r"\s+", " ", clean).strip()
                        if len(clean) > 600:
                            clean = clean[:600] + "..."
                        st.markdown("**Description:**")
                        st.caption(clean)

                with col_actions:
                    if job.get("link"):
                        st.link_button("🔗 Apply Now", job["link"], use_container_width=True)

                    if has_resume:
                        with open(job["resume_path"], "rb") as f:
                            st.download_button(
                                "📄 Resume PDF",
                                data=f.read(),
                                file_name=os.path.basename(job["resume_path"]),
                                mime="application/pdf",
                                use_container_width=True,
                                key=f"dl_resume_{i}",
                            )

                    if has_cover:
                        with open(job["cover_letter"], "rb") as f:
                            st.download_button(
                                "💌 Cover Letter",
                                data=f.read(),
                                file_name=os.path.basename(job["cover_letter"]),
                                mime="application/pdf",
                                use_container_width=True,
                                key=f"dl_cl_{i}",
                            )

                    status_options = ["new", "saved", "applied", "interviewing", "offer", "rejected", "archived"]
                    current = job.get("applied_status", "new")
                    idx = status_options.index(current) if current in status_options else 0
                    new_status = st.selectbox(
                        "Status",
                        status_options,
                        index=idx,
                        key=f"status_{i}",
                        label_visibility="collapsed",
                    )
                    if st.button("Update Status", key=f"update_{i}", use_container_width=True):
                        update_applied_status(job["job_id"], new_status)
                        st.rerun()

                    if not has_resume:
                        if st.button("🤖 Generate Resume", key=f"gen_{i}", use_container_width=True):
                            with st.spinner("Generating resume & cover letter..."):
                                try:
                                    resume_text = generate_tailored_resume(
                                        job["title"], job["company"], job["description"]
                                    )
                                    pdf_path = generate_pdf(resume_text, job["job_id"])
                                    update_resume_path(job["job_id"], pdf_path)

                                    cl_text = generate_cover_letter(
                                        job["title"], job["company"], job["description"]
                                    )
                                    cl_path = generate_cover_letter_pdf(cl_text, job["job_id"])
                                    update_cover_letter(job["job_id"], cl_path)

                                    st.success("Resume & cover letter generated!")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error: {e}")


# ═══════════════════════════════════════════════════════════════
# TAB: Analytics
# ═══════════════════════════════════════════════════════════════
with tab_analytics:
    st.markdown("### 📊 Job Analytics")

    if counts["total"] == 0:
        st.info("No data yet. Run the pipeline to fetch jobs and see analytics here.")
    else:
        # Row 1: Score distribution + Status breakdown
        chart1, chart2 = st.columns(2)

        with chart1:
            st.markdown("#### Score Distribution")
            score_data = get_score_distribution()
            if score_data:
                df_scores = pd.DataFrame(score_data)
                st.bar_chart(df_scores.set_index("score_range")["count"])

        with chart2:
            st.markdown("#### Status Breakdown")
            status_data = get_status_breakdown()
            if status_data:
                df_status = pd.DataFrame(status_data)
                st.bar_chart(df_status.set_index("status")["count"])

        st.markdown("---")

        # Row 2: Jobs over time + Source breakdown
        chart3, chart4 = st.columns(2)

        with chart3:
            st.markdown("#### Jobs Added Over Time (30 days)")
            time_data = get_jobs_over_time(30)
            if time_data:
                df_time = pd.DataFrame(time_data)
                df_time["date"] = pd.to_datetime(df_time["date"])
                st.line_chart(df_time.set_index("date")["count"])
            else:
                st.caption("No time series data yet.")

        with chart4:
            st.markdown("#### Jobs by Source")
            source_data = get_jobs_by_source()
            if source_data:
                df_source = pd.DataFrame(source_data)
                st.bar_chart(df_source.set_index("source")["count"])

        st.markdown("---")

        # Row 3: Top companies
        st.markdown("#### Top Companies by Job Count")
        company_data = get_jobs_by_company(15)
        if company_data:
            df_companies = pd.DataFrame(company_data)
            st.dataframe(
                df_companies.rename(columns={
                    "company": "Company",
                    "count": "Jobs",
                    "avg_score": "Avg Score",
                }),
                use_container_width=True,
                hide_index=True,
            )

        st.markdown("---")

        # Row 4: Top scoring jobs
        st.markdown("#### 🏆 Top 10 Highest Scoring Jobs")
        top_jobs = get_top_scoring_jobs(10)
        if top_jobs:
            for tj in top_jobs:
                score = tj.get("match_score", 0)
                emoji = "🔥" if score >= 75 else "⭐" if score >= 50 else "📋"
                resume_status = "✅" if tj.get("resume_path") else "⏳"
                st.markdown(
                    f"{emoji} **[{score:.0f}]** {tj['company']} — {tj['title']}  |  "
                    f"📍 {tj.get('location', 'N/A')}  |  {resume_status}"
                )


# ═══════════════════════════════════════════════════════════════
# TAB: Pipeline History
# ═══════════════════════════════════════════════════════════════
with tab_history:
    st.markdown("### 🔄 Pipeline Run History")

    history = get_pipeline_history(20)
    if not history:
        st.info("No pipeline runs yet. Click 'Run Full Pipeline' in the sidebar to start.")
    else:
        for run in history:
            status_icon = "✅" if run.get("status", "").startswith("completed") else "🔄"
            if run.get("errors"):
                status_icon = "⚠️"

            duration = run.get("duration_s", 0) or 0
            started = run.get("started_at", "")[:19]

            with st.expander(
                f"{status_icon} Run #{run['id']} — {started} — "
                f"{run.get('new_jobs', 0)} new jobs — {duration:.0f}s",
                expanded=False,
            ):
                rc1, rc2, rc3, rc4 = st.columns(4)
                with rc1:
                    st.metric("New Jobs", run.get("new_jobs", 0))
                with rc2:
                    st.metric("Fetched", run.get("total_fetched", 0))
                with rc3:
                    st.metric("Resumes", run.get("resumes_gen", 0))
                with rc4:
                    st.metric("Alerts", run.get("alerts_sent", 0))

                if run.get("errors"):
                    st.warning(f"Errors: {run['errors']}")

                st.caption(
                    f"Started: {run.get('started_at', 'N/A')} | "
                    f"Finished: {run.get('finished_at', 'N/A')} | "
                    f"Status: {run.get('status', 'unknown')}"
                )


# ─── Footer ─────────────────────────────────────────────────────
st.markdown("---")
st.caption(
    f"Job Automation System v2.0 · "
    f"{counts['total']} jobs tracked · "
    f"Last refreshed: {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
    "Does not auto-apply"
)
