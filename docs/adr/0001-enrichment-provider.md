# ADR 0001: Enrichment provider

**Status:** accepted, 2026-09-25

## Context

The brief asks for a people-profile API on a free plan and names People Data Labs (PDL)
and FullContact as examples. Input rows carry a first and last name and, sometimes, an
email or company. The output must include employment history, not just a current title.
The whole POC must fit in free credits.

## Options considered (vendor docs checked 2026-09-25)

| Provider | Free allowance | Bare name accepted? | Work history? | Sandbox? | Notes |
|---|---|---|---|---|---|
| **People Data Labs** | 100 credits/month per product; Enrichment bills only on a match, 404 is free; Identify bills every call | Enrichment no, Identify yes | Yes (`experience[]`) | Yes, synthetic, free | Work email needed to sign up; one API key on the free plan; contact and fine location fields obscured |
| **Diffbot Enhance** | 10,000 credits/month, 25 per person record | Yes | Yes (`employments[]`) | No | No card; best fallback |
| Coresignal | 2,000 trial credits for 7 days | Yes (search) | Yes | No | Short trial window |
| Apollo.io | ~75 credits/month | No (needs domain or email) | Yes | No | |
| Hunter.io | 50 credits/month | No | No | No | Email finding only |
| FullContact | No public free tier; sales-gated | No (name needs postal address) | Yes | No | Assets acquired by Ziff Davis 2024 |
| Clearbit, Proxycurl | Discontinued | – | – | – | |

## Decision

- **Primary: People Data Labs.** Person Enrichment for rows that carry an email,
  LinkedIn URL, or name plus company/location. Person Identify for name-only rows,
  gated by score thresholds and its own credit budget because it bills on no-match.
- **Fallback: Diffbot Enhance**, wired behind the same `Provider` interface and used
  when PDL returns HTTP 402 or the monthly budget guard trips.
- **Development and CI use a `MockProvider`** with fixtures recorded from the PDL
  sandbox (synthetic people, zero credits, no real PII in the repo).

## Consequences

- The matching ladder (email → LinkedIn → name+context → name-only) and the
  `ambiguous` status become first-class parts of the pipeline.
- Credit accounting reads `x-call-credits-spent` and `x-totallimit-remaining` and is
  enforced by a DynamoDB counter, not by trusting the API alone.
- Only one PDL key exists on the free plan; it lives in SSM Parameter Store and is
  rolled from the dashboard if ever exposed.
- Free-plan PDL responses return location fields other than country as booleans, so
  `location_name` is nullable in the curated schema.
