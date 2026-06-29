import json
import os
import sys
from datetime import datetime, timezone

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInput, ApiException
import requests as req_lib
from tenacity import retry, retry_if_exception

from utils import (
    write_dlq,
    _is_hubspot_transient,
    _is_requests_transient,
    HS_RETRY_KWARGS,
    REQ_RETRY_KWARGS,
    safe_truncate,
)


EMAIL_PROPS = {
    1: ("subject_1", "email_1"),
}

_hs_retry = retry(retry=retry_if_exception(_is_hubspot_transient), **HS_RETRY_KWARGS)
_req_retry = retry(retry=retry_if_exception(_is_requests_transient), **REQ_RETRY_KWARGS)


@_hs_retry
def _hs_update(client, contact_id, input_obj):
    return client.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=input_obj,
    )


@_req_retry
def _hs_post_note(url, headers, payload):
    resp = req_lib.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp


def h(tag, text):
    return f"<{tag}>{text}</{tag}>"


def build_note_body(output):
    generated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    parts = [
        h("h3", "Staff Domain Inbound Campaign") +
        f"<div>Generated {generated}</div>"
    ]

    email_lines = []
    labels = {
        1: "Step 1 — First touch",
    }
    for i in range(1, 2):
        email = output.get(f"email_{i}")
        if email and email.get("subject"):
            email_lines.append(
                f"<li><strong>{labels[i]}:</strong> {email['subject']}</li>"
            )
    if email_lines:
        parts.append(h("h4", "Email Sequence") + f"<ul>{''.join(email_lines)}</ul>")

    return "<br><br>".join(parts)


def main():
    contact_id = os.environ["INPUT_CONTACT_ID"]
    contact_email = os.environ.get("INPUT_CONTACT_EMAIL", "unknown")
    runner_temp = os.environ["RUNNER_TEMP"]

    write_dlq(contact_id, contact_email, "write_hubspot", "Script started", retry_count=0)

    with open(os.path.join(runner_temp, "campaign_output.json"), encoding="utf-8") as f:
        output = json.load(f)

    print(f"campaign_output.json loaded: keys={list(output.keys())}")

    client = hubspot.Client.create(access_token=os.environ["HUBSPOT_API_KEY"])
    headers = {"Authorization": f"Bearer {os.environ['HUBSPOT_API_KEY']}"}

    properties = {}

    for i, (subj_prop, body_prop) in EMAIL_PROPS.items():
        email = output.get(f"email_{i}")
        if not email:
            continue
        if isinstance(email, dict):
            properties[subj_prop] = safe_truncate(email.get("subject") or "", 1024)
            properties[body_prop] = safe_truncate(email.get("body") or "", 65000)
        else:
            properties[body_prop] = safe_truncate(str(email), 65000)

    print(f"Properties to write ({len(properties)}): {list(properties.keys())}")

    try:
        _hs_update(client, contact_id, SimplePublicObjectInput(properties=properties))
        print(f"Contact {contact_id} updated ({len(properties)} fields)")

        try:
            note_body = build_note_body(output)
            note_payload = {
                "properties": {
                    "hs_note_body": note_body,
                    "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                },
                "associations": [
                    {
                        "to": {"id": str(contact_id)},
                        "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
                    }
                ],
            }
            note_resp = _hs_post_note(
                "https://api.hubapi.com/crm/v3/objects/notes",
                headers,
                note_payload,
            )
            print(f"Note created: ID {note_resp.json().get('id')}")
        except Exception as e:
            print(f"WARNING: note creation failed (properties already written): {e}", file=sys.stderr)

        print(f"Write-back complete: {len(properties)} properties + note on contact {contact_id}")

    except ApiException as exc:
        retry_count = getattr(getattr(exc, "__cause__", None), "statistics", {}).get("attempt_number", 1)
        write_dlq(contact_id, contact_email, "write_hubspot", exc, retry_count)
        raise


if __name__ == "__main__":
    main()
