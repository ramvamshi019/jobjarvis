-- mart_salary_insights: salary statistics aggregated by role, experience, country
-- Powers the /analytics/salary/insights API and compensation benchmarking.

with jobs as (
    select *
    from {{ ref('stg_jobs') }}
    where salary_min_usd is not null
      and salary_max_usd is not null
),

agg as (
    select
        role_category,
        experience_level,
        country,
        remote_type,
        employment_type,

        -- Core stats
        count(*)                                          as sample_size,
        round(avg(salary_min_usd)::numeric, 0)::int       as avg_salary_min,
        round(avg(salary_max_usd)::numeric, 0)::int       as avg_salary_max,
        round(avg(salary_mid_usd)::numeric, 0)::int       as avg_salary_mid,
        round(min(salary_min_usd)::numeric, 0)::int       as min_salary,
        round(max(salary_max_usd)::numeric, 0)::int       as max_salary,
        round(stddev(salary_mid_usd)::numeric, 0)::int    as stddev_salary,

        -- Percentiles
        round(percentile_cont(0.10) within group (order by salary_mid_usd)::numeric, 0)::int as p10,
        round(percentile_cont(0.25) within group (order by salary_mid_usd)::numeric, 0)::int as p25,
        round(percentile_cont(0.50) within group (order by salary_mid_usd)::numeric, 0)::int as p50_median,
        round(percentile_cont(0.75) within group (order by salary_mid_usd)::numeric, 0)::int as p75,
        round(percentile_cont(0.90) within group (order by salary_mid_usd)::numeric, 0)::int as p90,

        -- Freshness (weight recent data more)
        count(case when first_seen_at >= current_timestamp - interval '30 days' then 1 end) as last_30d_count,
        count(case when first_seen_at >= current_timestamp - interval '7 days'  then 1 end) as last_7d_count

    from jobs
    group by 1, 2, 3, 4, 5
    having count(*) >= 3  -- need at least 3 data points for meaningful stats
)

select
    {{ dbt_utils.generate_surrogate_key(['role_category', 'experience_level', 'country', 'remote_type', 'employment_type']) }}
                                                          as salary_key,
    role_category,
    experience_level,
    country,
    remote_type,
    employment_type,
    sample_size,
    avg_salary_min,
    avg_salary_max,
    avg_salary_mid,
    min_salary,
    max_salary,
    stddev_salary,
    p10, p25, p50_median, p75, p90,
    last_30d_count,
    last_7d_count,
    'USD'                                                 as currency,
    'annual'                                              as period,
    current_timestamp                                     as refreshed_at
from agg
order by role_category, experience_level, country
