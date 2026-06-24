"""
Enrichment gate — runs after fetch_hubspot, before compute_campaign_tokens.

1. Checks which important contact/company fields are present.
2. If no associated company (or company size missing), looks up the company
   via ZoomInfo using the contact's email domain or form-submitted name.
3. If a company is found, creates/associates it in HubSpot and fills
   company_properties from ZoomInfo data.
4. Enriches missing contact fields (job title, name) via ZoomInfo.
5. If important gaps remain after enrichment, posts an SDR review note to
   HubSpot and continues — never hard-fails the pipeline.
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests as req_lib
from tenacity import retry, retry_if_exception

from utils import write_dlq, _is_requests_transient, REQ_RETRY_KWARGS


ZOOMINFO_BASE = "https://api.zoominfo.com"

# Domains that belong to personal email providers — cannot identify a company.
PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "me.com", "live.com", "aol.com", "protonmail.com",
    "msn.com", "ymail.com",
}

# Fields whose absence is worth flagging for SDR review.
IMPORTANT_CONTACT_FIELDS = [
    ("firstname",  "Contact first name"),
    ("jobtitle",   "Contact job title / seniority"),
]
IMPORTANT_COMPANY_FIELDS = [
    ("name",              "Company name"),
    ("industry",          "Company industry"),
    ("numberofemployees", "Number of employees"),
]
IMPORTANT_FORM_FIELDS = [
    ("what_role_s_are_you_looking_to_scale_right_now_", "Role(s) enquired about"),
]


# ── ZoomInfo helpers ──────────────────────────────────────────────────────────

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


def _zi_enrich_company(jwt, *, domain=None, name=None):
    """Enrich a company via ZoomInfo. Returns the first result dict or {}."""
    if not jwt:
        return {}
    match_input = {}
    if domain:
        match_input["website"] = domain
    elif name:
        match_input["companyName"] = name
    if not match_input:
        return {}
    try:
        resp = req_lib.post(
            f"{ZOOMINFO_BASE}/enrich/company",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "matchCompanyInput": [match_input],
                "outputFields": [
                    "id", "name", "website", "revenue", "employees",
                    "primaryIndustry", "city", "state", "country",
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
        results = (resp.json().get("data") or {}).get("result") or []
        return results[0] if results else {}
    except Exception as exc:
        print(f"  ZoomInfo company enrich failed: {exc}", file=sys.stderr)
        return {}


def _zi_enrich_contact(jwt, email):
    """
    Enrich a contact via ZoomInfo by email.
    Uses matchPersonInput per ZoomInfo API v2 spec.
    Also returns company-level fields (companyEmployeeCount, companyPrimaryIndustry, etc.)
    so a separate company enrich call is often unnecessary.
    Returns the first result dict or {}.
    """
    if not jwt or not email:
        return {}
    try:
        resp = req_lib.post(
            f"{ZOOMINFO_BASE}/enrich/contact",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "matchPersonInput": [{"emailAddress": email}],
                "outputFields": [
                    "id", "firstName", "lastName", "jobTitle",
                    "companyName", "companyEmployeeCount",
                    "companyPrimaryIndustry", "companyWebsite",
                    "companyCity", "companyState", "companyCountry",
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
        results = (resp.json().get("data") or {}).get("result") or []
        return results[0] if results else {}
    except Exception as exc:
        print(f"  ZoomInfo contact enrich failed: {exc}", file=sys.stderr)
        return {}


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def _clean_domain(url):
    """Strip protocol/www from a URL to get a bare domain."""
    if not url:
        return ""
    d = url.lower().replace("https://", "").replace("http://", "").lstrip("www.")
    return d.split("/")[0].strip()


def _hs_search_company_by_domain(headers, domain):
    """Return the first HubSpot company matching the domain, or None."""
    try:
        resp = req_lib.post(
            "https://api.hubapi.com/crm/v3/objects/companies/search",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "filterGroups": [{
                    "filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}],
                }],
                "properties": [
                    "name", "domain", "industry",
                    "numberofemployees", "city", "country", "description",
                ],
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


def _hs_create_company(headers, zi_company):
    """Create a HubSpot company from ZoomInfo data. Returns the new company ID or None."""
    zi_domain = _clean_domain(zi_company.get("website", ""))
    props = {
        k: v for k, v in {
            "name":              zi_company.get("name", ""),
            "domain":            zi_domain,
            "industry":          zi_company.get("industry", ""),
            "numberofemployees": str(zi_company["employees"]) if zi_company.get("employees") else "",
            "annualrevenue":     str(zi_company["revenue"])   if zi_company.get("revenue")   else "",
            "city":              zi_company.get("city", ""),
            "country":           zi_company.get("country", ""),
            "description":       zi_company.get("description", ""),
        }.items() if v
    }
    try:
        resp = req_lib.post(
            "https://api.hubapi.com/crm/v3/objects/companies",
            headers={**headers, "Content-Type": "application/json"},
            json={"properties": props},
            timeout=30,
        )
        resp.raise_for_status()
        cid = resp.json().get("id")
        print(f"  HubSpot company created: ID {cid} ({props.get('name')})")
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
        gap_html  = "".join(f"<li>{g}</li>" for g in gaps)
        log_html  = "".join(f"<li>{e}</li>" for e in enrichment_log) if enrichment_log else "<li>(none attempted)</li>"
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
                "hs_note_body":  body,
                "hs_timestamp":  str(int(datetime.now(timezone.utc).timestamp() * 1000)),
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
    company_id    = data.get("company_id")          # str or None

    headers = {"Authorization": f"Bearer {os.environ['HUBSPOT_API_KEY']}"}

    # Email domain — used to find company when no association exists.
    try:
        domain = contact_email.split("@", 1)[1].lower().strip()
    except (IndexError, AttributeError):
        domain = ""
    lookup_domain = domain if domain and domain not in PERSONAL_DOMAINS else ""

    enrichment_log = []
    jwt = None  # fetched lazily

    # ── Initial gap report ────────────────────────────────────────────────────
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

    # ── Phase 1: Company ──────────────────────────────────────────────────────
    needs_company   = not (company_props.get("name") or "").strip()
    needs_employees = not (company_props.get("numberofemployees") or "").strip()

    if needs_company or needs_employees:
        company_name = (
            contact_props.get("company") or company_props.get("name") or ""
        ).strip()
        zi_lookup = lookup_domain or company_name

        if zi_lookup:
            jwt = jwt or _get_zoominfo_jwt()
            if jwt:
                print(f"ZoomInfo company lookup: domain={lookup_domain!r} name={company_name!r}")
                zi_co = _zi_enrich_company(jwt, domain=lookup_domain or None, name=company_name or None)

                if zi_co:
                    zi_name    = zi_co.get("name", "")
                    zi_emp     = zi_co.get("employees")
                    zi_raw_url = zi_co.get("website", "")
                    zi_domain  = _clean_domain(zi_raw_url) or lookup_domain

                    enrichment_log.append(
                        f"ZoomInfo company found: {zi_name} | employees={zi_emp or 'N/A'}"
                    )
                    print(f"  ZI company: {zi_name} | employees={zi_emp}")

                    if needs_company:
                        # Find or create HubSpot company, then associate.
                        existing = _hs_search_company_by_domain(headers, zi_domain) if zi_domain else None
                        if existing:
                            company_id = existing["id"]
                            enrichment_log.append(f"Found existing HubSpot company ID {company_id}")
                        else:
                            company_id = _hs_create_company(headers, zi_co)
                            if company_id:
                                enrichment_log.append(f"Created HubSpot company ID {company_id}")

                        if company_id:
                            _hs_associate_contact_company(headers, contact_id, company_id)

                    # Patch HubSpot company with any newly available fields.
                    # ZI company enrich returns "primaryIndustry", not "industry".
                    zi_industry = zi_co.get("primaryIndustry") or zi_co.get("industry", "")
                    if company_id:
                        hs_co_patch = {}
                        if needs_employees and zi_emp:
                            hs_co_patch["numberofemployees"] = str(zi_emp)
                        if not (company_props.get("industry") or "").strip() and zi_industry:
                            hs_co_patch["industry"] = zi_industry
                        if hs_co_patch:
                            _hs_patch_company(headers, company_id, hs_co_patch)

                    # Merge ZI data into in-memory company_props (fill blanks only).
                    _merge = {
                        "name":              zi_name,
                        "industry":          zi_industry,
                        "numberofemployees": str(zi_emp) if zi_emp else "",
                        "city":              zi_co.get("city", ""),
                        "country":           zi_co.get("country", ""),
                        "website":           zi_raw_url,
                        "annualrevenue":     str(zi_co["revenue"]) if zi_co.get("revenue") else "",
                    }
                    for k, v in _merge.items():
                        if v and not (company_props.get(k) or "").strip():
                            company_props[k] = v

                else:
                    enrichment_log.append(
                        f"ZoomInfo company: no match (domain={lookup_domain!r} name={company_name!r})"
                    )
                    print("  ZI company: no result", file=sys.stderr)
            else:
                enrichment_log.append("ZoomInfo unavailable — credentials not configured")
        else:
            enrichment_log.append("Company lookup skipped — no domain or name to search")

    # ── Phase 2: Contact fields ───────────────────────────────────────────────
    missing_contact = [
        f for f, _ in IMPORTANT_CONTACT_FIELDS
        if not (contact_props.get(f) or "").strip()
    ]

    if missing_contact and contact_email:
        jwt = jwt or _get_zoominfo_jwt()
        if jwt:
            print(f"ZoomInfo contact enrichment for {contact_email}")
            zi_ct = _zi_enrich_contact(jwt, contact_email)

            if zi_ct:
                enrichment_log.append(
                    f"ZoomInfo contact found: {zi_ct.get('firstName', '')} {zi_ct.get('lastName', '')} "
                    f"| {zi_ct.get('jobTitle', 'no title')}"
                )
                hs_ct_patch = {}
                for prop_name, zi_key in [
                    ("firstname", "firstName"),
                    ("lastname",  "lastName"),
                    ("jobtitle",  "jobTitle"),
                ]:
                    if not (contact_props.get(prop_name) or "").strip() and zi_ct.get(zi_key):
                        contact_props[prop_name] = zi_ct[zi_key]
                        hs_ct_patch[prop_name]   = zi_ct[zi_key]

                # ZI contact response includes company fields (companyEmployeeCount etc.)
                if not (company_props.get("numberofemployees") or "").strip() and zi_ct.get("companyEmployeeCount"):
                    company_props["numberofemployees"] = str(zi_ct["companyEmployeeCount"])
                if not (company_props.get("industry") or "").strip() and zi_ct.get("companyPrimaryIndustry"):
                    company_props["industry"] = zi_ct["companyPrimaryIndustry"]
                if not (company_props.get("name") or "").strip() and zi_ct.get("companyName"):
                    company_props["name"] = zi_ct["companyName"]
                if not (company_props.get("website") or "").strip() and zi_ct.get("companyWebsite"):
                    company_props["website"] = zi_ct["companyWebsite"]

                if hs_ct_patch:
                    _hs_patch_contact(headers, contact_id, hs_ct_patch)
            else:
                enrichment_log.append("ZoomInfo contact: no match")
                print("  ZI contact: no result", file=sys.stderr)

    # ── Phase 3: Final gap check + SDR note ───────────────────────────────────
    final_gaps = _check_gaps(contact_props, company_props)

    if final_gaps:
        print(f"Remaining gaps after enrichment ({len(final_gaps)}):")
        for g in final_gaps:
            print(f"  - {g}")
        _hs_post_sdr_note(headers, contact_id, final_gaps, enrichment_log)
    else:
        print("All important fields resolved after enrichment — no SDR note needed")

    # ── Write enriched hubspot_contact.json ───────────────────────────────────
    data["contact_properties"] = contact_props
    data["company_properties"] = company_props
    data["company_id"]         = company_id
    data["enrichment_log"]     = enrichment_log
    data["enrichment_gaps"]    = final_gaps

    with open(hs_path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"hubspot_contact.json updated — enrichment complete")


if __name__ == "__main__":
    main()
