import os
import secrets
import hashlib
import base64
import json
import html
import re

from email.utils import parseaddr
from email.message import EmailMessage

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


app = FastAPI(title="Residency Pre-Pilot Backend")


# =========================================================
# Temporary Runtime State
# Later this will move to persistent storage.
# =========================================================

latest_gmail_notification = None
latest_authorized_email = None

last_history_id = None

processed_message_ids = set()


# =========================================================
# Environment Variables
# =========================================================

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REDIRECT_URI = os.environ["GOOGLE_REDIRECT_URI"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]

RESIDENT_REGISTRY_SPREADSHEET_ID = os.environ[
    "RESIDENT_REGISTRY_SPREADSHEET_ID"
]


# =========================================================
# Google API Scopes
# =========================================================

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets"
]


# =========================================================
# Google Authentication
# =========================================================

def create_flow():
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }
    }

    return Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
        autogenerate_code_verifier=False,
    )


def get_google_credentials():
    return Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )


# =========================================================
# Google Services
# =========================================================

def get_gmail_service():
    return build(
        "gmail",
        "v1",
        credentials=get_google_credentials()
    )


def get_sheets_service():
    return build(
        "sheets",
        "v4",
        credentials=get_google_credentials()
    )


# =========================================================
# Resident Registry
#
# A = Resident_ID
# B = Name
# C = Email
# D = Status
# E = Counter
# F = Mentor
# =========================================================

VALID_RESIDENT_STATUSES = {
    "JOINED",
    "PENDING",
    "READY",
    "ACTIVE",
    "SUBMITTED",
    "INACTIVE",
    "DEAD",
}


def get_residents():
    sheets = get_sheets_service()

    result = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=RESIDENT_REGISTRY_SPREADSHEET_ID,
            range="Approved_Residents!A:F"
        )
        .execute()
    )

    rows = result.get("values", [])

    residents = []

    for sheet_row, row in enumerate(
        rows[1:],
        start=2
    ):
        if not row:
            continue

        resident_id = (
            row[0].strip()
            if len(row) > 0
            else ""
        )

        name = (
            row[1].strip()
            if len(row) > 1
            else ""
        )

        email_address = (
            row[2].strip().lower()
            if len(row) > 2
            else ""
        )

        status = (
            row[3].strip().upper()
            if len(row) > 3
            else ""
        )

        counter_raw = (
            row[4].strip()
            if len(row) > 4
            else ""
        )

        mentor = (
            row[5].strip().lower()
            if len(row) > 5
            else ""
        )

        try:
            counter = (
                int(counter_raw)
                if counter_raw != ""
                else None
            )
        except ValueError:
            counter = None

        residents.append({
            "resident_id": resident_id,
            "name": name,
            "email": email_address,
            "status": status,
            "counter": counter,
            "mentor": mentor,
            "sheet_row": sheet_row
        })

    return residents


def get_resident_by_email(sender_email):
    normalized_email = (
        sender_email
        .strip()
        .lower()
    )

    for resident in get_residents():
        if resident["email"] == normalized_email:
            return resident

    return None


def get_resident_by_id(resident_id):
    normalized_id = (
        resident_id
        .strip()
        .upper()
    )

    for resident in get_residents():
        if (
            resident["resident_id"].upper()
            == normalized_id
        ):
            return resident

    return None


def authorize_sender(sender_email):
    resident = get_resident_by_email(
        sender_email
    )

    if resident is None:
        return {
            "authorized": False
        }

    return {
        "authorized": True,
        "resident_id": resident["resident_id"],
        "name": resident["name"],
        "email": resident["email"],
        "status": resident["status"],
        "counter": resident["counter"],
        "mentor": resident["mentor"],
        "sheet_row": resident["sheet_row"]
    }


# =========================================================
# Resident Registry Writes
# =========================================================

def update_resident_state(
    sheet_row,
    status=None,
    counter=None
):
    if status is not None:
        status = (
            str(status)
            .strip()
            .upper()
        )

        if status not in VALID_RESIDENT_STATUSES:
            raise ValueError(
                f"Invalid resident status: {status}"
            )

    sheets = get_sheets_service()

    updates = []

    if status is not None:
        updates.append({
            "range":
                f"Approved_Residents!D{sheet_row}",
            "values": [[status]]
        })

    if counter is not None:
        updates.append({
            "range":
                f"Approved_Residents!E{sheet_row}",
            "values": [[counter]]
        })

    if not updates:
        return {
            "updated": False,
            "updated_cells": 0
        }

    body = {
        "valueInputOption": "RAW",
        "data": updates
    }

    result = (
        sheets.spreadsheets()
        .values()
        .batchUpdate(
            spreadsheetId=
                RESIDENT_REGISTRY_SPREADSHEET_ID,
            body=body
        )
        .execute()
    )

    return {
        "updated": True,
        "updated_cells":
            result.get(
                "totalUpdatedCells",
                0
            )
    }


# =========================================================
# Generic Outgoing Email
# =========================================================

def send_email(
    recipient,
    subject,
    body
):
    gmail = get_gmail_service()

    email_message = EmailMessage()

    email_message["To"] = recipient
    email_message["Subject"] = subject

    email_message.set_content(body)

    encoded_message = (
        base64.urlsafe_b64encode(
            email_message.as_bytes()
        )
        .decode()
    )

    sent_message = (
        gmail.users()
        .messages()
        .send(
            userId="me",
            body={
                "raw": encoded_message
            }
        )
        .execute()
    )

    return sent_message


# =========================================================
# Welcome Template
# JOINED -> PENDING
# =========================================================

def build_welcome_message(resident):
    first_name = (
        resident["name"]
        .strip()
        .split()[0]
        if resident["name"].strip()
        else "Resident"
    )

    subject = (
        "Welcome to The Tech Residency Program"
    )

    body = f"""Hi {first_name},

Welcome to The Tech Residency Program (TTRP). Glad to have you with us.

Before we get you started, I'd like to know a little about your current technical background. Please reply to this email with the following:

- Your preferred area(s) of work
- Programming languages you are comfortable with
- Technologies/tools you have worked with
- Your GitHub username/profile
- A short description of any project you have previously worked on

Don't worry about having experience in everything. This information simply helps us understand where you're starting from.

Once we receive your response, we'll complete your onboarding and get you ready for your first assignment.

Regards,
John Doe
Tech Lead
The Tech Residency Program"""

    return {
        "subject": subject,
        "body": body
    }


# =========================================================
# First Residency Workflow
# JOINED -> Welcome Email -> PENDING
# =========================================================

def process_joined_resident(resident):
    if resident["status"] != "JOINED":
        return {
            "resident_id":
                resident["resident_id"],
            "action":
                "skipped",
            "reason":
                "resident_not_joined"
        }

    if not resident["email"]:
        return {
            "resident_id":
                resident["resident_id"],
            "action":
                "failed",
            "reason":
                "resident_email_missing"
        }

    welcome = build_welcome_message(
        resident
    )

    # IMPORTANT:
    # Send first.
    # Change status only after Gmail confirms success.
    sent_message = send_email(
        recipient=resident["email"],
        subject=welcome["subject"],
        body=welcome["body"]
    )

    sent_message_id = (
        sent_message.get("id")
    )

    if not sent_message_id:
        return {
            "resident_id":
                resident["resident_id"],
            "action":
                "failed",
            "reason":
                "gmail_send_not_confirmed"
        }

    update_result = update_resident_state(
        sheet_row=resident["sheet_row"],
        status="PENDING"
    )

    return {
        "resident_id":
            resident["resident_id"],
        "resident_email":
            resident["email"],
        "action":
            "welcome_sent",
        "sent_message_id":
            sent_message_id,
        "old_status":
            "JOINED",
        "new_status":
            "PENDING",
        "sheet_update":
            update_result
    }


def process_all_joined_residents():
    residents = get_residents()

    joined_residents = [
        resident
        for resident in residents
        if resident["status"] == "JOINED"
    ]

    results = []

    for resident in joined_residents:
        try:
            result = process_joined_resident(
                resident
            )

        except Exception as error:
            result = {
                "resident_id":
                    resident["resident_id"],
                "action":
                    "failed",
                "error":
                    str(error)
            }

        results.append(result)

    return {
        "joined_resident_count":
            len(joined_residents),
        "results":
            results
    }


# =========================================================
# Gmail Metadata Helpers
# =========================================================

def get_message_metadata(message_id):
    gmail = get_gmail_service()

    message = (
        gmail.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=[
                "From",
                "To",
                "Subject",
                "Date",
                "Message-ID"
            ]
        )
        .execute()
    )

    headers = {
        header["name"].lower():
            header["value"]
        for header in (
            message
            .get("payload", {})
            .get("headers", [])
        )
    }

    sender_name, sender_email = parseaddr(
        headers.get("from", "")
    )

    return {
        "message_id":
            message_id,
        "thread_id":
            message.get("threadId"),
        "label_ids":
            message.get("labelIds", []),
        "sender_name":
            sender_name,
        "sender_email":
            sender_email.strip().lower(),
        "subject":
            headers.get("subject", ""),
        "date":
            headers.get("date", ""),
        "rfc_message_id":
            headers.get("message-id", "")
    }


# =========================================================
# Email Body Extraction
# =========================================================

def decode_body_data(data):
    if not data:
        return ""

    padded_data = (
        data
        + "=" * (-len(data) % 4)
    )

    decoded_bytes = (
        base64.urlsafe_b64decode(
            padded_data
        )
    )

    return decoded_bytes.decode(
        "utf-8",
        errors="replace"
    )


def extract_plain_text_from_payload(payload):
    mime_type = payload.get(
        "mimeType",
        ""
    )

    body_data = (
        payload
        .get("body", {})
        .get("data")
    )

    if (
        mime_type == "text/plain"
        and body_data
    ):
        return (
            decode_body_data(
                body_data
            )
            .strip()
        )

    parts = payload.get(
        "parts",
        []
    )

    # Prefer text/plain.
    for part in parts:
        if (
            part.get("mimeType")
            == "text/plain"
        ):
            text = (
                extract_plain_text_from_payload(
                    part
                )
            )

            if text:
                return text

    # Search nested multipart structures.
    for part in parts:
        if part.get("parts"):
            text = (
                extract_plain_text_from_payload(
                    part
                )
            )

            if text:
                return text

    # HTML fallback.
    if (
        mime_type == "text/html"
        and body_data
    ):
        html_content = (
            decode_body_data(
                body_data
            )
        )

        text = re.sub(
            r"<[^>]+>",
            " ",
            html_content
        )

        text = html.unescape(
            text
        )

        text = re.sub(
            r"\s+",
            " ",
            text
        )

        return text.strip()

    for part in parts:
        if (
            part.get("mimeType")
            == "text/html"
        ):
            text = (
                extract_plain_text_from_payload(
                    part
                )
            )

            if text:
                return text

    return ""


def get_full_authorized_message(message_id):
    gmail = get_gmail_service()

    message = (
        gmail.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="full"
        )
        .execute()
    )

    payload = message.get(
        "payload",
        {}
    )

    body = (
        extract_plain_text_from_payload(
            payload
        )
    )

    return {
        "body":
            body,
        "snippet":
            message.get(
                "snippet",
                ""
            )
    }


# =========================================================
# Fixed Decline Reply
# =========================================================

def send_decline_reply(message):
    gmail = get_gmail_service()

    original_subject = (
        message["subject"]
    )

    if (
        original_subject
        .lower()
        .startswith("re:")
    ):
        reply_subject = (
            original_subject
        )
    else:
        reply_subject = (
            f"Re: {original_subject}"
        )

    body = (
        "Thank you for contacting "
        "The Tech Residency Program.\n\n"

        "This email address is not currently "
        "authorized to interact with the Residency "
        "system. If you believe this is an error, "
        "please contact the Residency Program "
        "coordinator using your registered email "
        "address.\n\n"

        "Regards,\n"
        "John Doe\n"
        "Tech Lead\n"
        "The Tech Residency Program"
    )

    email_message = EmailMessage()

    email_message["To"] = (
        message["sender_email"]
    )

    email_message["Subject"] = (
        reply_subject
    )

    if message["rfc_message_id"]:
        email_message["In-Reply-To"] = (
            message["rfc_message_id"]
        )

        email_message["References"] = (
            message["rfc_message_id"]
        )

    email_message.set_content(
        body
    )

    encoded_message = (
        base64.urlsafe_b64encode(
            email_message.as_bytes()
        )
        .decode()
    )

    sent_message = (
        gmail.users()
        .messages()
        .send(
            userId="me",
            body={
                "raw":
                    encoded_message,
                "threadId":
                    message["thread_id"]
            }
        )
        .execute()
    )

    return sent_message


# =========================================================
# Gmail History
# =========================================================

def get_new_message_ids(
    start_history_id
):
    gmail = get_gmail_service()

    message_ids = set()
    page_token = None

    while True:
        request = (
            gmail.users()
            .history()
            .list(
                userId="me",
                startHistoryId=str(
                    start_history_id
                ),
                historyTypes=[
                    "messageAdded"
                ],
                labelId="INBOX",
                pageToken=page_token
            )
        )

        result = request.execute()

        for history_record in (
            result.get(
                "history",
                []
            )
        ):
            for added in (
                history_record.get(
                    "messagesAdded",
                    []
                )
            ):
                message = (
                    added.get(
                        "message",
                        {}
                    )
                )

                message_id = (
                    message.get("id")
                )

                label_ids = (
                    message.get(
                        "labelIds",
                        []
                    )
                )

                if (
                    message_id
                    and "INBOX" in label_ids
                ):
                    message_ids.add(
                        message_id
                    )

        page_token = (
            result.get(
                "nextPageToken"
            )
        )

        if not page_token:
            break

    return list(
        message_ids
    )


# =========================================================
# Process Exact Gmail Message
# =========================================================

def process_message(message_id):
    global latest_authorized_email

    if (
        message_id
        in processed_message_ids
    ):
        return {
            "message_id":
                message_id,
            "action":
                "duplicate_ignored"
        }

    message = (
        get_message_metadata(
            message_id
        )
    )

    if (
        "INBOX"
        not in message["label_ids"]
    ):
        processed_message_ids.add(
            message_id
        )

        return {
            "message_id":
                message_id,
            "action":
                "non_inbox_ignored"
        }

    authorization = (
        authorize_sender(
            message["sender_email"]
        )
    )

    # -----------------------------------------------------
    # Unknown / unregistered email.
    # Do NOT retrieve body.
    # -----------------------------------------------------

    if not authorization["authorized"]:
        sent_message = (
            send_decline_reply(
                message
            )
        )

        processed_message_ids.add(
            message_id
        )

        return {
            "message_id":
                message_id,
            "sender_email":
                message["sender_email"],
            "action":
                "decline_sent",
            "sent_message_id":
                sent_message.get("id")
        }

    # -----------------------------------------------------
    # Registered resident.
    # Only now retrieve full content.
    # -----------------------------------------------------

    full_message = (
        get_full_authorized_message(
            message_id
        )
    )

    latest_authorized_email = {
        "message_id":
            message["message_id"],
        "thread_id":
            message["thread_id"],
        "resident_id":
            authorization["resident_id"],
        "resident_name":
            authorization["name"],
        "sender_email":
            authorization["email"],
        "status":
            authorization["status"],
        "counter":
            authorization["counter"],
        "mentor":
            authorization["mentor"],
        "sheet_row":
            authorization["sheet_row"],
        "subject":
            message["subject"],
        "date":
            message["date"],
        "body":
            full_message["body"],
        "snippet":
            full_message["snippet"]
    }

    processed_message_ids.add(
        message_id
    )

    return {
        "message_id":
            message_id,
        "sender_email":
            message["sender_email"],
        "resident_id":
            authorization["resident_id"],
        "resident_name":
            authorization["name"],
        "resident_status":
            authorization["status"],
        "resident_counter":
            authorization["counter"],
        "action":
            "registered_resident_email_extracted"
    }


# =========================================================
# Basic Endpoints
# =========================================================

@app.get("/")
def home():
    return {
        "service":
            "Residency Pre-Pilot Backend",
        "status":
            "running"
    }


@app.get("/health")
def health():
    return {
        "status": "ok"
    }


# =========================================================
# OAuth
# =========================================================

@app.get("/auth/google")
def google_auth():
    flow = create_flow()

    code_verifier = (
        secrets.token_urlsafe(64)
    )

    digest = hashlib.sha256(
        code_verifier.encode()
    ).digest()

    code_challenge = (
        base64.urlsafe_b64encode(
            digest
        )
        .decode()
        .rstrip("=")
    )

    state_data = {
        "cv":
            code_verifier
    }

    state = (
        base64.urlsafe_b64encode(
            json.dumps(
                state_data
            ).encode()
        )
        .decode()
    )

    authorization_url, _ = (
        flow.authorization_url(
            access_type="offline",
            prompt="consent",
            state=state,
            code_challenge=
                code_challenge,
            code_challenge_method=
                "S256",
        )
    )

    return RedirectResponse(
        authorization_url
    )


@app.get("/oauth2/callback")
def oauth_callback(
    request: Request
):
    state = (
        request.query_params.get(
            "state"
        )
    )

    if not state:
        return {
            "status": "error",
            "message":
                "OAuth state is missing."
        }

    try:
        padded_state = (
            state
            + "="
            * (-len(state) % 4)
        )

        state_data = json.loads(
            base64.urlsafe_b64decode(
                padded_state
            ).decode()
        )

        code_verifier = (
            state_data["cv"]
        )

    except Exception:
        return {
            "status": "error",
            "message":
                "Invalid OAuth state."
        }

    flow = create_flow()

    flow.code_verifier = (
        code_verifier
    )

    flow.fetch_token(
        authorization_response=str(
            request.url
        )
    )

    credentials = flow.credentials

    return {
        "status":
            "authorization_successful",
        "refresh_token_received":
            bool(
                credentials.refresh_token
            ),
        "message":
            "Google authorization "
            "completed successfully."
    }


# =========================================================
# Connectivity Tests
# =========================================================

@app.get("/test/gmail")
def test_gmail():
    gmail = get_gmail_service()

    profile = (
        gmail.users()
        .getProfile(
            userId="me"
        )
        .execute()
    )

    return {
        "status":
            "gmail_connected",
        "email":
            profile.get(
                "emailAddress"
            ),
        "messages_total":
            profile.get(
                "messagesTotal"
            ),
        "threads_total":
            profile.get(
                "threadsTotal"
            )
    }


@app.get("/test/residents")
def test_residents():
    residents = get_residents()

    return {
        "status":
            "registry_connected",
        "resident_count":
            len(residents),
        "residents":
            residents
    }


# =========================================================
# Temporary State-Write Test
# =========================================================

@app.get(
    "/test/update-resident-state/{resident_id}"
)
def test_update_resident_state(
    resident_id: str,
    status: str = None,
    counter: int = None
):
    resident = get_resident_by_id(
        resident_id
    )

    if resident is None:
        return {
            "status":
                "resident_not_found",
            "resident_id":
                resident_id
        }

    if (
        status is None
        and counter is None
    ):
        return {
            "status":
                "no_change_requested",
            "resident":
                resident,
            "message":
                "Provide status and/or counter "
                "as query parameters."
        }

    try:
        result = (
            update_resident_state(
                sheet_row=
                    resident["sheet_row"],
                status=status,
                counter=counter
            )
        )

    except ValueError as error:
        return {
            "status":
                "invalid_request",
            "message":
                str(error)
        }

    return {
        "status":
            "resident_state_updated",
        "resident_id":
            resident_id,
        "result":
            result
    }


# =========================================================
# TEST: Process JOINED Residents
#
# For development only.
# Later this function will be triggered by the orchestrator.
# =========================================================

@app.get("/test/process-joined")
def test_process_joined():
    try:
        result = (
            process_all_joined_residents()
        )

        return {
            "status":
                "joined_processing_complete",
            **result
        }

    except HttpError as error:
        return {
            "status":
                "google_api_error",
            "error":
                str(error)
        }

    except Exception as error:
        return {
            "status":
                "processing_error",
            "error":
                str(error)
        }


# =========================================================
# Gmail Pub/Sub Webhook
# =========================================================

@app.post("/webhooks/gmail")
async def gmail_pubsub_webhook(
    request: Request
):
    global latest_gmail_notification
    global last_history_id

    try:
        payload = (
            await request.json()
        )

        pubsub_message = (
            payload.get(
                "message",
                {}
            )
        )

        encoded_data = (
            pubsub_message.get(
                "data",
                ""
            )
        )

        decoded_data = {}

        if encoded_data:
            padded_data = (
                encoded_data
                + "="
                * (-len(encoded_data) % 4)
            )

            decoded_bytes = (
                base64.urlsafe_b64decode(
                    padded_data
                )
            )

            decoded_data = (
                json.loads(
                    decoded_bytes.decode(
                        "utf-8"
                    )
                )
            )

        incoming_history_id = (
            decoded_data.get(
                "historyId"
            )
        )

        latest_gmail_notification = {
            "pubsub_message_id":
                pubsub_message.get(
                    "messageId"
                ),
            "publish_time":
                pubsub_message.get(
                    "publishTime"
                ),
            "email_address":
                decoded_data.get(
                    "emailAddress"
                ),
            "incoming_history_id":
                incoming_history_id,
            "previous_history_id":
                last_history_id,
            "processing_results":
                []
        }

        if not incoming_history_id:
            latest_gmail_notification[
                "status"
            ] = "missing_history_id"

            return {
                "status":
                    "notification_received"
            }

        if last_history_id is None:
            last_history_id = (
                incoming_history_id
            )

            latest_gmail_notification[
                "status"
            ] = (
                "history_baseline_initialized"
            )

            return {
                "status":
                    "notification_received"
            }

        message_ids = (
            get_new_message_ids(
                last_history_id
            )
        )

        processing_results = []

        for message_id in message_ids:
            result = (
                process_message(
                    message_id
                )
            )

            processing_results.append(
                result
            )

        last_history_id = (
            incoming_history_id
        )

        latest_gmail_notification[
            "status"
        ] = "history_processed"

        latest_gmail_notification[
            "processing_results"
        ] = processing_results

        latest_gmail_notification[
            "new_last_history_id"
        ] = last_history_id

        return {
            "status":
                "notification_received"
        }

    except HttpError as error:
        latest_gmail_notification = {
            "status":
                "gmail_history_error",
            "error":
                str(error)
        }

        return {
            "status":
                "notification_error"
        }

    except Exception as error:
        latest_gmail_notification = {
            "status":
                "processing_error",
            "error":
                str(error)
        }

        return {
            "status":
                "notification_error"
        }


# =========================================================
# Diagnostics
# =========================================================

@app.get("/test/latest-notification")
def get_latest_notification():

    if (
        latest_gmail_notification
        is None
    ):
        return {
            "status":
                "no_notification_received"
        }

    return {
        "status":
            "notification_available",
        "notification":
            latest_gmail_notification
    }


@app.get("/test/latest-authorized-email")
def get_latest_authorized_email():

    if (
        latest_authorized_email
        is None
    ):
        return {
            "status":
                "no_authorized_email_extracted"
        }

    return {
        "status":
            "authorized_email_available",
        "email":
            latest_authorized_email
    }


@app.get("/test/runtime-state")
def get_runtime_state():
    return {
        "last_history_id":
            last_history_id,
        "processed_message_count":
            len(
                processed_message_ids
            )
    }


# =========================================================
# Start / Renew Gmail Inbox Watch
# =========================================================

@app.get("/test/start-gmail-watch")
def start_gmail_watch():
    global last_history_id

    gmail = get_gmail_service()

    request_body = {
        "topicName": (
            "projects/residency-prepilot/"
            "topics/gmail-residency-inbox"
        ),
        "labelIds": [
            "INBOX"
        ],
        "labelFilterBehavior":
            "INCLUDE"
    }

    result = (
        gmail.users()
        .watch(
            userId="me",
            body=request_body
        )
        .execute()
    )

    last_history_id = (
        result.get(
            "historyId"
        )
    )

    return {
        "status":
            "gmail_watch_started",
        "history_id":
            result.get(
                "historyId"
            ),
        "expiration":
            result.get(
                "expiration"
            ),
        "history_baseline_saved":
            True
    }
