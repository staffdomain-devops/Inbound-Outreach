# Roadmap — Inbound Speed-to-Lead Email Pipeline

**6 phases** | **22 requirements mapped** | All v1 requirements covered ✓

| # | Phase | Goal | Requirements | Success Criteria |
|---|-------|------|--------------|-----------------|
| 1 | Scaffolding | Folder structure, shared utilities, dependencies | DATA-01 (partial), ERR-01–02 | 3 |
| 2 | HubSpot Fetch | Contact + company + engagement history | DATA-01–08, ERR-01–02 | 4 |
| 3 | Tokens | Step passthrough + current_date | TOKEN-01 | 2 |
| 4 | Generate | Prompt assembly + Claude API call + output validation | GEN-01–07, ERR-01–02 | 4 |
| 5 | Write-back | Properties + note written to HubSpot | WRITE-01–03, ERR-01–02 | 3 |
| 6 | CI/CD | GitHub Actions workflow + failure handling | CI-01–06, ERR-03–04 | 4 |

---

### Phase 1: Scaffolding

**Goal:** Establish the project folder structure, shared utilities, and Python dependencies so all subsequent phases can build on a clean foundation.
**Mode:** mvp

**Requirements:** DATA-01 (partial), ERR-01–02

**Success Criteria:**
1. `Inbound/scripts/utils.py` exists with `write_dlq`, `_is_hubspot_transient`, `_is_requests_transient`, `_is_anthropic_transient`, `RetryAfterWait`, `HubSpotRetryAfterWait`, `HS_RETRY_KWARGS`, `REQ_RETRY_KWARGS`, `ANTHROPIC_RETRY_KWARGS`, `safe_truncate`
2. `Inbound/requirements.txt` lists all five Python dependencies with minimum version constraints
3. `Inbound/prompt_template.md` contains the inbound sequence prompt with `{{token.name}}` placeholders
4. Folder structure matches the spec: `scripts/`, `requirements.txt`, `prompt_template.md`

---

### Phase 2: HubSpot Fetch

**Goal:** `fetch_hubspot.py` fetches contact properties, associated company record, and all engagement history (emails, meetings, calls, notes on both contact and company), then writes `hubspot_contact.json` to RUNNER_TEMP.
**Mode:** mvp

**Requirements:** DATA-01–08, ERR-01–02

**Success Criteria:**
1. Running `python scripts/fetch_hubspot.py` with valid env vars writes `hubspot_contact.json` containing `contact_properties`, `company_properties`, `email_history`, `meeting_engagements`, `call_history`, `contact_notes`, `company_notes`
2. Missing company association is handled gracefully (empty dict, no crash)
3. Missing or empty engagement history (emails, meetings, calls, notes) results in empty arrays, not errors
4. Script writes DLQ sentinel at startup and updates on failure; all HubSpot/requests calls wrapped in tenacity retry

---

### Phase 3: Tokens

**Goal:** `compute_campaign_tokens.py` reads the `INPUT_STEP` env var and writes `campaign_tokens.json` with `step` and `current_date`.
**Mode:** mvp

**Requirements:** TOKEN-01

**Success Criteria:**
1. `campaign_tokens.json` contains `step` (string) and `current_date` (ISO date string)
2. Invalid or missing `INPUT_STEP` raises a clear error

---

### Phase 4: Generate

**Goal:** `generate_campaign.py` assembles the personalised prompt from all context, calls the Claude API, parses the response, and writes `campaign_output.json` with `{"subject": "...", "body": "..."}`.
**Mode:** mvp

**Requirements:** GEN-01–07, ERR-01–02

**Success Criteria:**
1. Prompt assembly includes contact properties, company properties, activity history (emails/meetings/calls/notes), and step value via `{{token.name}}` substitution
2. Claude API called with `claude-sonnet-4-6`, `max_tokens=4096`; `max_retries=0` on the SDK client
3. `campaign_output.json` written with valid `subject` and `body` keys
4. `stop_reason == "max_tokens"` raises a clear error; invalid JSON saves raw response to RUNNER_TEMP before raising

---

### Phase 5: Write-back

**Goal:** `write_hubspot.py` reads `campaign_output.json` and writes the generated email to step-specific HubSpot contact properties, plus creates a note on the contact record.
**Mode:** mvp

**Requirements:** WRITE-01–03, ERR-01–02

**Success Criteria:**
1. Contact property `inbound_s{n}_subject` and `inbound_s{n}_body` updated (where `n` is the step value from env var)
2. `inbound_generated_date` set to today's date
3. A note created on the contact record via HubSpot v3 Notes API; note failure is non-fatal (logs warning, continues)

---

### Phase 6: CI/CD

**Goal:** `.github/workflows/campaign.yml` orchestrates all scripts end-to-end, handles artifact upload, and sends failure notifications.
**Mode:** mvp

**Requirements:** CI-01–06, ERR-03–04

**Success Criteria:**
1. Workflow triggered by `workflow_dispatch` with three string inputs: `contact_id`, `contact_email`, `step`
2. Steps run in order; each passes contact_id, contact_email, step via env vars; data flows through `$RUNNER_TEMP` JSON files
3. `campaign_output.json` uploaded as artifact (7-day retention) on workflow success
4. On failure: `failed_contacts.json` artifact uploaded AND Teams webhook POSTed with contact_email, step, error excerpt, and run URL
