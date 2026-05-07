CREATE TEMP TABLE IF NOT EXISTS dupe_ids (id int PRIMARY KEY);

INSERT INTO dupe_ids
SELECT id FROM (
  SELECT id, ROW_NUMBER() OVER (
    PARTITION BY LOWER(ats_identifier), ats
    ORDER BY jobs_found_count DESC, id ASC
  ) AS rn
  FROM companies
  WHERE ats IS NOT NULL AND ats_identifier IS NOT NULL
) ranked WHERE rn > 1
ON CONFLICT DO NOTHING;

INSERT INTO dupe_ids
SELECT id FROM (
  SELECT id, ROW_NUMBER() OVER (
    PARTITION BY LOWER(name)
    ORDER BY jobs_found_count DESC, id ASC
  ) AS rn
  FROM companies
) ranked WHERE rn > 1
ON CONFLICT DO NOTHING;

SELECT COUNT(*) AS dupes_found FROM dupe_ids;

-- Delete child rows in dependency order
DELETE FROM fetch_audit_logs WHERE company_id IN (SELECT id FROM dupe_ids);
DELETE FROM bronze_raw_jobs WHERE scan_run_id IN (
  SELECT id FROM scan_runs WHERE company_id IN (SELECT id FROM dupe_ids)
);
DELETE FROM scan_runs WHERE company_id IN (SELECT id FROM dupe_ids);
DELETE FROM jobs WHERE company_id IN (SELECT id FROM dupe_ids);
DELETE FROM fetch_audit_logs WHERE company_id IN (SELECT id FROM dupe_ids);

DO $$ BEGIN
  DELETE FROM company_intelligence WHERE company_id IN (SELECT id FROM dupe_ids);
EXCEPTION WHEN undefined_table THEN NULL; END $$;

DELETE FROM companies WHERE id IN (SELECT id FROM dupe_ids);

DROP INDEX IF EXISTS uq_companies_ats_slug;
CREATE UNIQUE INDEX uq_companies_ats_slug
  ON companies (ats, LOWER(ats_identifier))
  WHERE ats IS NOT NULL AND ats_identifier IS NOT NULL;

SELECT ats, COUNT(*) AS count FROM companies WHERE active=true GROUP BY ats ORDER BY count DESC;
SELECT COUNT(*) AS total_companies FROM companies WHERE active=true;
