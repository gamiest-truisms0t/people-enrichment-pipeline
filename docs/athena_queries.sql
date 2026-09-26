-- Athena queries answering the brief's three questions, plus an operational view.
-- Terraform (infra/modules/catalog) creates the Glue database people_enrichment_<env>
-- with partition projection over batch_date, and saves these four queries in the
-- workgroup people-enrichment-<env>-analytics. Run them with `make athena-verify` or in
-- the Athena console with that workgroup selected. Restrict batch_date to keep scans
-- small; every table is one Parquet file per batch.

-- 1. Who are the individuals identified?
SELECT batch_date,
       full_name,
       current_job_title,
       current_company_name,
       location_country,
       linkedin_url,
       match_likelihood,
       lookup_method,
       quality_flags
FROM people_enrichment_dev.dim_person
WHERE batch_date >= CAST(current_date - interval '30' day AS varchar)
ORDER BY batch_date DESC, full_name;

-- 2. What companies have they worked at?
SELECT p.batch_date,
       p.full_name,
       e.company_name,
       e.company_industry,
       e.start_date,
       e.end_date,
       e.is_current
FROM people_enrichment_dev.fact_employment e
JOIN people_enrichment_dev.dim_person p
  ON p.person_id = e.person_id
 AND p.batch_id = e.batch_id
 AND p.batch_date = e.batch_date
WHERE p.batch_date = '2026-09-25'
ORDER BY p.full_name, e.sequence_no;

-- 3. What roles have they held?
SELECT p.full_name,
       e.title_name,
       e.title_role,
       e.title_levels,
       e.company_name,
       e.start_date,
       e.end_date
FROM people_enrichment_dev.fact_employment e
JOIN people_enrichment_dev.dim_person p
  ON p.person_id = e.person_id
 AND p.batch_id = e.batch_id
 AND p.batch_date = e.batch_date
WHERE p.batch_date = '2026-09-25'
ORDER BY p.full_name, e.sequence_no;

-- 4. Operational: outcome per input row and credits per batch.
SELECT batch_date,
       batch_id,
       status,
       lookup_method,
       count(*)                  AS rows,
       sum(credits_consumed)     AS credits,
       round(avg(likelihood), 1) AS avg_likelihood
FROM people_enrichment_dev.fact_lookup
WHERE batch_date = '2026-09-25'
GROUP BY 1, 2, 3, 4
ORDER BY batch_id, status, lookup_method;

-- Bonus: which companies appear most across the identified people?
SELECT e.company_name, count(DISTINCT e.person_id) AS people
FROM people_enrichment_dev.fact_employment e
WHERE e.batch_date = '2026-09-25'
GROUP BY 1
ORDER BY 2 DESC, 1
LIMIT 20;

-- 5. Latest snapshot per person. dim_person holds one row per person per batch, so a
--    registrant uploaded several times appears once per upload; this keeps the most recent
--    enrichment of each person for cross-batch questions.
SELECT full_name,
       current_job_title,
       current_company_name,
       location_country,
       linkedin_url,
       match_likelihood,
       quality_flags,
       batch_date,
       batch_id
FROM (
    SELECT p.*,
           row_number() OVER (PARTITION BY person_id ORDER BY enriched_at DESC) AS recency
    FROM people_enrichment_dev.dim_person p
)
WHERE recency = 1
ORDER BY full_name;
