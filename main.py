import os
import base64
import json
import re
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build


app = FastAPI(title="Residency Pre-Pilot Backend")

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REDIRECT_URI = os.environ["GOOGLE_REDIRECT_URI"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
SPREADSHEET_ID = os.environ["RESIDENT_REGISTRY_SPREADSHEET_ID"]

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]
SYSTEM_STATE_SHEET = "System_State"
GMAIL_HISTORY_KEY = "GMAIL_LAST_HISTORY_ID"

latest_gmail_notification = None
latest_authorized_email = None
processed_message_ids = set()
MAX_RUNTIME_MESSAGE_IDS = 500
GMAIL_POLL_INTERVAL_SECONDS = 30 * 60
_gmail_poll_lock = threading.Lock()


# ---------------------------------------------------------------------
# Google clients
# ---------------------------------------------------------------------

def credentials():
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    # Refresh explicitly through google-auth's requests transport. This avoids
    # relying on googleapiclient/httplib2 to perform the OAuth token refresh.
    creds.refresh(GoogleAuthRequest())
    return creds


def gmail():
    return build("gmail", "v1", credentials=credentials(), cache_discovery=False)


def sheets():
    return build("sheets", "v4", credentials=credentials(), cache_discovery=False)


def create_flow():
    config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }
    }
    return Flow.from_client_config(
        config,
        scopes=SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
        autogenerate_code_verifier=False,
    )


# ---------------------------------------------------------------------
# Sheets helpers
# ---------------------------------------------------------------------

def read_values(range_name):
    return (
        sheets().spreadsheets().values()
        .get(spreadsheetId=SPREADSHEET_ID, range=range_name)
        .execute()
        .get("values", [])
    )


def update_values(range_name, values):
    return (
        sheets().spreadsheets().values()
        .update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption="RAW",
            body={"values": values},
        )
        .execute()
    )


def append_values(range_name, values):
    return (
        sheets().spreadsheets().values()
        .append(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        )
        .execute()
    )


def ensure_system_state_sheet():
    svc = sheets()
    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if SYSTEM_STATE_SHEET not in titles:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": SYSTEM_STATE_SHEET}}}]},
        ).execute()
        update_values(f"{SYSTEM_STATE_SHEET}!A1:B2", [
            ["Key", "Value"],
            [GMAIL_HISTORY_KEY, ""],
        ])
        return

    rows = read_values(f"{SYSTEM_STATE_SHEET}!A:B")
    if not rows:
        update_values(f"{SYSTEM_STATE_SHEET}!A1:B2", [
            ["Key", "Value"],
            [GMAIL_HISTORY_KEY, ""],
        ])
    elif not any(row and row[0] == GMAIL_HISTORY_KEY for row in rows[1:]):
        append_values(f"{SYSTEM_STATE_SHEET}!A:B", [[GMAIL_HISTORY_KEY, ""]])


def get_history_cursor():
    ensure_system_state_sheet()
    rows = read_values(f"{SYSTEM_STATE_SHEET}!A:B")
    for row in rows[1:]:
        if row and row[0] == GMAIL_HISTORY_KEY:
            if len(row) > 1 and str(row[1]).strip():
                return int(row[1])
            return None
    return None


def set_history_cursor(history_id):
    ensure_system_state_sheet()
    rows = read_values(f"{SYSTEM_STATE_SHEET}!A:B")
    for i, row in enumerate(rows[1:], start=2):
        if row and row[0] == GMAIL_HISTORY_KEY:
            update_values(f"{SYSTEM_STATE_SHEET}!B{i}", [[str(int(history_id))]])
            return int(history_id)
    append_values(f"{SYSTEM_STATE_SHEET}!A:B", [[GMAIL_HISTORY_KEY, str(int(history_id))]])
    return int(history_id)


# ---------------------------------------------------------------------
# Residents
# ---------------------------------------------------------------------

def get_residents():
    rows = read_values("Approved_Residents!A:F")
    result = []
    for sheet_row, row in enumerate(rows[1:], start=2):
        if not row:
            continue
        row = row + [""] * (6 - len(row))
        result.append({
            "sheet_row": sheet_row,
            "resident_id": row[0].strip(),
            "name": row[1].strip(),
            "email": row[2].strip().lower(),
            "status": row[3].strip().upper(),
            "counter": int(row[4]) if str(row[4]).strip().isdigit() else 0,
            "mentor": row[5].strip(),
        })
    return result


def get_resident_by_email(email):
    email = (email or "").strip().lower()
    for resident in get_residents():
        if resident["email"] == email:
            return resident
    return None


def update_resident_state(sheet_row, status=None, counter=None):
    if status is not None:
        update_values(f"Approved_Residents!D{sheet_row}", [[status]])
    if counter is not None:
        update_values(f"Approved_Residents!E{sheet_row}", [[counter]])


# ---------------------------------------------------------------------
# Backlogs and assignments
# ---------------------------------------------------------------------

def get_backlogs():
    rows = read_values("Backlogs!A:H")
    result = []
    for row in rows[1:]:
        if not row:
            continue
        row = row + [""] * (8 - len(row))
        result.append({
            "backlog_id": row[0].strip(),
            "title": row[1].strip(),
            "project": row[2].strip(),
            "description": row[3].strip(),
            "expected_output": row[4].strip(),
            "completion_conditions": row[5].strip(),
            "estimated_effort": row[6].strip(),
            "active": row[7].strip().lower() in {"true", "yes", "1", "active"},
        })
    return result


def select_pilot_backlog():
    return next((b for b in get_backlogs() if b["active"]), None)


def get_backlog_by_id(backlog_id):
    return next((b for b in get_backlogs() if b["backlog_id"] == backlog_id), None)


def get_assignments():
    rows = read_values("Assignments!A:G")
    result = []
    for sheet_row, row in enumerate(rows[1:], start=2):
        if not row:
            continue
        row = row + [""] * (7 - len(row))
        result.append({
            "sheet_row": sheet_row,
            "assignment_id": row[0].strip(),
            "resident_id": row[1].strip(),
            "backlog_id": row[2].strip(),
            "status": row[3].strip().upper(),
            "assigned_at": row[4].strip(),
            "submitted_at": row[5].strip(),
            "github_url": row[6].strip(),
        })
    return result


def get_active_assignment_for_resident(resident_id):
    active = {"ACTIVE", "SUBMITTED"}
    matches = [
        a for a in get_assignments()
        if a["resident_id"] == resident_id and a["status"] in active
    ]
    return matches[-1] if matches else None


def generate_assignment_id():
    nums = []
    for a in get_assignments():
        m = re.fullmatch(r"ASG(\d+)", a["assignment_id"], re.I)
        if m:
            nums.append(int(m.group(1)))
    return f"ASG{(max(nums, default=0) + 1):03d}"


def create_assignment(resident_id, backlog_id):
    assignment_id = generate_assignment_id()
    assigned_at = datetime.now(timezone.utc).isoformat()
    append_values("Assignments!A:G", [[
        assignment_id, resident_id, backlog_id, "ACTIVE", assigned_at, "", ""
    ]])
    return assignment_id


def record_submission(assignment, github_url):
    submitted_at = datetime.now(timezone.utc).isoformat()
    row = assignment["sheet_row"]
    update_values(f"Assignments!D{row}:G{row}", [[
        "SUBMITTED",
        assignment["assigned_at"],
        submitted_at,
        github_url,
    ]])
    return submitted_at


def get_current_work_context(resident_id):
    assignment = get_active_assignment_for_resident(resident_id)
    if not assignment:
        return None, None
    return assignment, get_backlog_by_id(assignment["backlog_id"])


# ---------------------------------------------------------------------
# Gmail message helpers
# ---------------------------------------------------------------------

def header_value(headers, name):
    target = name.lower()
    for h in headers or []:
        if h.get("name", "").lower() == target:
            return h.get("value", "")
    return ""


def decode_body_data(data):
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")


def extract_plain_text(payload):
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    if mime == "text/plain" and body.get("data"):
        return decode_body_data(body["data"])
    parts = payload.get("parts", [])
    for part in parts:
        text = extract_plain_text(part)
        if text:
            return text
    if body.get("data"):
        return decode_body_data(body["data"])
    return ""


def get_message(message_id):
    raw = gmail().users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute()
    headers = raw.get("payload", {}).get("headers", [])
    sender = parseaddr(header_value(headers, "From"))[1].strip().lower()
    return {
        "id": raw["id"],
        "thread_id": raw.get("threadId"),
        "label_ids": raw.get("labelIds", []),
        "from": sender,
        "to": header_value(headers, "To"),
        "subject": header_value(headers, "Subject"),
        "message_id_header": header_value(headers, "Message-ID"),
        "references": header_value(headers, "References"),
        "body": extract_plain_text(raw.get("payload", {})),
    }


def get_own_email():
    return (
        gmail().users().getProfile(userId="me").execute()
        .get("emailAddress", "")
        .strip()
        .lower()
    )


def send_email(recipient, subject, body, thread_id=None, in_reply_to=None, references=None):
    msg = EmailMessage()
    msg["To"] = recipient
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)

    payload = {
        "raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()
    }
    if thread_id:
        payload["threadId"] = thread_id

    return gmail().users().messages().send(userId="me", body=payload).execute()


def reply(message, body):
    subject = message.get("subject") or "TTRP"
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    refs = " ".join(
        x for x in [message.get("references"), message.get("message_id_header")] if x
    )
    return send_email(
        message["from"],
        subject,
        body,
        thread_id=message.get("thread_id"),
        in_reply_to=message.get("message_id_header"),
        references=refs or None,
    )


# ---------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------

def first_name(resident):
    return resident["name"].split()[0] if resident["name"] else "Resident"


def process_joined_resident(resident):
    body = (
        f"Hello {first_name(resident)},\n\n"
        "Welcome to The Tech Residency Program (TTRP).\n\n"
        "Please reply to this email with a short introduction, your preferred technical "
        "track, current skills, and the areas you want to strengthen.\n\n"
        "Regards,\nTTRP"
    )
    sent = send_email(resident["email"], "Welcome to TTRP", body)
    update_resident_state(resident["sheet_row"], status="PENDING")
    return {"resident_id": resident["resident_id"], "sent_message_id": sent.get("id")}


def process_pending_reply(resident, message):
    body = (
        f"Hello {first_name(resident)},\n\n"
        "Thank you. Your initial information has been received. "
        "Your residency profile is now ready for the first work allocation.\n\n"
        "Regards,\nTTRP"
    )
    sent = reply(message, body)
    update_resident_state(resident["sheet_row"], status="READY")
    return {"action": "pending_to_ready", "sent_message_id": sent.get("id")}


def process_ready_resident(resident, backlog=None):
    backlog = backlog or select_pilot_backlog()
    if not backlog:
        return {"action": "failed", "reason": "no_active_backlog"}

    assignment_id = create_assignment(resident["resident_id"], backlog["backlog_id"])
    body = (
        f"Hello {first_name(resident)},\n\n"
        f"Your first backlog has been assigned.\n\n"
        f"Backlog: {backlog['backlog_id']} — {backlog['title']}\n"
        f"Project: {backlog['project']}\n"
        f"Description: {backlog['description']}\n"
        f"Expected output: {backlog['expected_output']}\n"
        f"Completion conditions: {backlog['completion_conditions']}\n"
        f"Estimated effort: {backlog['estimated_effort']}\n\n"
        "When the work is complete, submit exactly in this format:\n"
        "SUBMIT: https://github.com/username/repository\n\n"
        "Regards,\nTTRP"
    )
    sent = send_email(resident["email"], f"TTRP Backlog {backlog['backlog_id']}", body)
    update_resident_state(resident["sheet_row"], status="ACTIVE", counter=3)
    return {
        "action": "ready_to_active",
        "assignment_id": assignment_id,
        "sent_message_id": sent.get("id"),
    }


def extract_submission_url(body):
    match = re.search(
        r"(?im)^\s*SUBMIT\s*:\s*(https?://(?:www\.)?github\.com/[^\s<>]+)",
        body or "",
    )
    if not match:
        return None
    url = match.group(1).rstrip(".,);]")
    if "your-username/your-repository" in url.lower():
        return None
    return url


def process_active_submission(resident, message, github_url):
    assignment, backlog = get_current_work_context(resident["resident_id"])
    if not assignment:
        return {"action": "failed", "reason": "no_active_assignment"}

    body = (
        f"Hello {first_name(resident)},\n\n"
        f"Your submission for {assignment['backlog_id']} has been received.\n"
        f"Repository: {github_url}\n\n"
        "The submission has been recorded and will move to the evaluation process.\n\n"
        "Regards,\nTTRP"
    )

    # Send first; only transition state after Gmail confirms the acknowledgement.
    sent = reply(message, body)
    submitted_at = record_submission(assignment, github_url)
    update_resident_state(resident["sheet_row"], status="SUBMITTED")
    return {
        "action": "active_submission_processed",
        "submitted_at": submitted_at,
        "sent_message_id": sent.get("id"),
    }


def process_active_reply(resident, message):
    assignment, backlog = get_current_work_context(resident["resident_id"])
    if not assignment:
        return {"action": "failed", "reason": "no_active_assignment"}

    body = (
        f"Hello {first_name(resident)},\n\n"
        "Thanks for the update. Continue with the current backlog. "
        "Your activity counter has been restored to 3.\n\n"
        "When the work is complete, submit exactly as:\n"
        "SUBMIT: https://github.com/username/repository\n\n"
        "Regards,\nTTRP"
    )
    sent = reply(message, body)
    update_resident_state(resident["sheet_row"], counter=3)
    return {"action": "active_reply_processed", "sent_message_id": sent.get("id")}


def process_inactive_reply(resident, message):
    body = (
        f"Hello {first_name(resident)},\n\n"
        "Your residency has been resumed. You will return to READY status "
        "and receive work through the normal allocation process.\n\n"
        "Regards,\nTTRP"
    )
    sent = reply(message, body)
    update_resident_state(resident["sheet_row"], status="READY", counter=0)
    return {"action": "inactive_to_ready", "sent_message_id": sent.get("id")}


def process_dead_reply(resident, message):
    body = (
        f"Hello {first_name(resident)},\n\n"
        "This residency has already been terminated. This message does not "
        "reactivate the residency.\n\nRegards,\nTTRP"
    )
    sent = reply(message, body)
    return {"action": "dead_reply", "sent_message_id": sent.get("id")}


def process_message(message_id):
    global latest_authorized_email

    if message_id in processed_message_ids:
        return {"action": "duplicate_ignored", "message_id": message_id}

    # Claim locally BEFORE any outbound reply. This is critical: sending a reply
    # changes Gmail history and can immediately trigger another Pub/Sub webhook.
    processed_message_ids.add(message_id)
    if len(processed_message_ids) > MAX_RUNTIME_MESSAGE_IDS:
        # Preserve the currently claimed message while bounding runtime memory.
        processed_message_ids.clear()
        processed_message_ids.add(message_id)

    message = get_message(message_id)

    # Absolute loop guard: only true inbound inbox messages can drive lifecycle.
    if "INBOX" not in set(message.get("label_ids", [])):
        return {"action": "non_inbox_ignored", "message_id": message_id}

    own_email = get_own_email()
    if message["from"] == own_email:
        return {"action": "self_message_ignored", "message_id": message_id}

    if message["from"].startswith("mailer-daemon@") or message["from"].startswith("postmaster@"):
        return {"action": "system_message_ignored", "message_id": message_id}

    resident = get_resident_by_email(message["from"])
    if not resident:
        return {"action": "unauthorized_sender_ignored", "message_id": message_id}

    latest_authorized_email = {
        "message_id": message_id,
        "from": message["from"],
        "subject": message["subject"],
        "resident_id": resident["resident_id"],
        "status": resident["status"],
    }

    status = resident["status"]
    if status == "PENDING":
        return process_pending_reply(resident, message)
    if status == "ACTIVE":
        github_url = extract_submission_url(message["body"])
        if github_url:
            return process_active_submission(resident, message, github_url)
        return process_active_reply(resident, message)
    if status == "INACTIVE":
        return process_inactive_reply(resident, message)
    if status == "DEAD":
        return process_dead_reply(resident, message)

    return {"action": "no_inbound_action", "status": status, "message_id": message_id}


# ---------------------------------------------------------------------
# Gmail history
# ---------------------------------------------------------------------

def get_new_inbox_message_ids(start_history_id):
    svc = gmail()
    ids = []
    page_token = None

    while True:
        kwargs = {
            "userId": "me",
            "startHistoryId": str(start_history_id),
            "historyTypes": ["messageAdded"],
        }
        if page_token:
            kwargs["pageToken"] = page_token

        response = svc.users().history().list(**kwargs).execute()
        for history in response.get("history", []):
            for item in history.get("messagesAdded", []):
                message = item.get("message", {})
                # Gmail history itself tells us the labels at addition time.
                # Only INBOX messages are candidates. SENT messages can never
                # become lifecycle input.
                if "INBOX" in set(message.get("labelIds", [])):
                    mid = message.get("id")
                    if mid and mid not in ids:
                        ids.append(mid)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return ids


# ---------------------------------------------------------------------
# Gmail polling
# ---------------------------------------------------------------------

def check_gmail_now():
    """Process new inbox messages since the durable Gmail history cursor."""
    profile = gmail().users().getProfile(userId="me").execute()
    current = int(profile["historyId"])
    previous = get_history_cursor()

    if previous is None:
        set_history_cursor(current)
        return {
            "status": "history_baseline_initialized",
            "new_last_history_id": current,
            "message_ids": [],
            "processing_results": [],
        }

    if current <= previous:
        return {
            "status": "no_new_history",
            "previous_history_id": previous,
            "new_last_history_id": current,
            "message_ids": [],
            "processing_results": [],
        }

    message_ids = get_new_inbox_message_ids(previous)

    # Advance the durable cursor before processing. Sending TTRP replies changes
    # Gmail history; advancing first prevents those outbound changes from causing
    # the same inbound message to be rediscovered on the next poll.
    set_history_cursor(current)

    results = []
    for message_id in message_ids:
        try:
            results.append(process_message(message_id))
        except Exception as exc:
            results.append({
                "action": "failed",
                "message_id": message_id,
                "error": type(exc).__name__,
            })

    return {
        "status": "history_processed",
        "previous_history_id": previous,
        "new_last_history_id": current,
        "message_ids": message_ids,
        "processing_results": results,
    }


# ---------------------------------------------------------------------
# Scheduled deterministic processes
# ---------------------------------------------------------------------

def process_all_joined_residents():
    results = []
    for resident in get_residents():
        if resident["status"] == "JOINED":
            results.append(process_joined_resident(resident))
    return results


def process_all_ready_residents():
    results = []
    for resident in get_residents():
        if resident["status"] == "READY":
            results.append(process_ready_resident(resident))
    return results


def process_scheduled_active_resident(resident):
    assignment, backlog = get_current_work_context(resident["resident_id"])
    if not assignment:
        return {"action": "failed", "reason": "no_active_assignment"}

    current = resident["counter"]
    new_counter = max(current - 1, 0)

    if new_counter == 0:
        body = (
            f"Hello {first_name(resident)},\n\n"
            "No meaningful update was received within the current activity window. "
            "Your residency is now INACTIVE. Reply when you are ready to resume.\n\n"
            "Regards,\nTTRP"
        )
        sent = send_email(resident["email"], "TTRP Activity Status", body)
        update_resident_state(resident["sheet_row"], status="INACTIVE", counter=0)
        return {"action": "active_to_inactive", "sent_message_id": sent.get("id")}

    body = (
        f"Hello {first_name(resident)},\n\n"
        f"This is your scheduled Scrum follow-up for {assignment['backlog_id']}. "
        f"Your activity counter is now {new_counter}. Please reply with a meaningful update.\n\n"
        "Regards,\nTTRP"
    )
    sent = send_email(resident["email"], "TTRP Scrum Follow-up", body)
    update_resident_state(resident["sheet_row"], counter=new_counter)
    return {
        "action": "scheduled_scrum_sent",
        "counter": new_counter,
        "sent_message_id": sent.get("id"),
    }


def process_all_active_residents():
    return [
        process_scheduled_active_resident(r)
        for r in get_residents()
        if r["status"] == "ACTIVE"
    ]


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------

@app.get("/")
def home():
    return {"service": "Residency Pre-Pilot Backend", "status": "running"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/auth/google")
def google_auth():
    flow = create_flow()
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return RedirectResponse(url)


@app.get("/oauth2/callback")
def oauth_callback(request: Request):
    flow = create_flow()
    flow.fetch_token(authorization_response=str(request.url))
    creds = flow.credentials
    return {
        "status": "authorized",
        "has_refresh_token": bool(creds.refresh_token),
    }


@app.get("/test/gmail")
def test_gmail():
    profile = gmail().users().getProfile(userId="me").execute()
    return {
        "status": "ok",
        "email": profile.get("emailAddress"),
        "history_id": profile.get("historyId"),
    }


@app.get("/test/residents")
def test_residents():
    return {"residents": get_residents()}


@app.get("/test/process-joined")
def test_process_joined():
    return {"results": process_all_joined_residents()}


@app.get("/test/process-ready")
def test_process_ready():
    return {"results": process_all_ready_residents()}


@app.get("/test/process-active")
def test_process_active():
    return {"results": process_all_active_residents()}


@app.post("/webhooks/gmail")
async def gmail_webhook(request: Request):
    global latest_gmail_notification

    envelope = await request.json()
    message = envelope.get("message", {})
    encoded = message.get("data")
    if not encoded:
        return {"status": "ignored", "reason": "missing_pubsub_data"}

    try:
        decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
        incoming = int(decoded["historyId"])
    except Exception:
        return {"status": "ignored", "reason": "invalid_pubsub_data"}

    latest_gmail_notification = decoded
    previous = get_history_cursor()

    if previous is None:
        set_history_cursor(incoming)
        return {
            "status": "history_baseline_initialized",
            "new_last_history_id": incoming,
        }

    if incoming <= previous:
        return {
            "status": "stale_notification_ignored",
            "previous_history_id": previous,
            "incoming_history_id": incoming,
        }

    # CRITICAL LOOP FIX:
    # Advance the durable cursor BEFORE processing/sending replies.
    # Outbound TTRP mail changes Gmail history and triggers Pub/Sub again.
    # If the cursor were advanced only after sending, the nested webhook could
    # rediscover the same resident message and send another reply.
    message_ids = get_new_inbox_message_ids(previous)
    set_history_cursor(incoming)

    results = []
    for message_id in message_ids:
        try:
            results.append(process_message(message_id))
        except Exception as exc:
            results.append({
                "action": "failed",
                "message_id": message_id,
                "error": type(exc).__name__,
            })

    return {
        "status": "history_processed",
        "previous_history_id": previous,
        "new_last_history_id": incoming,
        "message_ids": message_ids,
        "processing_results": results,
    }


@app.get("/gmail/check")
def manual_gmail_check():
    return check_gmail_now()


def gmail_polling_loop():
    """Run the same Gmail checker automatically every 30 minutes."""
    while True:
        time.sleep(GMAIL_POLL_INTERVAL_SECONDS)
        if not _gmail_poll_lock.acquire(blocking=False):
            continue
        try:
            try:
                check_gmail_now()
            except Exception as exc:
                print(f"[gmail-poll] {type(exc).__name__}: {exc}", flush=True)
        finally:
            _gmail_poll_lock.release()


@app.on_event("startup")
def start_gmail_polling():
    threading.Thread(
        target=gmail_polling_loop,
        name="gmail-polling-loop",
        daemon=True,
    ).start()


@app.get("/test/latest-notification")
def get_latest_notification():
    return latest_gmail_notification or {"status": "none"}


@app.get("/test/latest-authorized-email")
def get_latest_authorized_email():
    return latest_authorized_email or {"status": "none"}


@app.get("/test/runtime-state")
def get_runtime_state():
    return {
        "processed_message_count": len(processed_message_ids),
        "persistent_history_id": get_history_cursor(),
    }


@app.get("/test/start-gmail-watch")
def start_gmail_watch():
    topic = "projects/residency-prepilot/topics/gmail-residency-inbox"
    response = gmail().users().watch(
        userId="me",
        body={"topicName": topic, "labelIds": ["INBOX"]},
    ).execute()

    history_id = int(response["historyId"])
    set_history_cursor(history_id)

    return {
        "status": "gmail_watch_started",
        "history_id": history_id,
        "expiration": response.get("expiration"),
        "persistent_history_id": get_history_cursor(),
    }
