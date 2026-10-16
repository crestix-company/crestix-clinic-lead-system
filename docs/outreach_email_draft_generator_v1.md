# Clinic Outreach Email Draft Generator v1 — Integration Plan

Status: DESIGN ONLY / no production migrations, writes, jobs, or Gmail draft creation authorized by this document.
Repository: crestix-company/crestix-clinic-lead-system
Target: existing Streamlit app (app_v2.py), Supabase runtime; no standalone app.
Last reviewed: 2026-10-11

## Goal
Add a new 「営業メール」 section to the existing Clinic Lead System. Reuse Clinic Master IDs, existing verified email enrichment, and existing Mynavi article/profile mappings (if present). Support preview and batch generation, then optional Gmail drafts in info@crestix-inc.com; NEVER auto-send.

## Existing-code observations (read-only)
- app_v2.py: Streamlit UI with NAV and production store selection.
- src/repository/runtime_store.py: Supabase-only runtime facade; verified email mapping exists.
- Current Git tree has no obvious Mynavi article dedicated module. Before implementing a match adapter, locate the previously developed article master/index and its Clinic Master joins; do not assume it is deployed.
- CURRENT_SYSTEM_STATUS.md updated 2026-10-05 and may be stale. Verify live schema/main before choosing new tables.

## Fixed template
Subject: 【お伝えしておりましたマイナビ記事について】株式会社Crestix {IS担当者名}

{クリニック名}
{役職} {院長・理事長名} 様

お世話になっております。
株式会社Crestixの{IS担当者名}と申します。

本日、貴院へお電話させていただき、ご対応いただいた方よりメールにてお送りするよう伺いましたので、ご連絡いたしました。

現在、マイナビにて掲載させていただいている下記記事について、掲載データの共有・現状のご報告と、掲載内容の確認をさせていただければと考えております。

【記事例】
{実在する記事例URL}

【掲載いただいている記事】
{確認済み掲載記事URLまたは医院プロフィールURL}

なお、掲載内容についての行き違い防止のため、お打ち合わせ当日は院長先生にもご参加いただく形でお願いしております。

恐れ入りますが、院長先生がご参加可能な日時を下記よりお知らせいただけますでしょうか。

{候補日時（原則5営業日）}

上記よりオンラインにて20分～30分ほど頂戴できますと幸いです。
何卒よろしくお願い申し上げます。

******************************************
株式会社Crestix 営業窓口
Phone：080-7323-8071
Mail：info@crestix-inc.com

〒101-0054
東京都千代田区神田錦町1丁目21−2
大手町モダンビルディング 4F
******************************************

IMPORTANT: The live example describes a Mynavi article but its 「掲載いただいている記事」 URL may point to a clinic profile. Do not present a profile as confirmed editorial article placement. Route cases without confirmed article placement to review or an explicitly approved article-not-listed template. Preserve the customer-approved wording otherwise.

## Components and contracts
1. Email recipient: verified clinic primary email only; keep unknown/multi-recipient cases in REVIEW.
2. Article lookup adapter: clinic_uuid -> {listing_status, profile_url, listed_article_urls[], example_article_url, provenance, confidence}; use the prior READ-ONLY article master; never fabricate URLs or use uncontrolled LLM search.
3. Preview renderer: deterministic template v1, Japanese weekday/date/time formatting (Asia/Tokyo), sender staff selected from approved list, role/name null-safe.
4. Schedule provider: configurable FS calendars and availability policy; do not combine free slots of different FS; allow human-approved fixed windows, never claim unsupported availability.
5. Batch queue: idempotency key (clinic_uuid, template_version, campaign_id), bounded concurrency, durable checkpoints, retry with backoff, resume; do not share HP worker lifecycle.
6. Gmail adapter: OAuth authorization to actual info@crestix-inc.com mailbox; a From alias alone is insufficient. Draft creation only on explicit action; no Gmail send permission required if drafts-only scope suffices.
7. Storage: separate migration proposal for draft jobs, draft entries, matching evidence, gmail_draft_id, created_by, review_status and timestamps. No production DB writes before human audit and approved migration.
8. UI: keep existing screens; add sidebar 「営業メール」 with filtering, IS selector, preview, review exceptions, batch progress, and explicit 「Gmail下書き作成」.

## Gates
- G0 READ ONLY: locate canonical article index, validate joined Clinic UUID and email source; report counts and match confidence.
- G1 Unit tests: fixed text and signature, date/weekday, missing data, article profile distinction, no fabricated URLs.
- G2 Preview-only 50 real cases with full human audit. No Gmail writes.
- G3 Staging Gmail draft test with authorized account; verify draft stored in target mailbox.
- G4 50 then 500 then 1,000; expand only after explicit release approval and no critical mismatches.
- Safety invariant: existing HP automatic research run is never stopped/restarted; no production migrations or database writes until approved; existing Comdesk output unchanged.

## Implementation sequence
A. Audit legacy article linking and runtime schema.
B. Build template, email input adapter, and article adapter behind default-OFF feature flag.
C. Add preview-only and deterministic batch queue.
D. Add calendar integration and Gmail draft adapter separately.
E. Test, request production migration and OAuth approval, then canary.

## Acceptance
- Correct Clinic UUID, To, sender name, article provenance, and URL for each generated row.
- No claim of editorial placement from profile URL alone.
- No mail is sent by this feature.
- No duplicates after retries.
- Review queue contains failures, mismatches, and incomplete records.
- Existing Streamlit UI, HP research, email research, Comdesk exports, and Production DB untouched until release gates.
