"""
Enrichment gate — runs after fetch_hubspot, before compute_campaign_tokens.

1. Checks which important contact/company fields are present.
2. If fields are missing, calls ZoomInfo contact enrich (single call).
   Company data is extracted from the contact result — no separate company call.
3. Updates HubSpot contact and/or company with any newly found data.
4. If important gaps remain after enrichment, posts an SDR review note.
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests as req_lib

from utils import write_dlq


ZOOMINFO_BASE = "https://api.zoominfo.com"

PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "me.com", "live.com", "aol.com", "protonmail.com",
    "msn.com", "ymail.com",
}

IMPORTANT_CONTACT_FIELDS = [
    ("firstname", "Contact first name"),
    ("jobtitle",  "Contact job title / seniority"),
]
IMPORTANT_COMPANY_FIELDS = [
    ("name",              "Company name"),
    ("industry",          "Company industry"),
    ("numberofemployees", "Number of employees"),
]
IMPORTANT_FORM_FIELDS = [
    ("what_role_s_are_you_looking_to_scale_right_now_", "Role(s) enquired about"),
]

_ZI_OUTPUT_FIELDS = [
    "id", "firstName", "middleName", "lastName", "email",
    "hasCanadianEmail", "phone", "directPhoneDoNotCall",
    "street", "city", "region", "metroArea", "zipCode", "state", "country",
    "personHasMoved", "withinEu", "withinCalifornia", "withinCanada",
    "lastUpdatedDate", "noticeProvidedDate",
    "salutation", "suffix", "jobTitle", "jobFunction", "companyDivision",
    "education", "hashedEmails", "picture", "mobilePhoneDoNotCall", "externalUrls",
    "companyId", "companyName", "companyDescriptionList", "companyPhone", "companyFax",
    "companyStreet", "companyCity", "companyState", "companyZipCode", "companyCountry",
    "companyLogo", "companySicCodes", "companyNaicsCodes", "contactAccuracyScore",
    "companyWebsite", "companyRevenue", "companyRevenueNumeric", "companyEmployeeCount",
    "companyType", "companyTicker", "companyRanking", "isDefunct",
    "companySocialMediaUrls", "companyPrimaryIndustry", "companyIndustries",
    "companyRevenueRange", "companyEmployeeRange",
    "employmentHistory", "managementLevel", "locationCompanyId",
]


# ── ZoomInfo ──────────────────────────────────────────────────────────────────

def _get_zoominfo_jwt():
    username = os.environ.get("ZOOMINFO_USERNAME", "")
    password = os.environ.get("ZOOMINFO_PASSWORD", "")
    if not username or not password:
        return None
    try:
        resp = req_lib.post(
            f"{ZOOMINFO_BASE}/authenticate",
            json={"username": username, "password": password},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("jwt")
    except Exception as exc:
        print(f"  ZoomInfo auth failed: {exc}", file=sys.stderr)
        return None


def _zi_enrich_contact(jwt, *, email=None, first_name=None, last_name=None, company_name=None):
    """
    Single ZoomInfo /enrich/contact call.
    Returns (contact_data, company_data) — both dicts, may be empty.
    Company fields come from result[0]["data"][0]["company"]; no separate company call.
    """
    if not jwt:
        return {}, {}

    attempts = []
    if email:
        primary = {"personEmailAddress": email}
        if first_name:
            primary["firstName"] = first_name
        if last_name:
            primary["lastName"] = last_name
        attempts.append(primary)

    if first_name and last_name:
        fallback = {"firstName": first_name, "lastName": last_name}
        if company_name:
            fallback["companyName"] = company_name
        if fallback not in attempts:
            attempts.append(fallback)

    for match_input in attempts:
        try:
            resp = req_lib.post(
                f"{ZOOMINFO_BASE}/enrich/contact",
                headers={"Authorization": f"Bearer {jwt}"},
                json={"matchPersonInput": [match_input], "outputFields": _ZI_OUTPUT_FIELDS},
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()

            result_list = (payload.get("data") or {}).get("result") or []
            if not result_list:
                print(f"  ZI: no result list (input={list(match_input.keys())})")
                continue

            first_result = result_list[0]
            match_status = first_result.get("matchStatus", "")
            data_list = first_result.get("data") or []

            if not data_list:
                print(f"  ZI: matchStatus={match_status}, no data (input={list(match_input.keys())})")
                continue

            contact_data = data_list[0]

            # Company data may be nested under "company" or returned as flat companyXxx fields.
            company_data = contact_data.get("company") or {}
            if not company_data.get("name") and contact_data.get("companyName"):
                company_data = {
                    "name":            contact_data.get("companyName", ""),
                    "website":         contact_data.get("companyWebsite", ""),
                    "employeeCount":   contact_data.get("companyEmployeeCount"),
                    "employeeRange":   contact_data.get("companyEmployeeRange", ""),
                    "primaryIndustry": contact_data.get("companyPrimaryIndustry") or contact_data.get("companyIndustries") or [],
                    "city":            contact_data.get("companyCity", ""),
                    "state":           contact_data.get("companyState", ""),
                    "country":         contact_data.get("companyCountry", ""),
                    "revenue":         contact_data.get("companyRevenue", ""),
                }

            print(
                f"  ZI matched via {list(match_input.keys())}: "
                f"{contact_data.get('firstName', '')} {contact_data.get('lastName', '')} "
                f"| {contact_data.get('jobTitle', 'no title')} | status={match_status}"
            )
            if company_data.get("name"):
                print(
                    f"  ZI company: {company_data['name']} "
                    f"| employees={company_data.get('employeeCount', company_data.get('employeeRange', 'N/A'))}"
                )

            return contact_data, company_data

        except Exception as exc:
            print(f"  ZI contact enrich failed ({list(match_input.keys())}): {exc}", file=sys.stderr)

    return {}, {}


def _extract_zi_company_props(zi_company):
    """Map ZoomInfo company dict (nested in contact result) to HubSpot-ready field dict."""
    primary_industry = zi_company.get("primaryIndustry") or []
    if isinstance(primary_industry, list):
        primary_industry = primary_industry[0] if primary_industry else ""

    employees = ""
    if zi_company.get("employeeCount"):
        employees = str(zi_company["employeeCount"])
    elif zi_company.get("employeeRange"):
        employees = str(zi_company["employeeRange"])

    return {
        "name":              zi_company.get("name", ""),
        "industry":          primary_industry,
        "numberofemployees": employees,
        "website":           zi_company.get("website", ""),
        "city":              zi_company.get("city", ""),
        "state":             zi_company.get("state", ""),
        "country":           zi_company.get("country", ""),
    }


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def _clean_domain(url):
    if not url:
        return ""
    d = url.lower().replace("https://", "").replace("http://", "").lstrip("www.")
    return d.split("/")[0].strip()


def _hs_search_company_by_domain(headers, domain):
    try:
        resp = req_lib.post(
            "https://api.hubapi.com/crm/v3/objects/companies/search",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "filterGroups": [{
                    "filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}],
                }],
                "properties": ["name", "domain", "industry", "numberofemployees", "city", "country"],
                "limit": 1,
            },
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None
    except Exception as exc:
        print(f"  HubSpot company search failed: {exc}", file=sys.stderr)
        return None


def _hs_create_company(headers, zi_co_props):
    """Create a HubSpot company from extracted ZoomInfo props. Returns new company ID or None."""
    zi_domain = _clean_domain(zi_co_props.get("website", ""))
    hs_props = {
        k: v for k, v in {
            "name":              zi_co_props.get("name", ""),
            "domain":            zi_domain,
            "industry":          zi_co_props.get("industry", ""),
            "numberofemployees": zi_co_props.get("numberofemployees", ""),
            "city":              zi_co_props.get("city", ""),
            "country":           zi_co_props.get("country", ""),
        }.items() if v
    }
    try:
        resp = req_lib.post(
            "https://api.hubapi.com/crm/v3/objects/companies",
            headers={**headers, "Content-Type": "application/json"},
            json={"properties": hs_props},
            timeout=30,
        )
        resp.raise_for_status()
        cid = resp.json().get("id")
        print(f"  HubSpot company created: ID {cid} ({hs_props.get('name')})")
        return cid
    except Exception as exc:
        print(f"  HubSpot company creation failed: {exc}", file=sys.stderr)
        return None


def _hs_patch_company(headers, company_id, properties):
    if not properties or not company_id:
        return
    try:
        resp = req_lib.patch(
            f"https://api.hubapi.com/crm/v3/objects/companies/{company_id}",
            headers={**headers, "Content-Type": "application/json"},
            json={"properties": properties},
            timeout=30,
        )
        if not resp.ok:
            print(f"  HubSpot company patch failed {resp.status_code}: {resp.text[:400]}", file=sys.stderr)
            resp.raise_for_status()
        print(f"  HubSpot company {company_id} patched: {list(properties.keys())}")
    except Exception as exc:
        print(f"  HubSpot company patch failed: {exc}", file=sys.stderr)


def _hs_patch_contact(headers, contact_id, properties):
    if not properties:
        return
    try:
        resp = req_lib.patch(
            f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}",
            headers={**headers, "Content-Type": "application/json"},
            json={"properties": properties},
            timeout=30,
        )
        resp.raise_for_status()
        print(f"  HubSpot contact {contact_id} patched: {list(properties.keys())}")
    except Exception as exc:
        print(f"  HubSpot contact patch failed: {exc}", file=sys.stderr)


def _hs_associate_contact_company(headers, contact_id, company_id):
    try:
        resp = req_lib.post(
            "https://api.hubapi.com/crm/v4/associations/0-1/0-2/batch/create",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "inputs": [{
                    "from": {"id": str(contact_id)},
                    "to":   {"id": str(company_id)},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 1}],
                }]
            },
            timeout=30,
        )
        resp.raise_for_status()
        print(f"  Associated contact {contact_id} → company {company_id}")
    except Exception as exc:
        print(f"  HubSpot association failed: {exc}", file=sys.stderr)


def _hs_post_sdr_note(headers, contact_id, gaps, enrichment_log):
    try:
        ts = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
        gap_html = "".join(f"<li>{g}</li>" for g in gaps)
        log_html = "".join(f"<li>{e}</li>" for e in enrichment_log) if enrichment_log else "<li>(none attempted)</li>"
        body = (
            f"<h3>SDR Review Required — Enrichment Incomplete</h3>"
            f"<div>Checked {ts}</div><br>"
            f"<h4>Missing Fields</h4><ul>{gap_html}</ul>"
            f"<h4>Enrichment Log</h4><ul>{log_html}</ul>"
            f"<p>Emails have been generated with the available data. "
            f"Please fill in the missing fields and regenerate if needed.</p>"
        )
        payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
            },
            "associations": [{
                "to":    {"id": str(contact_id)},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            }],
        }
        resp = req_lib.post(
            "https://api.hubapi.com/crm/v3/objects/notes",
            headers={**headers, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        print(f"  SDR review note posted (ID {resp.json().get('id')})")
    except Exception as exc:
        print(f"  Could not post SDR note: {exc}", file=sys.stderr)


# ── Gap check ─────────────────────────────────────────────────────────────────

def _check_gaps(contact_props, company_props):
    gaps = []
    for field, label in IMPORTANT_CONTACT_FIELDS:
        if not (contact_props.get(field) or "").strip():
            gaps.append(f"{label}  [contact.{field}]")
    for field, label in IMPORTANT_COMPANY_FIELDS:
        if not (company_props.get(field) or "").strip():
            gaps.append(f"{label}  [company.{field}]")
    for field, label in IMPORTANT_FORM_FIELDS:
        if not (contact_props.get(field) or "").strip():
            gaps.append(f"{label}  [form.{field}]")
    return gaps


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    contact_id    = os.environ["INPUT_CONTACT_ID"]
    contact_email = os.environ.get("INPUT_CONTACT_EMAIL", "")
    runner_temp   = os.environ["RUNNER_TEMP"]

    write_dlq(contact_id, contact_email, "enrich_contact", "Script started", retry_count=0)

    hs_path = os.path.join(runner_temp, "hubspot_contact.json")
    with open(hs_path) as f:
        data = json.load(f)

    contact_props = data.get("contact_properties") or {}
    company_props = data.get("company_properties") or {}
    company_id    = data.get("company_id")

    headers = {"Authorization": f"Bearer {os.environ['HUBSPOT_API_KEY']}"}

    try:
        domain = contact_email.split("@", 1)[1].lower().strip()
    except (IndexError, AttributeError):
        domain = ""
    lookup_domain = domain if domain and domain not in PERSONAL_DOMAINS else ""

    enrichment_log = []

    # ── Initial gap check ─────────────────────────────────────────────────────
    initial_gaps = _check_gaps(contact_props, company_props)
    print(f"Gap check — {len(initial_gaps)} missing field(s):")
    for g in initial_gaps:
        print(f"  - {g}")

    if not initial_gaps:
        print("All important fields present — skipping enrichment")
        data["enrichment_log"]  = []
        data["enrichment_gaps"] = []
        with open(hs_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        return

    # ── ZoomInfo contact enrich (company embedded in result) ──────────────────
    jwt = _get_zoominfo_jwt()
    zi_contact, zi_company = {}, {}

    if jwt:
        fn = (contact_props.get("firstname") or "").strip()
        ln = (contact_props.get("lastname") or "").strip()
        co = (company_props.get("name") or contact_props.get("company") or "").strip()

        print(f"ZoomInfo enrich — email={contact_email!r} name={fn!r} {ln!r} company={co!r}")
        zi_contact, zi_company = _zi_enrich_contact(
            jwt,
            email=contact_email or None,
            first_name=fn or None,
            last_name=ln or None,
            company_name=co or None,
        )

        if zi_contact:
            enrichment_log.append(
                f"ZoomInfo contact matched: {zi_contact.get('firstName', '')} "
                f"{zi_contact.get('lastName', '')} | {zi_contact.get('jobTitle', 'no title')}"
            )
        else:
            enrichment_log.append("ZoomInfo contact: no match (tried email + name fallback)")
            print("  ZI: no contact result", file=sys.stderr)

        if zi_company.get("name"):
            enrichment_log.append(
                f"ZoomInfo company (from contact result): {zi_company['name']} "
                f"| employees={zi_company.get('employeeCount', zi_company.get('employeeRange', 'N/A'))}"
            )
    else:
        enrichment_log.append("ZoomInfo unavailable — credentials not configured")

    # ── Apply contact fields ──────────────────────────────────────────────────
    if zi_contact:
        hs_ct_patch = {}
        for prop_name, zi_key in [
            ("firstname", "firstName"),
            ("lastname",  "lastName"),
            ("jobtitle",  "jobTitle"),
        ]:
            if not (contact_props.get(prop_name) or "").strip() and zi_contact.get(zi_key):
                contact_props[prop_name] = zi_contact[zi_key]
                hs_ct_patch[prop_name]   = zi_contact[zi_key]

        if hs_ct_patch:
            _hs_patch_contact(headers, contact_id, hs_ct_patch)

    # ── Apply company fields ──────────────────────────────────────────────────
    if zi_company:
        zi_co_props = _extract_zi_company_props(zi_company)
        zi_domain   = _clean_domain(zi_co_props.get("website", "")) or lookup_domain

        if not company_id and zi_co_props.get("name"):
            existing = _hs_search_company_by_domain(headers, zi_domain) if zi_domain else None
            if existing:
                company_id = existing["id"]
                enrichment_log.append(f"Found existing HubSpot company ID {company_id}")
                print(f"  Found existing HubSpot company ID {company_id}")
            else:
                company_id = _hs_create_company(headers, zi_co_props)
                if company_id:
                    enrichment_log.append(f"Created HubSpot company ID {company_id}")

            if company_id:
                _hs_associate_contact_company(headers, contact_id, company_id)

        if company_id:
            hs_co_patch = {}
            # "industry" is a HubSpot enum — patching with a free-text ZI value causes 400.
            # Keep industry in-memory for Claude but don't write it back to HubSpot.
            for field in ["name", "numberofemployees", "city", "country"]:
                if not (company_props.get(field) or "").strip() and zi_co_props.get(field):
                    hs_co_patch[field] = zi_co_props[field]
            if hs_co_patch:
                _hs_patch_company(headers, company_id, hs_co_patch)

        for field, value in zi_co_props.items():
            if value and not (company_props.get(field) or "").strip():
                company_props[field] = value

    # ── Final gap check + SDR note ────────────────────────────────────────────
    final_gaps = _check_gaps(contact_props, company_props)

    if final_gaps:
        print(f"Remaining gaps after enrichment ({len(final_gaps)}):")
        for g in final_gaps:
            print(f"  - {g}")
        _hs_post_sdr_note(headers, contact_id, final_gaps, enrichment_log)
    else:
        print("All important fields resolved — no SDR note needed")

    # ── Write enriched hubspot_contact.json ───────────────────────────────────
    data["contact_properties"] = contact_props
    data["company_properties"] = company_props
    data["company_id"]         = company_id
    data["enrichment_log"]     = enrichment_log
    data["enrichment_gaps"]    = final_gaps

    with open(hs_path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    print("hubspot_contact.json updated — enrichment complete")


if __name__ == "__main__":
    main()
