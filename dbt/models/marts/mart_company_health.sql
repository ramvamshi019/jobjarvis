-- mart_company_health: per-company hiring activity and reliability metrics
-- Used for company scoring, prioritisation, and the hiring dashboard.

with companies as (
    select * from {{ ref('stg_companies') }}
),

jobs as (
    select * from {{ ref('stg_jobs') }}
),

job_stats as (
    select
        company_id,
        count(*)                                                        as total_jobs_active,
        count(case when first_seen_at >= current_timestamp - interval '7 days'  then 1 end) as jobs_7d,
        count(case when first_seen_at >= current_timestamp - interval '30 days' then 1 end) as jobs_30d,
        count(case when first_seen_at >= current_timestamp - interval '90 days' then 1 end) as jobs_90d,
        max(first_seen_at)                                              as latest_job_at,
        min(first_seen_at)                                              as earliest_job_at,
        round(avg(salary_mid_usd)::numeric, 0)::int                    as avg_salary,
        count(salary_mid_usd)                                           as salary_data_points,
        -- Most common role at this company
        mode() within group (order by role_category)                    as primary_role,
        -- Remote friendliness
        round(
            100.0 * sum(case when remote_type = 'remote' then 1 else 0 end)
            / nullif(count(*), 0)
        , 1)                                                            as remote_pct
    from jobs
    group by company_id
),

final as (
    select
        c.company_id,
        c.company_name,
        c.domain,
        c.ats_type,
        c.country,
        c.industry,
        c.size_range,
        c.scan_tier,
        c.priority_score,
        c.consecutive_failures,

        -- Job activity
        coalesce(js.total_jobs_active, 0)   as total_jobs_active,
        coalesce(js.jobs_7d, 0)             as jobs_7d,
        coalesce(js.jobs_30d, 0)            as jobs_30d,
        coalesce(js.jobs_90d, 0)            as jobs_90d,
        js.latest_job_at,
        js.earliest_job_at,
        js.primary_role,
        coalesce(js.remote_pct, 0)          as remote_pct,

        -- Salary benchmarks
        js.avg_salary,
        js.salary_data_points,

        -- Health signal: companies actively hiring with low failures score higher
        case
            when c.consecutive_failures >= 5 then 'unhealthy'
            when c.consecutive_failures >= 3 then 'degraded'
            when coalesce(js.jobs_7d, 0) >= 5 then 'high_activity'
            when coalesce(js.jobs_30d, 0) > 0 then 'active'
            else 'low_activity'
        end                                 as health_status,

        -- Days since last job posted
        extract(day from current_timestamp - js.latest_job_at)::int as days_since_last_job,

        c.last_checked_at,
        c.created_at

    from companies c
    left join job_stats js on c.company_id = js.company_id
)

select
    {{ dbt_utils.generate_surrogate_key(['company_id']) }} as company_health_key,
    *,
    current_timestamp as refreshed_at
from final
order by jobs_30d desc, priority_score desc
