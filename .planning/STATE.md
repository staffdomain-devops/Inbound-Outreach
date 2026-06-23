# State — Inbound Speed-to-Lead Email Pipeline

## Project Reference

See: `.planning/PROJECT.md` (updated 2026-06-24)

**Core value:** A prospect submits a web enquiry and receives a personalised email within minutes, referencing exactly what they asked for.
**Current focus:** Phase 1 — Scaffolding

## Current Phase

**Phase 1: Scaffolding**
Status: Not started
Goal: Establish folder structure, shared utilities, Python dependencies

## Phase History

| Phase | Status | Notes |
|-------|--------|-------|
| 1 | Not started | |
| 2 | Not started | |
| 3 | Not started | |
| 4 | Not started | |
| 5 | Not started | |
| 6 | Not started | |

## Open Questions

- What are the exact HubSpot custom property names for the enrichment fields the enrichment agent writes? (Assumed: standard contact/company properties for now)
- What are the exact property names for step-specific email write-back? (Decided: `inbound_s{n}_subject`, `inbound_s{n}_body` — create these in HubSpot before first run)
- Which Teams/Slack webhook URL is used for failure notifications? (Set as `TEAMS_WEBHOOK_URL` GitHub secret)

## Decisions Log

| Date | Decision | Context |
|------|----------|---------|
| 2026-06-24 | No Chorus integration | Inbound prospects have no prior call recordings |
| 2026-06-24 | Step-specific write-back properties | Preserves history; easier debugging |
| 2026-06-24 | Reuse Mid-Market utils.py unchanged | Same retry/DLQ patterns apply |
| 2026-06-24 | Company notes via engagements API | Enrichment agent writes to company record |
