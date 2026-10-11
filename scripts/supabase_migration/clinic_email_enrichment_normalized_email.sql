-- Adds normalized_email to the legacy public.clinic_email_enrichment table so it can be
-- joined against email_research.email_candidates.normalized_email without either side
-- reimplementing the other's normalization. Prepared offline; DO NOT apply to Production.
--
-- Additive only. No existing column is altered or dropped; no row is deleted.
--
-- IMPORTANT -- re-check immediately before apply, not just when this file was written:
--   select count(*) from public.clinic_email_enrichment;
-- This migration was authored assuming that count is 0 (legacy table not yet populated
-- for this feature). A column ADD + backfill UPDATE is still safe at any row count --
-- NOT NULL is intentionally *not* set here so a populated table can still be migrated --
-- but if the count is no longer 0, re-review the backfill UPDATE below (normalize() is
-- the Postgres built-in Unicode NFKC form, matching Python's
-- unicodedata.normalize("NFKC", v).strip().casefold() for the ASCII range email addresses
-- stay within; it is not a byte-for-byte guarantee outside ASCII) before trusting the
-- unique index it enables.
--
-- Uniqueness is scoped to (clinic_id, normalized_email), not normalized_email alone:
-- a single medical corporation with multiple locations, a shared front-desk inbox, or a
-- head-office contact can legitimately share one email across several clinic_id rows.
-- This mirrors email_research.email_candidates' own
-- unique(clinic_id, normalized_email) constraint deliberately -- same identity model on
-- both sides of the legacy relation.

begin;

alter table public.clinic_email_enrichment
  add column if not exists normalized_email text;

update public.clinic_email_enrichment
  set normalized_email = lower(btrim(normalize(email, nfkc)))
  where email is not null and normalized_email is null;

create unique index if not exists clinic_email_enrichment_normalized_email_key
  on public.clinic_email_enrichment(clinic_id, normalized_email)
  where normalized_email is not null;

commit;

-- Read-only verification after a separately approved apply:
-- select count(*) from public.clinic_email_enrichment where normalized_email is null and email is not null;
-- select clinic_id,normalized_email,count(*) from public.clinic_email_enrichment
--   group by clinic_id,normalized_email having count(*)>1;
