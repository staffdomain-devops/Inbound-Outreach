# State — Inbound Speed-to-Lead Email Pipeline

## Project Reference

See: `.planning/PROJECT.md` (updated 2026-06-24)

**Core value:** A prospect submits a web enquiry and receives a personalised email within minutes, referencing exactly what they asked for.
**Current focus:** Phase 1 — Scaffolding

## Current Phase

**Phase 2: HubSpot Fetch**
Status: Not started
Goal: Fetch contact + company + all engagement history from HubSpot

## Phase History

| Phase | Status | Notes |
|-------|--------|-------|
| 1 | Complete | scripts/utils.py, requirements.txt, prompt_template.md |
| 2 | Not started | |
| 3 | Not started | |
| 4 | Not started | |
| 5 | Not started | |
| 6 | Not started | |

## Open Questions

- What are the exact HubSpot custom property names for the enrichment fields the enrichment agent writes? (Assumed: standard contact/company properties for now)
- Which Teams/Slack webhook URL is used for failure notifications? (Set as `TEAMS_WEBHOOK_URL` GitHub secret)

## Decisions Log

| Date | Decision | Context |
|------|----------|---------|
| 2026-06-24 | No Chorus integration | Inbound prospects have no prior call recordings |
| 2026-06-24 | Step-specific write-back properties | Preserves history; easier debugging |
| 2026-06-24 | Reuse Mid-Market utils.py unchanged | Same retry/DLQ patterns apply |
| 2026-06-24 | Company notes via engagements API | Enrichment agent writes to company record |
