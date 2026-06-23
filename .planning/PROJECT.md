# Inbound Speed-to-Lead Email Pipeline

## What This Is

A GitHub Actions pipeline that generates one personalised inbound speed-to-lead email per sequence step for sales contacts who submitted a web enquiry. Triggered by Make.com when a contact enters a HubSpot sequence, it fetches contact and company data, calls the Claude API with the appropriate step context, and writes the generated email back to HubSpot contact properties for dispatch.

## Core Value

A prospect submits a web enquiry and receives a personalised, contextually relevant email within minutes — referencing exactly what they asked for, who they are, and where they sit in the sequence.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] Fetch contact properties and engagement history (emails, meetings, calls, notes) from HubSpot
- [ ] Fetch associated company (account) properties and notes from HubSpot
- [ ] Build personalised prompt from contact + company + history context
- [ ] Call Claude API to generate all 4 emails in the sequence at once
- [ ] Write generated subject/body for all 4 emails to HubSpot contact properties
- [ ] Create a note on the contact record summarising the generated email
- [ ] Retry transient API errors (429, 5xx) with exponential backoff
- [ ] Write DLQ record and upload artifact on pipeline failure
- [ ] POST Teams/Slack notification on failure

### Out of Scope

- Chorus call transcripts — inbound prospects have no prior calls; Chorus data not relevant
- EOFY or campaign-specific timing tokens — inbound sequence has no calendar dependency
- SDR call notes output — the prompt generates email only, not SDR prep material
- Sending emails directly — the pipeline writes to HubSpot; sending is handled by HubSpot sequences/Make.com

## Context

This system mirrors the Mid-Market pipeline (`C:\Users\irahfo\Outreach\Mid Market\Mid-Market\`) in architecture but is adapted for inbound enquiries:

- **Mid-Market**: Generates a 4-email campaign + SDR notes all at once for outbound contacts
- **Inbound**: Generates all 4 sequence emails at once for inbound form-fill contacts

The sequence covers 4 touch points: initial enquiry response, post-first-call follow-up, after several missed calls, and a break-up email. All 4 are generated in a single Claude call and written to HubSpot.

A separate enrichment agent has already run before this pipeline is triggered. It populates the HubSpot contact and company records with enriched data (industry, size, role context). The pipeline trusts those fields and passes them to the prompt as-is.

The prompt (`prompt.md`) instructs Claude to:
1. Scan all available contact + company data
2. Reference the highest-priority signal (form message > enrichment notes > role/company context)
3. Adjust tone and length based on seniority (C-level = sharp and brief; IC/Manager = more context)
4. Acknowledge we are NOT a recruitment firm (especially step 1)
5. Always aim to get the prospect to book a meeting

## Constraints

- **Tech Stack**: Python 3.12, GitHub Actions (ubuntu-latest), HubSpot private app token, Anthropic Claude API
- **Dependencies**: `hubspot-api-client>=12.0.0`, `requests>=2.31.0`, `beautifulsoup4>=4.12.0`, `anthropic>=0.30.0`, `tenacity>=9.0.0`
- **HubSpot Properties**: Step-specific custom properties must be created before first run (`inbound_s1_subject`, `inbound_s1_body`, etc.)
- **Secrets**: `HUBSPOT_API_KEY`, `ANTHROPIC_API_KEY`, `TEAMS_WEBHOOK_URL` (GitHub repo secrets)
- **Data quality**: Contact/company fields may be partial or missing — prompt and scripts must handle gracefully
- **No tiktoken**: Token counting not needed; inbound prompt is simpler and well within limits

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Generate all 4 emails in one Claude call (like Mid-Market) | Simpler orchestration; Make.com fires once per contact, not once per step | — Pending |
| Write-back properties `subject_1`–`subject_4` and `email_1`–`email_4` | Matches Mid-Market property naming; consistent across campaigns | — Pending |
| No Chorus integration | Inbound prospects have no prior call recordings; adding Chorus adds complexity with no benefit | — Pending |
| Reuse utils.py from Mid-Market unchanged | Same retry/DLQ patterns apply; no reason to diverge | — Pending |
| Company notes fetched via engagements API on the company object | Enrichment agent writes notes to the company record, not just the contact | — Pending |

---
*Last updated: 2026-06-24 after initialization*

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state
