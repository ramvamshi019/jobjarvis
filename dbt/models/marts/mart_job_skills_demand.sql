-- mart_job_skills_demand: which skills are most in-demand, by role and time window
-- Refreshed daily — used by the /analytics/skills/trending API and dashboards.

with skills as (
    select * from {{ ref('stg_skills') }}
),

-- Last 7 days
last_7d as (
    select
        skill,
        role_category,
        country,
        count(distinct job_id)          as jobs_7d,
        count(*)                        as occurrences_7d
    from skills
    where first_seen_at >= current_timestamp - interval '7 days'
      and skill_type = 'required'
    group by 1, 2, 3
),

-- Last 30 days
last_30d as (
    select
        skill,
        role_category,
        country,
        count(distinct job_id)          as jobs_30d,
        count(*)                        as occurrences_30d
    from skills
    where first_seen_at >= current_timestamp - interval '30 days'
      and skill_type = 'required'
    group by 1, 2, 3
),

-- All time (for context)
all_time as (
    select
        skill,
        role_category,
        country,
        count(distinct job_id)          as jobs_total,
        count(*)                        as occurrences_total,
        min(first_seen_at)              as first_seen,
        max(first_seen_at)              as last_seen
    from skills
    where skill_type = 'required'
    group by 1, 2, 3
),

combined as (
    select
        a.skill,
        a.role_category,
        a.country,
        coalesce(l7.jobs_7d, 0)         as jobs_requiring_7d,
        coalesce(l30.jobs_30d, 0)       as jobs_requiring_30d,
        a.jobs_total,
        a.occurrences_total,
        a.first_seen,
        a.last_seen,
        -- Week-over-week growth (if we have enough data)
        case
            when coalesce(l30.jobs_30d, 0) > 0
            then round(
                (coalesce(l7.jobs_7d, 0) * 4.0 / l30.jobs_30d - 1) * 100, 1
            )
        end                             as wow_growth_pct
    from all_time a
    left join last_7d  l7  using (skill, role_category, country)
    left join last_30d l30 using (skill, role_category, country)
)

select
    {{ dbt_utils.generate_surrogate_key(['skill', 'role_category', 'country']) }} as skill_demand_key,
    skill,
    role_category,
    country,
    jobs_requiring_7d,
    jobs_requiring_30d,
    jobs_total,
    occurrences_total,
    first_seen,
    last_seen,
    wow_growth_pct,
    current_timestamp                   as refreshed_at
from combined
where jobs_requiring_30d > 0
order by jobs_requiring_30d desc
