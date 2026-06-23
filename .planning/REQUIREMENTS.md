# Requirements — Inbound Speed-to-Lead Email Pipeline

## v1 Requirements

### Data Fetch

- [ ] **DATA-01**: Pipeline accepts `contact_id`, `contact_email`, and `step` as workflow_dispatch inputs
- [ ] **DATA-02**: `fetch_hubspot.py` fetches contact properties: `firstname`, `lastname`, `email`, `jobtitle`, `company`, `industry`, `num_employees`, `city`, `state`, `country`, `website`, `hubspot_owner_id`, `hs_lead_status`, and any custom enrichment properties
- [ ] **DATA-03**: `fetch_hubspot.py` fetches the associated company (account) record properties: `name`, `industry`, `numberofemployees`, `city`, `country`, `website`, `annualrevenue`, `description`
- [ ] **DATA-04**: `fetch_hubspot.py` fetches email history on the contact (past 12 months, HTML stripped)
- [ ] **DATA-05**: `fetch_hubspot.py` fetches meeting engagements on the contact (date, notes, internal notes)
- [ ] **DATA-06**: `fetch_hubspot.py` fetches call history on the contact (timestamp, disposition, notes)
- [ ] **DATA-07**: `fetch_hubspot.py` fetches notes engagements on both contact and company records (enrichment agent output)
- [ ] **DATA-08**: All data written to `$RUNNER_TEMP/hubspot_contact.json`

### Tokens

- [ ] **TOKEN-01**: `compute_campaign_tokens.py` writes `current_date` (ISO format) and `step` (passed through from env var) to `$RUNNER_TEMP/campaign_tokens.json`

### Generation

- [ ] **GEN-01**: `generate_campaign.py` reads `hubspot_contact.json` and `campaign_tokens.json` from RUNNER_TEMP
- [ ] **GEN-02**: `generate_campaign.py` assembles activity history string from emails + meetings + calls + notes
- [ ] **GEN-03**: `generate_campaign.py` substitutes `{{token.name}}` placeholders in `prompt_template.md`
- [ ] **GEN-04**: `generate_campaign.py` calls `claude-sonnet-4-6` with `max_tokens=4096`
- [ ] **GEN-05**: `generate_campaign.py` parses and validates JSON output `{"subject": "...", "body": "..."}`
- [ ] **GEN-06**: `generate_campaign.py` post-processes output: strips em/en dashes, validates required keys
- [ ] **GEN-07**: `generate_campaign.py` writes result to `$RUNNER_TEMP/campaign_output.json`

### Write-back

- [ ] **WRITE-01**: `write_hubspot.py` reads `campaign_output.json` and writes subject/body to step-specific contact properties (`inbound_s{n}_subject`, `inbound_s{n}_body`)
- [ ] **WRITE-02**: `write_hubspot.py` updates `inbound_generated_date` property on the contact
- [ ] **WRITE-03**: `write_hubspot.py` creates a note on the contact record via HubSpot v3 Notes API with generated email summary

### Error Handling

- [ ] **ERR-01**: All scripts implement exponential backoff retry (up to 6 attempts, 60s max) on 429 and 5xx errors
- [ ] **ERR-02**: Each script writes a DLQ sentinel at startup, updated with error details on failure
- [ ] **ERR-03**: GitHub Actions workflow copies `failed_contacts.json` and uploads as `failed-contacts` artifact on pipeline failure
- [ ] **ERR-04**: GitHub Actions workflow POSTs a Teams/Slack webhook notification on failure with contact_email, failed_step, error excerpt, and run log link

### CI/CD

- [ ] **CI-01**: GitHub Actions workflow file at `.github/workflows/campaign.yml`
- [ ] **CI-02**: Workflow triggered by `workflow_dispatch` with inputs: `contact_id`, `contact_email`, `step`
- [ ] **CI-03**: Workflow runs steps in order: `fetch_hubspot.py` → `compute_campaign_tokens.py` → `generate_campaign.py` → `write_hubspot.py`
- [ ] **CI-04**: Workflow uploads `campaign_output.json` as artifact with 7-day retention on success
- [ ] **CI-05**: Workflow uses secrets: `HUBSPOT_API_KEY`, `ANTHROPIC_API_KEY`, `TEAMS_WEBHOOK_URL`
- [ ] **CI-06**: Working directory set to `Inbound/` subfolder within the repo

## v2 Requirements (Deferred)

- Chorus integration for later-stage steps (7, 11, 14) when calls may have occurred
- Token count logging / prompt size monitoring
- Per-step skip logic (skip generation if email already written for this step)
- Automated test fixture with mock HubSpot/Claude responses

## Out of Scope

- Sending emails directly — handled by HubSpot sequences or Make.com
- SDR call notes generation — not part of inbound sequence
- EOFY or campaign-specific timing — inbound has no calendar dependency
- Multi-email generation in a single run — one step per dispatch
- Dashboard or reporting UI — HubSpot native reporting covers this

## Traceability

| REQ-ID | Phase |
|--------|-------|
| DATA-01–08 | Phase 2 |
| TOKEN-01 | Phase 3 |
| GEN-01–07 | Phase 4 |
| WRITE-01–03 | Phase 5 |
| ERR-01–04 | Phase 2–5 (each script) + Phase 6 |
| CI-01–06 | Phase 6 |
