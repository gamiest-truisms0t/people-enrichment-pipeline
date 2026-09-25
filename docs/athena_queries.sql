-- Athena queries answering the brief's three questions.
-- Database and tables are created by Terraform (infra/modules/catalog) in Phase 5.
-- Replace the batch_date literal with the partition you want to inspect.

-- 1. Who are the individuals identified?
SELECT full_name,
       current_job_title,
       current_company_name,
       location_country,
       linkedin_url,
       match_likelihood
FROM people_enrichment.dim_person
WHERE batch_date = '2026-10-01'
ORDER BY full_name;

-- 2. What companies have they worked at?
SELECT p.full_name,
       e.company_name,
       e.company_industry,
       e.start_date,
       e.end_date,
       e.is_current
FROM people_enrichment.fact_employment e
JOIN people_enrichment.dim_person p
  ON p.person_id = e.person_id AND p.batch_date = e.batch_date
WHERE e.batch_date = '2026-10-01'
ORDER BY p.full_name, e.sequence_no;

-- 3. What roles have they held?
SELECT p.full_name,
       e.title_name,
       e.title_role,
       e.title_levels,
       e.company_name
FROM people_enrichment.fact_employment e
JOIN people_enrichment.dim_person p
  ON p.person_id = e.person_id AND p.batch_date = e.batch_date
WHERE e.batch_date = '2026-10-01'
ORDER BY p.full_name, e.sequence_no;

-- Operational: match rate and credit use per batch.
SELECT batch_id,
       status,
       count(*)              AS rows,
       sum(credits_consumed) AS credits
FROM people_enrichment.fact_lookup
WHERE batch_date = '2026-10-01'
GROUP BY 1, 2
ORDER BY 1, 2;
