-- stg_skills: unnested skills per job (one row per job-skill pair)
-- Uses PostgreSQL's jsonb_array_elements_text to flatten required_skills JSON arrays.

with jobs as (
    select * from {{ ref('stg_jobs') }}
),

unnested as (
    select
        j.job_id,
        j.company_id,
        j.company_name,
        j.role_category,
        j.experience_level,
        j.country,
        j.first_seen_at,
        j.posted_at,
        trim(skill.value)                   as skill,
        'required'                          as skill_type
    from jobs j,
         jsonb_array_elements_text(j.required_skills::jsonb) as skill(value)
    where j.required_skills::text != '[]'
      and j.required_skills::text != 'null'

    union all

    select
        j.job_id,
        j.company_id,
        j.company_name,
        j.role_category,
        j.experience_level,
        j.country,
        j.first_seen_at,
        j.posted_at,
        trim(skill.value)                   as skill,
        'preferred'                         as skill_type
    from jobs j,
         jsonb_array_elements_text(j.preferred_skills::jsonb) as skill(value)
    where j.preferred_skills::text != '[]'
      and j.preferred_skills::text != 'null'
)

select
    {{ dbt_utils.generate_surrogate_key(['job_id', 'skill', 'skill_type']) }} as skill_job_key,
    *
from unnested
where
    skill is not null
    and length(skill) between 1 and 100
