import os
import secrets
import hashlib
import base64
import json
from email.utils import parseaddr

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


app = FastAPI(title="Residency Pre-Pilot Backend")


# ---------------------------------------------------------
# Environment Variables
# ---------------------------------------------------------

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REDIRECT_URI = os.environ["GOOGLE_REDIRECT_URI"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]

RESIDENT_REGISTRY_SPREADSHEET_ID = os.environ[
    "RESIDENT_REGISTRY_SPREADSHEET_ID"
]


# ---------------------------------------------------------
# Google API Scopes
# ---------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets.readonly"
]


# ---------------------------------------------------------
# OAuth
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# Resident Registry
# ---------------------------------------------------------

def get_residents():
    credentials = get_google_credentials()

    sheets = build(
        "sheets",
        "v4",
        credentials=credentials
    )

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
            "resident_id": row[0].strip() if len(row) > 0 else "",
            "name": row[1].strip() if len(row) > 1 else "",
            "email": row[2].strip().lower() if len(row) > 2 else "",
            "active": row[3].strip().upper() if len(row) > 3 else ""
        })

    return residents


def authorize_sender(sender_email):
    normalized_email = sender_email.strip().lower()

    residents = get_residents()

    for resident in residents:
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


# ---------------------------------------------------------
# Basic Endpoints
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# Google Authorization
# ---------------------------------------------------------

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
        "message": "Google authorization completed successfully."
    }


# ---------------------------------------------------------
# Gmail Connection Test
# ---------------------------------------------------------

@app.get("/test/gmail")
def test_gmail():
    credentials = get_google_credentials()

    gmail = build(
        "gmail",
        "v1",
        credentials=credentials
    )

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


# ---------------------------------------------------------
# Resident Registry Test
# ---------------------------------------------------------

@app.get("/test/residents")
def test_residents():
    residents = get_residents()

    return {
        "status": "registry_connected",
        "resident_count": len(residents),
        "residents": residents
    }


# ---------------------------------------------------------
# Latest Inbox Message + Authorization Test
# ---------------------------------------------------------

@app.get("/test/latest-email")
def test_latest_email():
    credentials = get_google_credentials()

    gmail = build(
        "gmail",
        "v1",
        credentials=credentials
    )

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
        return {
            "status": "no_inbox_messages"
        }

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
                "Subject",
                "Date"
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

    raw_from = headers.get("from", "")

    sender_name, sender_email = parseaddr(raw_from)

    sender_email = sender_email.strip().lower()

    authorization = authorize_sender(sender_email)

    return {
        "status": "email_checked",
        "message_id": message_id,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "authorization": authorization
    }
