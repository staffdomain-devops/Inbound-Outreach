import json
import os
import sys
import traceback
from datetime import datetime, timezone

import hubspot
from hubspot.crm.contacts import ApiException
from bs4 import BeautifulSoup
import requests as req_lib
from tenacity import retry, retry_if_exception

from utils import (
    write_dlq,
    _is_hubspot_transient,
    _is_requests_transient,
    HS_RETRY_KWARGS,
    REQ_RETRY_KWARGS,
)


_hs_retry = retry(retry=retry_if_exception(_is_hubspot_transient), **HS_RETRY_KWARGS)
_req_retry = retry(retry=retry_if_exception(_is_requests_transient), **REQ_RETRY_KWARGS)


@_hs_retry
def _get_contact(client, contact_id, properties):
    return client.crm.contacts.basic_api.get_by_id(contact_id, properties=properties)


@_hs_retry
def _get_company(client, company_id, properties):
    return client.crm.companies.basic_api.get_by_id(company_id, properties=properties)


@_hs_retry
def _get_associations(client, contact_id, from_obj, to_obj):
    return client.crm.associations.v4.basic_api.get_all(from_obj, contact_id, to_obj)


@_hs_retry
def _get_owner(client, owner_id):
    return client.crm.owners.owners_api.get_by_id(owner_id=int(owner_id), id_property="id")


def _resolve_enum_label(client, object_type, property_name, value):
    """
    HubSpot dropdown/radio properties store an internal key (e.g. 'np95Md0Jqz7A2Q7J0PhGF')
    rather than the human-readable label. Fetch the property definition and map the key
    to its label. Returns the original value unchanged if resolution fails or is unnecessary.
    """
    if not value or not isinstance(value, str) or " " in value.strip():
        return value  # already looks like human text
    try:
        prop = client.crm.properties.core_api.get_by_name(object_type, property_name)
        for option in (prop.options or []):
            if option.value == value:
                print(f"  Resolved {property_name}: {value!r} → {option.label!r}")
                return option.label
    except Exception as exc:
        print(f"  Warning: could not resolve {property_name} enum: {exc}", file=sys.stderr)
    return value


@_req_retry
def _get_engagements_page(headers, object_type, object_id, params):
    url = f"https://api.hubapi.com/engagements/v1/engagements/associated/{object_type}/{object_id}/paged"
    resp = req_lib.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp


def strip_html(html_text):
    if not html_text:
        return ""
    try:
        return BeautifulSoup(html_text, "html.parser").get_text(separator=" ", strip=True)
    except Exception:
        import re
        return re.sub(r"<[^>]+>", " ", html_text).strip()


def fetch_contact_engagements(contact_id, headers):
    """Fetch emails, meetings, calls, and notes from the contact record."""
    email_history = []
    meeting_engagements = []
    call_history = []
    contact_notes = []
    offset = 0
    has_more = True

    while has_more:
        resp = _get_engagements_page(headers, "CONTACT", contact_id, {"limit": 100, "offset": offset})
        data = resp.json()

        for item in data.get("results", []):
            eng = item.get("engagement", {})
            meta = item.get("metadata", {})
            eng_type = eng.get("type")
            created_at = datetime.fromtimestamp(eng.get("createdAt", 0) / 1000, tz=timezone.utc)

            if eng_type in ("EMAIL", "INCOMING_EMAIL"):
                body_html = meta.get("html") or meta.get("body") or meta.get("text") or ""
                email_history.append({
                    "subject": meta.get("subject", ""),
                    "body_text": strip_html(body_html),
                    "direction": meta.get("direction", ""),
                    "timestamp": created_at.isoformat(),
                })

            elif eng_type == "MEETING":
                notes_raw = meta.get("body") or meta.get("description") or ""
                attendees = meta.get("attendees") or []
                start_time = meta.get("startTime")
                meeting_date = (
                    datetime.fromtimestamp(start_time / 1000, tz=timezone.utc).isoformat()
                    if start_time else created_at.isoformat()
                )
                duration_ms = meta.get("durationMilliseconds") or 0
                meeting_engagements.append({
                    "meeting_date": meeting_date,
                    "attendees_ours": [a for a in attendees if isinstance(a, str) and "staffdomain.com.au" in a],
                    "attendees_theirs": [a for a in attendees if isinstance(a, str) and "staffdomain.com.au" not in a],
                    "notes": strip_html(notes_raw),
                    "internal_notes": strip_html(meta.get("internalMeetingNotes") or ""),
                    "duration_minutes": round(duration_ms / 60000) if duration_ms else None,
                })

            elif eng_type in ("CALL", "INCOMING_CALL"):
                notes_raw = meta.get("body") or meta.get("text") or ""
                duration_ms = meta.get("durationMilliseconds") or 0
                call_history.append({
                    "timestamp": created_at.isoformat(),
                    "notes": strip_html(notes_raw),
                    "disposition": meta.get("disposition", ""),
                    "duration_minutes": round(duration_ms / 60000) if duration_ms else None,
                })

            elif eng_type == "NOTE":
                body_raw = meta.get("body") or ""
                if body_raw.strip():
                    contact_notes.append({
                        "timestamp": created_at.isoformat(),
                        "body": strip_html(body_raw),
                    })

        has_more = data.get("hasMore", False)
        offset = data.get("offset", offset + 100)

    return email_history, meeting_engagements, call_history, contact_notes


def fetch_company_notes(company_id, headers):
    """Fetch notes from the company (account) record — enrichment agent output lives here."""
    notes = []
    offset = 0
    has_more = True

    while has_more:
        try:
            resp = _get_engagements_page(headers, "COMPANY", company_id, {"limit": 100, "offset": offset})
        except Exception as e:
            print(f"  Warning: could not fetch company engagements: {e}", file=sys.stderr)
            break
        data = resp.json()

        for item in data.get("results", []):
            eng = item.get("engagement", {})
            meta = item.get("metadata", {})
            if eng.get("type") == "NOTE":
                body_raw = meta.get("body") or ""
                if body_raw.strip():
                    created_at = datetime.fromtimestamp(eng.get("createdAt", 0) / 1000, tz=timezone.utc)
                    notes.append({
                        "timestamp": created_at.isoformat(),
                        "body": strip_html(body_raw),
                    })

        has_more = data.get("hasMore", False)
        offset = data.get("offset", offset + 100)

    return notes


def main():
    contact_id = os.environ["INPUT_CONTACT_ID"]
    contact_email = os.environ["INPUT_CONTACT_EMAIL"]
    runner_temp = os.environ["RUNNER_TEMP"]

    write_dlq(contact_id, contact_email, "fetch_hubspot", "Script started", retry_count=0)

    client = hubspot.Client.create(access_token=os.environ["HUBSPOT_API_KEY"])
    headers = {"Authorization": f"Bearer {os.environ['HUBSPOT_API_KEY']}"}

    try:
        contact = _get_contact(
            client,
            contact_id,
            properties=[
                "firstname", "lastname", "email", "jobtitle", "company",
                "industry", "num_employees", "city", "state", "country",
                "website", "hubspot_owner_id",
                # Form submission fields
                "what_role_s_are_you_looking_to_scale_right_now_",
                "how_many_staff_are_you_looking_to_hire",
                "why_are_you_looking_to_offshore_",
                "anything_else_we_should_know_",
            ],
        )
        contact_properties = contact.properties or {}

        # Dropdown/radio fields store an internal key — resolve to the human-readable label.
        for _enum_field in ["why_are_you_looking_to_offshore_"]:
            if contact_properties.get(_enum_field):
                contact_properties[_enum_field] = _resolve_enum_label(
                    client, "contacts", _enum_field, contact_properties[_enum_field]
                )

        def resolve_owner_firstname(owner_id):
            if not owner_id:
                return ""
            try:
                owner = _get_owner(client, owner_id)
                return owner.first_name or ""
            except Exception as e:
                print(f"  Warning: could not resolve owner {owner_id}: {e}", file=sys.stderr)
                return ""

        contact_properties["current_owner_firstname"] = resolve_owner_firstname(
            contact_properties.get("hubspot_owner_id")
        )

        # Fetch associated company — use REST v3 directly (SDK v4 get_all unreliable)
        company_properties = {}
        company_id = None
        try:
            assoc_resp = req_lib.get(
                f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}/associations/companies",
                headers=headers,
                timeout=30,
            )
            assoc_resp.raise_for_status()
            company_ids = [r["id"] for r in assoc_resp.json().get("results", [])]
            if company_ids:
                company_id = company_ids[0]
                company = _get_company(
                    client,
                    company_id,
                    properties=[
                        "name", "industry", "numberofemployees", "city", "country",
                        "website", "annualrevenue", "description",
                    ],
                )
                company_properties = company.properties or {}
                print(f"Company: {company_properties.get('name', '(unnamed)')} (ID {company_id})")
            else:
                print("No associated company found", file=sys.stderr)
        except Exception as e:
            print(f"  Warning: could not fetch company: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

        # Fetch contact engagements
        try:
            email_history, meeting_engagements, call_history, contact_notes = fetch_contact_engagements(
                contact_id, headers
            )
        except Exception as e:
            print(f"Warning: could not fetch contact engagements: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            email_history, meeting_engagements, call_history, contact_notes = [], [], [], []

        # Fetch company notes
        company_notes = []
        if company_id:
            try:
                company_notes = fetch_company_notes(company_id, headers)
            except Exception as e:
                print(f"Warning: could not fetch company notes: {e}", file=sys.stderr)

        print(
            f"Contact engagements: {len(email_history)} emails, {len(meeting_engagements)} meetings, "
            f"{len(call_history)} calls, {len(contact_notes)} notes"
        )
        print(f"Company notes: {len(company_notes)}")

        output = {
            "contact_properties": contact_properties,
            "company_id": company_id,
            "company_properties": company_properties,
            "email_history": email_history,
            "meeting_engagements": meeting_engagements,
            "call_history": call_history,
            "contact_notes": contact_notes,
            "company_notes": company_notes,
        }

        out_path = os.path.join(runner_temp, "hubspot_contact.json")
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2, default=str)

        print(f"hubspot_contact.json written")

    except Exception as exc:
        retry_count = getattr(getattr(exc, "__cause__", None), "statistics", {}).get("attempt_number", 1)
        write_dlq(contact_id, contact_email, "fetch_hubspot", exc, retry_count)
        raise


if __name__ == "__main__":
    main()
