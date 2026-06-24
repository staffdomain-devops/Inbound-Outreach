import json
import os
import re
import sys
from pathlib import Path

import anthropic
from tenacity import retry, retry_if_exception

from utils import write_dlq, _is_anthropic_transient, ANTHROPIC_RETRY_KWARGS


PROMPT_PATH = Path(__file__).parent.parent / "prompt_template.md"
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192
EMAIL_BODY_CAP = 3000

_anthropic_retry = retry(retry=retry_if_exception(_is_anthropic_transient), **ANTHROPIC_RETRY_KWARGS)

JSON_SCHEMA_HINT = """
Return ONLY a raw JSON object — no markdown, no code fences, no explanation. Shape:
{
  "email_1": {"subject": "...", "body": "..."},
  "email_2": {"subject": "...", "body": "..."},
  "email_3": {"subject": "...", "body": "..."},
  "email_4": {"subject": "...", "body": "..."}
}
"""


@_anthropic_retry
def _call_claude(client, system, messages):
    return client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=messages,
    )


def build_activity_history(email_history, meeting_engagements, call_history,
                            contact_notes, company_notes):
    sections = []

    if email_history:
        lines = ["[EMAIL THREAD]"]
        for e in email_history:
            lines.append(
                f"--- {e.get('timestamp', '')} | {e.get('direction', '')} "
                f"| Subject: {e.get('subject', '(no subject)')} ---"
            )
            body = (e.get("body_text") or "").strip()
            if body:
                lines.append(body[:EMAIL_BODY_CAP])
            lines.append("")
        sections.append("\n".join(lines))

    if meeting_engagements:
        lines = ["[MEETING NOTES]"]
        for m in meeting_engagements:
            duration = m.get("duration_minutes")
            lines.append(
                f"--- Meeting: {m.get('meeting_date', '')}"
                f"{f' | {duration}min' if duration else ''} ---"
            )
            if m.get("notes"):
                lines.append(f"Notes: {m['notes'].strip()}")
            if m.get("internal_notes"):
                lines.append(f"Internal notes: {m['internal_notes'].strip()}")
            lines.append("")
        sections.append("\n".join(lines))

    if call_history:
        lines = ["[CALL LOG]"]
        for c in call_history:
            duration = c.get("duration_minutes")
            lines.append(
                f"--- {c.get('timestamp', '')} | {c.get('disposition', '')}"
                f"{f' | {duration}min' if duration else ''} ---"
            )
            notes = (c.get("notes") or "").strip()
            if notes:
                lines.append(notes[:EMAIL_BODY_CAP])
            lines.append("")
        sections.append("\n".join(lines))

    if contact_notes:
        lines = ["[CONTACT NOTES]"]
        for n in contact_notes:
            lines.append(f"--- {n.get('timestamp', '')} ---")
            lines.append((n.get("body") or "").strip()[:EMAIL_BODY_CAP])
            lines.append("")
        sections.append("\n".join(lines))

    if company_notes:
        lines = ["[COMPANY NOTES]"]
        for n in company_notes:
            lines.append(f"--- {n.get('timestamp', '')} ---")
            lines.append((n.get("body") or "").strip()[:EMAIL_BODY_CAP])
            lines.append("")
        sections.append("\n".join(lines))

    return "\n\n".join(sections) if sections else "(no activity history on file)"


def substitute_tokens(template, tokens):
    def replacer(match):
        key = match.group(1)
        return str(tokens.get(key, ""))
    return re.sub(r"\{\{([^}]+)\}\}", replacer, template)


def clean_text(text):
    if not isinstance(text, str):
        return text
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"  +", " ", text)
    return text.strip()


def strip_code_fence(text):
    """Remove ```json / ``` wrappers Claude sometimes adds despite instructions."""
    text = text.strip()
    match = re.match(r'^```(?:json)?\s*([\s\S]*?)```\s*$', text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Fallback: extract the first {...} block if the response has surrounding prose.
    match = re.search(r'(\{[\s\S]*\})', text)
    if match:
        return match.group(1).strip()
    return text


def clean_emails(output):
    for i in range(1, 5):
        key = f"email_{i}"
        if key in output and isinstance(output[key], dict):
            email = output[key]
            if "subject" in email:
                email["subject"] = clean_text(email["subject"])
            if "body" in email:
                email["body"] = clean_text(email["body"])
    return output


def main():
    runner_temp = os.environ["RUNNER_TEMP"]
    contact_id = os.environ["INPUT_CONTACT_ID"]
    contact_email = os.environ.get("INPUT_CONTACT_EMAIL", "unknown")

    write_dlq(contact_id, contact_email, "generate_campaign", "Script started", retry_count=0)

    with open(os.path.join(runner_temp, "hubspot_contact.json")) as f:
        hubspot_data = json.load(f)
    with open(os.path.join(runner_temp, "campaign_tokens.json")) as f:
        campaign_tokens = json.load(f)

    contact_props = hubspot_data.get("contact_properties") or {}
    company_props = hubspot_data.get("company_properties") or {}
    email_history = hubspot_data.get("email_history") or []
    meeting_engagements = hubspot_data.get("meeting_engagements") or []
    call_history = hubspot_data.get("call_history") or []
    contact_notes = hubspot_data.get("contact_notes") or []
    company_notes = hubspot_data.get("company_notes") or []

    activity_history = build_activity_history(
        email_history, meeting_engagements, call_history,
        contact_notes, company_notes,
    )

    tokens = {
        # Form submission fields (strongest signal)
        "contact.form_role": contact_props.get("what_role_s_are_you_looking_to_scale_right_now_", ""),
        "contact.form_staff_count": contact_props.get("how_many_staff_are_you_looking_to_hire", ""),
        "contact.form_why_offshore": contact_props.get("why_are_you_looking_to_offshore_", ""),
        "contact.form_anything_else": contact_props.get("anything_else_we_should_know_", ""),
        # Contact properties
        "contact.first_name": contact_props.get("firstname", ""),
        "contact.last_name": contact_props.get("lastname", ""),
        "contact.jobtitle": contact_props.get("jobtitle", ""),
        "contact.company": contact_props.get("company", ""),
        "contact.industry": contact_props.get("industry", ""),
        "contact.numberofemployees": contact_props.get("num_employees", ""),
        "contact.city": contact_props.get("city", ""),
        "contact.state": contact_props.get("state", ""),
        "contact.country": contact_props.get("country", ""),
        "contact.website": contact_props.get("website", ""),
        "contact.current_owner_firstname": contact_props.get("current_owner_firstname", ""),
        # Company properties
        "company.name": company_props.get("name", ""),
        "company.industry": company_props.get("industry", ""),
        "company.numberofemployees": company_props.get("numberofemployees", ""),
        "company.city": company_props.get("city", ""),
        "company.country": company_props.get("country", ""),
        "company.website": company_props.get("website", ""),
        "company.annualrevenue": company_props.get("annualrevenue", ""),
        "company.description": company_props.get("description", ""),
        # Activity + date
        "crm.full_activity_history": activity_history,
        "campaign.current_date": campaign_tokens.get("current_date", ""),
    }

    template = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = substitute_tokens(template, tokens)

    print(f"Prompt assembled: {len(prompt):,} chars")
    print(
        f"  Contact: {tokens['contact.first_name']} {tokens['contact.last_name']}"
        f" @ {tokens['contact.company'] or tokens['company.name']}"
    )
    print(f"  Job title:       {tokens['contact.jobtitle'] or '(not provided)'}")
    print(f"  Company:         {tokens['company.name'] or '(not provided)'}")
    print(f"  Industry:        {tokens['company.industry'] or tokens['contact.industry'] or '(not provided)'}")
    print(f"  Employees:       {tokens['company.numberofemployees'] or '(not provided)'}")
    print(f"  --- Form submission ---")
    print(f"  Role(s):         {tokens['contact.form_role'] or '(not provided)'}")
    print(f"  Staff count:     {tokens['contact.form_staff_count'] or '(not provided)'}")
    print(f"  Why offshore:    {tokens['contact.form_why_offshore'] or '(not provided)'}")
    print(f"  Anything else:   {tokens['contact.form_anything_else'] or '(not provided)'}")
    print(
        f"  Activity: {len(email_history)} emails, {len(meeting_engagements)} meetings, "
        f"{len(call_history)} calls, {len(contact_notes)} contact notes, "
        f"{len(company_notes)} company notes"
    )
    print(f"Calling {MODEL}...")

    system = (
        "You are writing sales emails in strict Australian English. "
        "NEVER use em dashes (—), en dashes (–), or hyphens as sentence separators. "
        "Use commas, colons, or full stops instead. Compound adjectives (no-lock-in, all-in) are fine. "
        "Never use the words 'offshoring' or 'outsourcing'. "
        "Return only the raw JSON object — no markdown, no code blocks."
    )

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=0)
        message = _call_claude(
            client, system,
            [{"role": "user", "content": prompt + "\n\n" + JSON_SCHEMA_HINT}],
        )

        raw = message.content[0].text
        print(f"Response: {len(raw):,} chars | stop_reason={message.stop_reason}")

        # Always save the raw response so it's available as an artifact.
        raw_path = os.path.join(runner_temp, "campaign_output_raw.txt")
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(raw)

        if message.stop_reason == "max_tokens":
            raise RuntimeError("Claude response truncated (max_tokens) — increase MAX_TOKENS or shorten prompt")

        cleaned = strip_code_fence(raw)
        if cleaned != raw:
            print(f"Stripped code fence — cleaned response: {len(cleaned):,} chars")

        try:
            output = json.loads(cleaned)
        except json.JSONDecodeError as e:
            print(f"Raw response saved to {raw_path}", file=sys.stderr)
            raise RuntimeError(f"Claude response is not valid JSON: {e}") from e

        required_keys = ["email_1", "email_2", "email_3", "email_4"]
        missing = [k for k in required_keys if k not in output]
        if missing:
            print(f"WARNING: response missing keys: {missing}", file=sys.stderr)

        output = clean_emails(output)

        out_path = os.path.join(runner_temp, "campaign_output.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        print(f"campaign_output.json written: {len([k for k in output if k.startswith('email_')])} emails")

    except Exception as exc:
        retry_count = getattr(getattr(exc, "__cause__", None), "statistics", {}).get("attempt_number", 1)
        write_dlq(contact_id, contact_email, "generate_campaign", exc, retry_count)
        raise


if __name__ == "__main__":
    main()
