-- stg_jobs: clean, typed, deduplicated view of raw job postings
-- Removes nulls, normalises text casing, and applies basic filters.

with source as (
    select * from {{ source('jobjarvis', 'jobs') }}
),

cleaned as (
    select
        id                                              as job_id,
        company_id,
        external_id,

        -- Title normalisation
        initcap(trim(title))                            as title,
        initcap(trim(normalized_title))                 as normalized_title,

        -- Company
        trim(company_name)                              as company_name,

        -- Location
        trim(location)                                  as location,
        trim(city)                                      as city,
        trim(region)                                    as region,
        upper(trim(country))                            as country,
        lower(coalesce(remote_type, 'unknown'))         as remote_type,

        -- URLs
        url                                             as job_url,
        apply_url,

        -- Classification
        lower(coalesce(employment_type, 'unknown'))     as employment_type,
        lower(coalesce(experience_level, 'unknown'))    as experience_level,
        coalesce(role_category, 'Other')                as role_category,
        role_confidence,

        -- Salary (annualised USD only, nullify implausible values)
        case
            when salary_currency = 'USD'
             and salary_min between 10000 and 2000000
            then salary_min
        end                                             as salary_min_usd,
        case
            when salary_currency = 'USD'
             and salary_max between 10000 and 2000000
            then salary_max
        end                                             as salary_max_usd,
        case
            when salary_currency = 'USD'
             and salary_min between 10000 and 2000000
             and salary_max between 10000 and 2000000
            then (salary_min + salary_max) / 2.0
        end                                             as salary_mid_usd,

        -- Skills
        coalesce(required_skills, '[]'::json)           as required_skills,
        coalesce(preferred_skills, '[]'::json)          as preferred_skills,

        -- Quality
        coalesce(spam_score, 0)                         as spam_score,
        coalesce(freshness_score, 0)                    as freshness_score,
        coalesce(data_quality_score, 0)                 as data_quality_score,
        freshness_label,
        source,

        -- Dedup
        fingerprint,

        -- Timing
        posted_at,
        first_seen_at,
        last_seen_at,
        active,

        -- Age in days
        extract(day from now() - first_seen_at)         as age_days

    from source
    where
        active = true
        and spam_score < 0.6
        and title is not null
        and length(trim(title)) > 2
)

select * from cleaned
