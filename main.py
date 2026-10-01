import os
import secrets
import hashlib
import base64
import json

from email.utils import parseaddr
from email.message import EmailMessage

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


app = FastAPI(title="Residency Pre-Pilot Backend")


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
    "https://www.googleapis.com/auth/spreadsheets.readonly"
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
# =========================================================

def get_residents():
    sheets = get_sheets_service()

    result = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=RESIDENT_REGISTRY_SPREADSHEET_ID,
            range="Approved_Residents!A:D"
        )
        .execute()
    )

    rows = result.get("values", [])

    residents = []

    for row in rows[1:]:
        if not row:
            continue

        residents.append({
            "resident_id": (
                row[0].strip()
                if len(row) > 0 else ""
            ),
            "name": (
                row[1].strip()
                if len(row) > 1 else ""
            ),
            "email": (
                row[2].strip().lower()
                if len(row) > 2 else ""
            ),
            "active": (
                row[3].strip().upper()
                if len(row) > 3 else ""
            )
        })

    return residents


def authorize_sender(sender_email):
    normalized_email = sender_email.strip().lower()

    for resident in get_residents():

        if resident["email"] == normalized_email:

            if resident["active"] == "TRUE":
                return {
                    "authorized": True,
                    "resident_id": resident["resident_id"],
                    "name": resident["name"],
                    "email": resident["email"]
                }

            return {
                "authorized": False,
                "reason": "inactive"
            }

    return {
        "authorized": False,
        "reason": "not_registered"
    }


# =========================================================
# Gmail Helpers
# =========================================================

def get_latest_inbox_message():
    gmail = get_gmail_service()

    result = (
        gmail.users()
        .messages()
        .list(
            userId="me",
            labelIds=["INBOX"],
            maxResults=1
        )
        .execute()
    )

    messages = result.get("messages", [])

    if not messages:
        return None

    message_id = messages[0]["id"]

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
        header["name"].lower(): header["value"]
        for header in message
        .get("payload", {})
        .get("headers", [])
    }

    sender_name, sender_email = parseaddr(
        headers.get("from", "")
    )

    return {
        "message_id": message_id,
        "thread_id": message.get("threadId"),
        "sender_name": sender_name,
        "sender_email": sender_email.strip().lower(),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "rfc_message_id": headers.get("message-id", "")
    }


def send_decline_reply(message):
    gmail = get_gmail_service()

    original_subject = message["subject"]

    if original_subject.lower().startswith("re:"):
        reply_subject = original_subject
    else:
        reply_subject = f"Re: {original_subject}"

    body = (
        "Thank you for contacting The Tech Residency Program.\n\n"
        "This email address is not currently authorized to interact "
        "with the Residency system. If you believe this is an error, "
        "please contact the Residency Program coordinator using your "
        "registered email address.\n\n"
        "The Tech Residency Program"
    )

    email_message = EmailMessage()

    email_message["To"] = message["sender_email"]
    email_message["Subject"] = reply_subject

    if message["rfc_message_id"]:
        email_message["In-Reply-To"] = message["rfc_message_id"]
        email_message["References"] = message["rfc_message_id"]

    email_message.set_content(body)

    encoded_message = base64.urlsafe_b64encode(
        email_message.as_bytes()
    ).decode()

    sent_message = (
        gmail.users()
        .messages()
        .send(
            userId="me",
            body={
                "raw": encoded_message,
                "threadId": message["thread_id"]
            }
        )
        .execute()
    )

    return sent_message


# =========================================================
# Basic Endpoints
# =========================================================

@app.get("/")
def home():
    return {
        "service": "Residency Pre-Pilot Backend",
        "status": "running"
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

    code_verifier = secrets.token_urlsafe(64)

    digest = hashlib.sha256(
        code_verifier.encode()
    ).digest()

    code_challenge = (
        base64.urlsafe_b64encode(digest)
        .decode()
        .rstrip("=")
    )

    state_data = {
        "cv": code_verifier
    }

    state = base64.urlsafe_b64encode(
        json.dumps(state_data).encode()
    ).decode()

    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
        code_challenge=code_challenge,
        code_challenge_method="S256",
    )

    return RedirectResponse(authorization_url)


@app.get("/oauth2/callback")
def oauth_callback(request: Request):
    state = request.query_params.get("state")

    if not state:
        return {
            "status": "error",
            "message": "OAuth state is missing."
        }

    try:
        padded_state = state + "=" * (-len(state) % 4)

        state_data = json.loads(
            base64.urlsafe_b64decode(
                padded_state
            ).decode()
        )

        code_verifier = state_data["cv"]

    except Exception:
        return {
            "status": "error",
            "message": "Invalid OAuth state."
        }

    flow = create_flow()
    flow.code_verifier = code_verifier

    flow.fetch_token(
        authorization_response=str(request.url)
    )

    credentials = flow.credentials

    return {
        "status": "authorization_successful",
        "refresh_token_received": bool(
            credentials.refresh_token
        ),
        "message":
            "Google authorization completed successfully."
    }


# =========================================================
# Connectivity Tests
# =========================================================

@app.get("/test/gmail")
def test_gmail():
    gmail = get_gmail_service()

    profile = (
        gmail.users()
        .getProfile(userId="me")
        .execute()
    )

    return {
        "status": "gmail_connected",
        "email": profile.get("emailAddress"),
        "messages_total": profile.get("messagesTotal"),
        "threads_total": profile.get("threadsTotal")
    }


@app.get("/test/residents")
def test_residents():
    residents = get_residents()

    return {
        "status": "registry_connected",
        "resident_count": len(residents),
        "residents": residents
    }


# =========================================================
# Latest Email Authorization Test
# =========================================================

@app.get("/test/latest-email")
def test_latest_email():
    message = get_latest_inbox_message()

    if not message:
        return {
            "status": "no_inbox_messages"
        }

    authorization = authorize_sender(
        message["sender_email"]
    )

    return {
        "status": "email_checked",
        **message,
        "authorization": authorization
    }


# =========================================================
# Manual Processing Test
# =========================================================

@app.get("/test/process-latest-email")
def process_latest_email():
    message = get_latest_inbox_message()

    if not message:
        return {
            "status": "no_inbox_messages"
        }

    authorization = authorize_sender(
        message["sender_email"]
    )

    # Authorized resident:
    # Do not send anything yet.
    if authorization["authorized"]:
        return {
            "status": "authorized",
            "action": "none",
            "message_id": message["message_id"],
            "sender_email": message["sender_email"],
            "resident": authorization
        }

    # Unauthorized/inactive sender:
    # Fixed deterministic decline reply.
    sent_message = send_decline_reply(message)

    return {
        "status": "unauthorized",
        "action": "decline_sent",
        "message_id": message["message_id"],
        "sender_email": message["sender_email"],
        "internal_reason": authorization["reason"],
        "sent_message_id": sent_message.get("id"),
        "thread_id": sent_message.get("threadId")
    }
