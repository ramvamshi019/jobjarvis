-- stg_companies: clean company registry with derived tier labels

with source as (
    select * from {{ source('jobjarvis', 'companies') }}
),

cleaned as (
    select
        id                                               as company_id,
        trim(name)                                       as company_name,
        lower(trim(domain))                              as domain,
        lower(coalesce(ats, 'unknown'))                  as ats_type,
        trim(ats_identifier)                             as ats_identifier,
        upper(coalesce(country, 'US'))                   as country,
        lower(coalesce(industry, 'unknown'))             as industry,
        lower(coalesce(size_range, 'unknown'))           as size_range,

        -- Priority tier (derived from priority_score)
        case
            when priority_score >= 90 then 'tier1'
            when priority_score >= 60 then 'tier2'
            when priority_score >= 20 then 'tier3'
            else 'tier4'
        end                                              as scan_tier,

        priority_score,
        scan_frequency_minutes,
        jobs_found_count,
        consecutive_failures,
        failure_count,
        active,

        last_scanned_at                                  as last_checked_at,
        last_success_at,
        next_scan_at,
        created_at,
        updated_at

    from source
    where active = true
)

select * from cleaned
