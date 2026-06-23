# Inbound Speed-to-Lead Email Pipeline

## What This Is

GitHub Actions pipeline that generates one personalised email per sequence step for inbound enquiries. Triggered by Make.com → GitHub Actions `workflow_dispatch`. Fetches HubSpot contact + company data, calls Claude API, writes email back to HubSpot.

## Folder Structure

```
Inbound/
├── scripts/
│   ├── utils.py                    — shared retry, DLQ, wait helpers
│   ├── fetch_hubspot.py            — contact + company + engagement history
│   ├── compute_campaign_tokens.py  — step passthrough + current_date
│   ├── generate_campaign.py        — prompt assembly + Claude API call
│   └── write_hubspot.py            — write subject/body to HubSpot + note
├── prompt_template.md              — Claude prompt with {{token.name}} placeholders
├── requirements.txt
└── .github/workflows/campaign.yml
```

## Data Flow

```
Make.com → workflow_dispatch(contact_id, contact_email, step)
  → fetch_hubspot.py   → $RUNNER_TEMP/hubspot_contact.json
  → compute_campaign_tokens.py → $RUNNER_TEMP/campaign_tokens.json
  → generate_campaign.py → $RUNNER_TEMP/campaign_output.json
  → write_hubspot.py   → HubSpot contact properties + note
```

## Step Values

Valid `step` inputs: `1`, `3`, `7`, `11`, `14`

Each step corresponds to a day in the speed-to-lead sequence. Step 1 fires within minutes of form submission. Steps 3–14 fire after missed call attempts.

## HubSpot Properties Written

For each run with step `n`:
- `inbound_s{n}_subject` — generated email subject
- `inbound_s{n}_body` — generated email body (HTML stripped, ends before meeting link)
- `inbound_generated_date` — date of most recent generation

Create these properties in HubSpot (Settings → Properties → Contact) before first run.

## GitHub Secrets Required

- `HUBSPOT_API_KEY` — HubSpot private app token (contacts + companies read/write, engagements read/write, owners read)
- `ANTHROPIC_API_KEY` — Anthropic API key
- `TEAMS_WEBHOOK_URL` — Microsoft Teams incoming webhook (or Slack)

## Reference

- Mid-Market pipeline (same architecture): `C:\Users\irahfo\Outreach\Mid Market\Mid-Market\`
- Prompt spec: `prompt.md` (project root)
- GSD planning: `.planning/`

## GSD Workflow

Run `/gsd-plan-phase N` to plan a phase, then `/gsd-execute-phase N` to build it.
Current phase: **1 — Scaffolding** (not started)
