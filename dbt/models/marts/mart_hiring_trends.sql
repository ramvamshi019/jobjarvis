-- mart_hiring_trends: daily job posting volume by role, ATS, and country
-- Powers the /analytics/market/trends API endpoint and time-series dashboards.

with jobs as (
    select * from {{ ref('stg_jobs') }}
),

companies as (
    select * from {{ ref('stg_companies') }}
),

daily_volume as (
    select
        date_trunc('day', j.first_seen_at)::date     as posting_date,
        j.role_category,
        j.country,
        j.remote_type,
        j.experience_level,
        c.ats_type,
        c.industry,

        count(j.job_id)                              as jobs_posted,
        count(distinct j.company_id)                 as unique_companies,
        avg(j.salary_mid_usd)                        as avg_salary_mid,
        count(j.salary_mid_usd)                      as salary_sample_size,
        avg(j.freshness_score)                       as avg_freshness_score

    from jobs j
    left join companies c on j.company_id = c.company_id
    group by 1, 2, 3, 4, 5, 6, 7
),

-- 7-day rolling average for smoothing
with_rolling_avg as (
    select
        *,
        avg(jobs_posted) over (
            partition by role_category, country, ats_type
            order by posting_date
            rows between 6 preceding and current row
        )                                            as jobs_7d_rolling_avg
    from daily_volume
)

select
    {{ dbt_utils.generate_surrogate_key(['posting_date', 'role_category', 'country', 'ats_type']) }}
                                                     as trend_key,
    posting_date,
    role_category,
    country,
    remote_type,
    experience_level,
    ats_type,
    industry,
    jobs_posted,
    unique_companies,
    round(avg_salary_mid::numeric, 0)::int           as avg_salary_mid,
    salary_sample_size,
    round(avg_freshness_score::numeric, 3)           as avg_freshness_score,
    round(jobs_7d_rolling_avg::numeric, 1)           as jobs_7d_rolling_avg,
    current_timestamp                                as refreshed_at
from with_rolling_avg
order by posting_date desc, jobs_posted desc
