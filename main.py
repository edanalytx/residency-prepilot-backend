import os
import secrets
import hashlib
import base64
import json
import html
import re

from datetime import datetime, timezone
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
#
# Later:
# - Gmail history cursor
# - processed message IDs
# will move to persistent storage.
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
    "https://www.googleapis.com/auth/spreadsheets",
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
        credentials=get_google_credentials(),
    )


def get_sheets_service():
    return build(
        "sheets",
        "v4",
        credentials=get_google_credentials(),
    )


# =========================================================
# Resident Registry
#
# Approved_Residents
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
            range="Approved_Residents!A:F",
        )
        .execute()
    )

    rows = result.get("values", [])

    residents = []

    for sheet_row, row in enumerate(
        rows[1:],
        start=2,
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
            "sheet_row": sheet_row,
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
        "sheet_row": resident["sheet_row"],
    }


# =========================================================
# Resident Registry Writes
# =========================================================

def update_resident_state(
    sheet_row,
    status=None,
    counter=None,
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
            "values": [[status]],
        })

    if counter is not None:
        updates.append({
            "range":
                f"Approved_Residents!E{sheet_row}",
            "values": [[counter]],
        })

    if not updates:
        return {
            "updated": False,
            "updated_cells": 0,
        }

    result = (
        sheets.spreadsheets()
        .values()
        .batchUpdate(
            spreadsheetId=
                RESIDENT_REGISTRY_SPREADSHEET_ID,
            body={
                "valueInputOption": "RAW",
                "data": updates,
            },
        )
        .execute()
    )

    return {
        "updated": True,
        "updated_cells":
            result.get(
                "totalUpdatedCells",
                0,
            ),
    }


# =========================================================
# Backlog Catalog
#
# Backlogs
#
# A = Backlog_ID
# B = Title
# C = Project
# D = Description
# E = Expected_Output
# F = Completion_Conditions
# G = Estimated_Effort
# H = Active
# =========================================================

def get_backlogs():
    sheets = get_sheets_service()

    result = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=
                RESIDENT_REGISTRY_SPREADSHEET_ID,
            range="Backlogs!A:H",
        )
        .execute()
    )

    rows = result.get("values", [])

    backlogs = []

    for sheet_row, row in enumerate(
        rows[1:],
        start=2,
    ):
        if not row:
            continue

        backlogs.append({
            "backlog_id": (
                row[0].strip()
                if len(row) > 0
                else ""
            ),
            "title": (
                row[1].strip()
                if len(row) > 1
                else ""
            ),
            "project": (
                row[2].strip()
                if len(row) > 2
                else ""
            ),
            "description": (
                row[3].strip()
                if len(row) > 3
                else ""
            ),
            "expected_output": (
                row[4].strip()
                if len(row) > 4
                else ""
            ),
            "completion_conditions": (
                row[5].strip()
                if len(row) > 5
                else ""
            ),
            "estimated_effort": (
                row[6].strip()
                if len(row) > 6
                else ""
            ),
            "active": (
                row[7].strip().upper()
                if len(row) > 7
                else ""
            ),
            "sheet_row": sheet_row,
        })

    return backlogs


def get_active_backlogs():
    return [
        backlog
        for backlog in get_backlogs()
        if backlog["active"] == "TRUE"
    ]


def get_backlog_by_id(backlog_id):
    normalized_id = (
        backlog_id
        .strip()
        .upper()
    )

    for backlog in get_backlogs():
        if (
            backlog["backlog_id"]
            .upper()
            == normalized_id
        ):
            return backlog

    return None


def select_pilot_backlog():
    """
    Temporary pre-pilot allocation rule.

    Select the first active Backlog.

    Later this function becomes the insertion
    point for the Job Allocation AI.
    """

    active_backlogs = (
        get_active_backlogs()
    )

    if not active_backlogs:
        return None

    return active_backlogs[0]


# =========================================================
# Assignment Registry
#
# Assignments
#
# A = Assignment_ID
# B = Resident_ID
# C = Backlog_ID
# D = Status
# E = Assigned_At
# F = Submitted_At
# G = GitHub_URL
# =========================================================

def get_assignments():
    sheets = get_sheets_service()

    result = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=
                RESIDENT_REGISTRY_SPREADSHEET_ID,
            range="Assignments!A:G",
        )
        .execute()
    )

    rows = result.get("values", [])

    assignments = []

    for sheet_row, row in enumerate(
        rows[1:],
        start=2,
    ):
        if not row:
            continue

        assignments.append({
            "assignment_id": (
                row[0].strip()
                if len(row) > 0
                else ""
            ),
            "resident_id": (
                row[1].strip()
                if len(row) > 1
                else ""
            ),
            "backlog_id": (
                row[2].strip()
                if len(row) > 2
                else ""
            ),
            "status": (
                row[3].strip().upper()
                if len(row) > 3
                else ""
            ),
            "assigned_at": (
                row[4].strip()
                if len(row) > 4
                else ""
            ),
            "submitted_at": (
                row[5].strip()
                if len(row) > 5
                else ""
            ),
            "github_url": (
                row[6].strip()
                if len(row) > 6
                else ""
            ),
            "sheet_row": sheet_row,
        })

    return assignments


def get_active_assignment_for_resident(
    resident_id
):
    normalized_id = (
        resident_id
        .strip()
        .upper()
    )

    for assignment in get_assignments():
        if (
            assignment["resident_id"].upper()
            == normalized_id
            and
            assignment["status"]
            == "ACTIVE"
        ):
            return assignment

    return None


def generate_assignment_id():
    assignments = get_assignments()

    highest_number = 0

    for assignment in assignments:
        assignment_id = (
            assignment["assignment_id"]
            .strip()
            .upper()
        )

        match = re.fullmatch(
            r"ASG(\d+)",
            assignment_id,
        )

        if match:
            number = int(
                match.group(1)
            )

            highest_number = max(
                highest_number,
                number,
            )

    return (
        f"ASG{highest_number + 1:03d}"
    )


def create_assignment(
    resident_id,
    backlog_id,
):
    existing = (
        get_active_assignment_for_resident(
            resident_id
        )
    )

    if existing:
        return {
            "created": False,
            "reason":
                "active_assignment_already_exists",
            "assignment":
                existing,
        }

    sheets = get_sheets_service()

    assignment_id = (
        generate_assignment_id()
    )

    assigned_at = (
        datetime.now(timezone.utc)
        .isoformat()
    )

    result = (
        sheets.spreadsheets()
        .values()
        .append(
            spreadsheetId=
                RESIDENT_REGISTRY_SPREADSHEET_ID,
            range="Assignments!A:G",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={
                "values": [[
                    assignment_id,
                    resident_id,
                    backlog_id,
                    "ACTIVE",
                    assigned_at,
                    "",
                    "",
                ]]
            },
        )
        .execute()
    )

    return {
        "created": True,
        "assignment_id":
            assignment_id,
        "resident_id":
            resident_id,
        "backlog_id":
            backlog_id,
        "status":
            "ACTIVE",
        "assigned_at":
            assigned_at,
        "updated_range":
            (
                result
                .get("updates", {})
                .get("updatedRange")
            ),
    }


def update_assignment_status(
    sheet_row,
    status,
):
    normalized_status = (
        str(status)
        .strip()
        .upper()
    )

    sheets = get_sheets_service()

    result = (
        sheets.spreadsheets()
        .values()
        .update(
            spreadsheetId=
                RESIDENT_REGISTRY_SPREADSHEET_ID,
            range=
                f"Assignments!D{sheet_row}",
            valueInputOption="RAW",
            body={
                "values": [[
                    normalized_status
                ]]
            },
        )
        .execute()
    )

    return {
        "updated": True,
        "status":
            normalized_status,
        "updated_cells":
            result.get(
                "updatedCells",
                0,
            ),
    }


# =========================================================
# Current Work Context
# =========================================================

def get_current_work_context(
    resident_id
):
    assignment = (
        get_active_assignment_for_resident(
            resident_id
        )
    )

    if assignment is None:
        return None

    backlog = get_backlog_by_id(
        assignment["backlog_id"]
    )

    if backlog is None:
        return None

    return {
        "assignment":
            assignment,
        "backlog":
            backlog,
    }


# =========================================================
# Generic Outgoing Email
# =========================================================

def send_email(
    recipient,
    subject,
    body,
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

    return (
        gmail.users()
        .messages()
        .send(
            userId="me",
            body={
                "raw": encoded_message
            },
        )
        .execute()
    )


# =========================================================
# Threaded Reply Helper
# =========================================================

def send_threaded_reply(
    message,
    body,
):
    gmail = get_gmail_service()

    original_subject = (
        message["subject"]
        if message["subject"]
        else "The Tech Residency Program"
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

    email_message.set_content(body)

    encoded_message = (
        base64.urlsafe_b64encode(
            email_message.as_bytes()
        )
        .decode()
    )

    return (
        gmail.users()
        .messages()
        .send(
            userId="me",
            body={
                "raw":
                    encoded_message,
                "threadId":
                    message["thread_id"],
            },
        )
        .execute()
    )


# =========================================================
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
        "body": body,
    }


def process_joined_resident(
    resident
):
    if resident["status"] != "JOINED":
        return {
            "resident_id":
                resident["resident_id"],
            "action":
                "skipped",
            "reason":
                "resident_not_joined",
        }

    if not resident["email"]:
        return {
            "resident_id":
                resident["resident_id"],
            "action":
                "failed",
            "reason":
                "resident_email_missing",
        }

    welcome = (
        build_welcome_message(
            resident
        )
    )

    sent_message = send_email(
        recipient=
            resident["email"],
        subject=
            welcome["subject"],
        body=
            welcome["body"],
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
                "gmail_send_not_confirmed",
        }

    update_result = (
        update_resident_state(
            sheet_row=
                resident["sheet_row"],
            status="PENDING",
        )
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
            update_result,
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
            result = (
                process_joined_resident(
                    resident
                )
            )

        except Exception as error:
            result = {
                "resident_id":
                    resident["resident_id"],
                "action":
                    "failed",
                "error":
                    str(error),
            }

        results.append(result)

    return {
        "joined_resident_count":
            len(joined_residents),
        "results":
            results,
    }


# =========================================================
# PENDING -> READY
# =========================================================

def process_pending_reply(
    resident,
    message,
):
    first_name = (
        resident["name"]
        .strip()
        .split()[0]
        if resident["name"].strip()
        else "Resident"
    )

    body = f"""Hi {first_name},

Thanks for sharing your details. Your onboarding is now complete.

You're now ready to begin your residency with The Tech Residency Program.

You'll be working on a real-world-style project that has been divided into smaller engineering Backlogs. Each Backlog represents a specific piece of work with clear requirements, expected outputs, and completion conditions.

You don't need to understand the entire project before starting. You'll gradually become familiar with the system as you work through different Backlogs.

Your work will be maintained in GitHub, and I'll periodically check in with you through our Scrum emails while you're working on an assignment.

Your first Backlog will be assigned shortly.

Welcome to the team.

Regards,
John Doe
Tech Lead
The Tech Residency Program"""

    sent_message = (
        send_threaded_reply(
            message=message,
            body=body,
        )
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
                "onboarding_reply_not_confirmed",
        }

    update_result = (
        update_resident_state(
            sheet_row=
                resident["sheet_row"],
            status="READY",
        )
    )

    return {
        "resident_id":
            resident["resident_id"],
        "action":
            "onboarding_completed",
        "sent_message_id":
            sent_message_id,
        "old_status":
            "PENDING",
        "new_status":
            "READY",
        "sheet_update":
            update_result,
    }


# =========================================================
# READY -> ACTIVE / Counter 3
# =========================================================

def build_backlog_assignment_message(
    resident,
    backlog,
):
    first_name = (
        resident["name"]
        .strip()
        .split()[0]
        if resident["name"].strip()
        else "Resident"
    )

    subject = (
        f"Your First Backlog — "
        f"{backlog['backlog_id']}: "
        f"{backlog['title']}"
    )

    body = f"""Hi {first_name},

You're ready to begin your first assignment.

I've assigned you the following Backlog:

Backlog ID: {backlog['backlog_id']}
Project: {backlog['project']}
Title: {backlog['title']}

WHAT YOU'LL BE WORKING ON

{backlog['description']}

EXPECTED OUTPUT

{backlog['expected_output']}

COMPLETION CONDITIONS

{backlog['completion_conditions']}

ESTIMATED EFFORT

{backlog['estimated_effort']}

Please work on this assignment in your GitHub repository and commit your work regularly as you progress.

When the Backlog is complete, reply to this email with the GitHub repository link containing your submission.

You don't need to wait until completion to contact me. If you're blocked, unsure about a requirement, or need guidance, reply to this thread and let me know what you're working on and where you're stuck.

I'll also check in periodically while the Backlog is active.

Good luck with your assignment.

Regards,
John Doe
Tech Lead
The Tech Residency Program"""

    return {
        "subject": subject,
        "body": body,
    }


def process_ready_resident(
    resident,
    backlog,
):
    if resident["status"] != "READY":
        return {
            "resident_id":
                resident["resident_id"],
            "action":
                "skipped",
            "reason":
                "resident_not_ready",
        }

    if not resident["email"]:
        return {
            "resident_id":
                resident["resident_id"],
            "action":
                "failed",
            "reason":
                "resident_email_missing",
        }

    if backlog is None:
        return {
            "resident_id":
                resident["resident_id"],
            "action":
                "failed",
            "reason":
                "no_active_backlog_available",
        }

    existing_assignment = (
        get_active_assignment_for_resident(
            resident["resident_id"]
        )
    )

    if existing_assignment:
        return {
            "resident_id":
                resident["resident_id"],
            "action":
                "skipped",
            "reason":
                "active_assignment_already_exists",
            "assignment":
                existing_assignment,
        }

    assignment_message = (
        build_backlog_assignment_message(
            resident=resident,
            backlog=backlog,
        )
    )

    # 1. Send assignment.
    sent_message = send_email(
        recipient=
            resident["email"],
        subject=
            assignment_message["subject"],
        body=
            assignment_message["body"],
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
                "assignment_email_not_confirmed",
        }

    # 2. Persist assignment.
    assignment_result = (
        create_assignment(
            resident_id=
                resident["resident_id"],
            backlog_id=
                backlog["backlog_id"],
        )
    )

    if not assignment_result.get(
        "created"
    ):
        return {
            "resident_id":
                resident["resident_id"],
            "action":
                "assignment_record_failed",
            "sent_message_id":
                sent_message_id,
            "assignment_result":
                assignment_result,
        }

    # 3. Activate resident.
    update_result = (
        update_resident_state(
            sheet_row=
                resident["sheet_row"],
            status="ACTIVE",
            counter=3,
        )
    )

    return {
        "resident_id":
            resident["resident_id"],
        "resident_email":
            resident["email"],
        "assignment_id":
            assignment_result[
                "assignment_id"
            ],
        "backlog_id":
            backlog["backlog_id"],
        "backlog_title":
            backlog["title"],
        "action":
            "backlog_assigned",
        "sent_message_id":
            sent_message_id,
        "old_status":
            "READY",
        "new_status":
            "ACTIVE",
        "new_counter":
            3,
        "assignment_record":
            assignment_result,
        "sheet_update":
            update_result,
    }


def process_all_ready_residents():
    residents = get_residents()

    ready_residents = [
        resident
        for resident in residents
        if resident["status"] == "READY"
    ]

    backlog = (
        select_pilot_backlog()
    )

    results = []

    for resident in ready_residents:
        try:
            result = (
                process_ready_resident(
                    resident=resident,
                    backlog=backlog,
                )
            )

        except Exception as error:
            result = {
                "resident_id":
                    resident["resident_id"],
                "action":
                    "failed",
                "error":
                    str(error),
            }

        results.append(result)

    return {
        "ready_resident_count":
            len(ready_residents),
        "selected_backlog":
            (
                backlog["backlog_id"]
                if backlog
                else None
            ),
        "results":
            results,
    }


# =========================================================
# ACTIVE Scheduled Scrum Templates
#
# Escalation policy:
#
#  3  -> Normal
#  2  -> Normal
#  1  -> Concern
#  0  -> Concern
# -1  -> Escalation
# -2  -> Escalation
# -3  -> Final Warning
# -4  -> End active Backlog / INACTIVE
# =========================================================


def get_resident_first_name(resident):
    return (
        resident["name"]
        .strip()
        .split()[0]
        if resident["name"].strip()
        else "Resident"
    )


# =========================================================
# Level 1: Normal Scrum
# Counter 3 and 2
# =========================================================

def build_normal_scrum_message(
    resident,
    backlog,
    counter,
):
    first_name = (
        get_resident_first_name(
            resident
        )
    )

    if counter == 3:
        subject = (
            f"Scrum Check-in — "
            f"{backlog['backlog_id']}"
        )

        body = f"""Hi {first_name},

Just checking in on your current Backlog:

{backlog['backlog_id']} — {backlog['title']}

When you get a moment, reply with a short update on:

- What you've completed so far
- What you're currently working on
- Anything blocking your progress

A brief update is enough. If you're stuck somewhere, include the issue and I'll help you work through it.

Regards,
John Doe
Scrum Master
The Tech Residency Program"""

    else:
        subject = (
            f"Scrum Follow-up — "
            f"{backlog['backlog_id']}"
        )

        body = f"""Hi {first_name},

Following up on your current Backlog:

{backlog['backlog_id']} — {backlog['title']}

I haven't received your latest progress update yet.

Please reply with a brief update on what you've completed, what you're currently working on, and whether anything is blocking you.

If you're facing a problem, let me know so we can address it early.

Regards,
John Doe
Scrum Master
The Tech Residency Program"""

    return {
        "subject": subject,
        "body": body,
    }


# =========================================================
# Level 2: Concern
# Counter 1 and 0
# =========================================================

def build_concern_scrum_message(
    resident,
    backlog,
    counter,
):
    first_name = (
        get_resident_first_name(
            resident
        )
    )

    if counter == 1:
        subject = (
            f"Progress Update Due — "
            f"{backlog['backlog_id']}"
        )

        body = f"""Hi {first_name},

I'm still waiting for a progress update on your active Backlog:

{backlog['backlog_id']} — {backlog['title']}

We've had more than one check-in without an update, so I'd like to understand where the work currently stands.

Please reply with:

- Your current progress
- What you're working on now
- Any blocker or difficulty you're facing

Even if progress has been limited, please send an update. If something is preventing you from continuing, let me know so we can address it.

Regards,
John Doe
Scrum Master
The Tech Residency Program"""

    else:
        subject = (
            f"Progress Update Required — "
            f"{backlog['backlog_id']}"
        )

        body = f"""Hi {first_name},

We still haven't received a progress update for your active Backlog:

{backlog['backlog_id']} — {backlog['title']}

Regular communication is an important part of the residency workflow, particularly while a Backlog is active.

Please reply with your current progress or explain what is preventing you from continuing.

If you're blocked, unavailable, or facing another issue, simply let me know. The immediate priority is to understand the current status of the work.

Continued absence of progress updates will move the Backlog into the escalation stage.

Regards,
John Doe
Scrum Master
The Tech Residency Program"""

    return {
        "subject": subject,
        "body": body,
    }


# =========================================================
# Level 3: Escalation
# Counter -1 and -2
# =========================================================

def build_escalation_scrum_message(
    resident,
    backlog,
    counter,
):
    first_name = (
        get_resident_first_name(
            resident
        )
    )

    if counter == -1:
        subject = (
            f"Escalation: Progress Update Required — "
            f"{backlog['backlog_id']}"
        )

        body = f"""Hi {first_name},

Your active Backlog has now entered the escalation stage because we have not received a progress update after multiple check-ins.

Backlog:

{backlog['backlog_id']} — {backlog['title']}

Please reply as soon as possible with your current status.

You may report completed work, work in progress, a blocker, temporary unavailability, or any other issue affecting the assignment.

The purpose of this escalation is to establish whether the Backlog is still actively being worked on.

If we continue to receive no response, the Backlog will move toward closure due to inactivity.

Regards,
John Doe
Scrum Master
The Tech Residency Program"""

    else:
        subject = (
            f"Escalation Reminder: Response Required — "
            f"{backlog['backlog_id']}"
        )

        body = f"""Hi {first_name},

This is a further escalation regarding your active Backlog:

{backlog['backlog_id']} — {backlog['title']}

We have not received a response despite repeated Scrum check-ins and the previous escalation.

Please reply with your current status, even if you have not been able to make progress.

If there is a blocker or a genuine reason for the inactivity, include it in your response so that it can be considered.

Without a response, the current Backlog will proceed to a final inactivity warning.

Regards,
John Doe
Scrum Master
The Tech Residency Program"""

    return {
        "subject": subject,
        "body": body,
    }


# =========================================================
# Level 4: Final Warning
# Counter -3
# =========================================================

def build_final_warning_message(
    resident,
    backlog,
):
    first_name = (
        get_resident_first_name(
            resident
        )
    )

    subject = (
        f"Final Warning: Response Required — "
        f"{backlog['backlog_id']}"
    )

    body = f"""Hi {first_name},

This is the final inactivity warning for your active Backlog:

{backlog['backlog_id']} — {backlog['title']}

We have not received the required progress updates despite multiple check-ins and escalation messages.

Please reply before the next Scrum cycle with your current progress, blocker, reason for inactivity, or any other relevant update.

Any genuine response will allow us to understand your situation and continue the residency workflow appropriately.

If no response is received before the next Scrum cycle, the current Backlog will be closed due to inactivity and your residency status will be moved to INACTIVE.

Regards,
John Doe
Scrum Master
The Tech Residency Program"""

    return {
        "subject": subject,
        "body": body,
    }


# =========================================================
# Level 5: Backlog Closed
# Counter -4
# =========================================================

def build_inactivity_closure_message(
    resident,
    backlog,
):
    first_name = (
        get_resident_first_name(
            resident
        )
    )

    subject = (
        f"Backlog Closed Due to Inactivity — "
        f"{backlog['backlog_id']}"
    )

    body = f"""Hi {first_name},

Your current Backlog has been closed due to continued inactivity and the absence of a response to the previous Scrum check-ins.

Closed Backlog:

{backlog['backlog_id']} — {backlog['title']}

Your residency status has now been moved to INACTIVE.

This does not automatically mean that your participation in The Tech Residency Program has been terminated. If you are ready to resume your residency, please reply to this email.

Further instructions will be provided based on your residency status.

Regards,
John Doe
Scrum Master
The Tech Residency Program"""

    return {
        "subject": subject,
        "body": body,
    }


# =========================================================
# ACTIVE Scheduled Scrum Lifecycle
# =========================================================

def process_scheduled_active_resident(
    resident
):
    counter = resident["counter"]

    if counter is None:
        counter = 3

    work_context = (
        get_current_work_context(
            resident["resident_id"]
        )
    )

    if work_context is None:
        return {
            "resident_id":
                resident["resident_id"],
            "action":
                "failed",
            "reason":
                "active_assignment_not_found",
        }

    assignment = (
        work_context["assignment"]
    )

    backlog = (
        work_context["backlog"]
    )

    # -----------------------------------------------------
    # Counter -4 or lower
    #
    # End the active Backlog.
    # Assignment -> DISCARDED
    # Resident -> INACTIVE / 0
    # Send closure notification.
    # -----------------------------------------------------

    if counter <= -4:

        closure_message = (
            build_inactivity_closure_message(
                resident=resident,
                backlog=backlog,
            )
        )

        # Send closure email before changing state.
        sent_message = send_email(
            recipient=
                resident["email"],
            subject=
                closure_message["subject"],
            body=
                closure_message["body"],
        )

        sent_message_id = (
            sent_message.get("id")
        )

        if not sent_message_id:
            return {
                "resident_id":
                    resident["resident_id"],
                "assignment_id":
                    assignment["assignment_id"],
                "backlog_id":
                    backlog["backlog_id"],
                "action":
                    "failed",
                "reason":
                    "inactivity_closure_email_not_confirmed",
            }

        assignment_update = (
            update_assignment_status(
                sheet_row=
                    assignment["sheet_row"],
                status="DISCARDED",
            )
        )

        resident_update = (
            update_resident_state(
                sheet_row=
                    resident["sheet_row"],
                status="INACTIVE",
                counter=0,
            )
        )

        return {
            "resident_id":
                resident["resident_id"],
            "assignment_id":
                assignment["assignment_id"],
            "backlog_id":
                backlog["backlog_id"],
            "action":
                "resident_moved_inactive",
            "message_level":
                "closure",
            "sent_message_id":
                sent_message_id,
            "old_status":
                "ACTIVE",
            "new_status":
                "INACTIVE",
            "old_counter":
                counter,
            "new_counter":
                0,
            "assignment_status":
                "DISCARDED",
            "assignment_update":
                assignment_update,
            "resident_update":
                resident_update,
        }

    # -----------------------------------------------------
    # Counter 3 and 2
    # Normal Scrum
    # -----------------------------------------------------

    if counter >= 2:

        message = (
            build_normal_scrum_message(
                resident=resident,
                backlog=backlog,
                counter=counter,
            )
        )

        message_level = "normal"

    # -----------------------------------------------------
    # Counter 1 and 0
    # Concern
    # -----------------------------------------------------

    elif counter >= 0:

        message = (
            build_concern_scrum_message(
                resident=resident,
                backlog=backlog,
                counter=counter,
            )
        )

        message_level = "concern"

    # -----------------------------------------------------
    # Counter -1 and -2
    # Escalation
    # -----------------------------------------------------

    elif counter >= -2:

        message = (
            build_escalation_scrum_message(
                resident=resident,
                backlog=backlog,
                counter=counter,
            )
        )

        message_level = "escalation"

    # -----------------------------------------------------
    # Counter -3
    # Final Warning
    # -----------------------------------------------------

    else:

        message = (
            build_final_warning_message(
                resident=resident,
                backlog=backlog,
            )
        )

        message_level = (
            "final_warning"
        )

    # -----------------------------------------------------
    # Send selected Scrum message.
    # -----------------------------------------------------

    sent_message = send_email(
        recipient=
            resident["email"],
        subject=
            message["subject"],
        body=
            message["body"],
    )

    sent_message_id = (
        sent_message.get("id")
    )

    if not sent_message_id:
        return {
            "resident_id":
                resident["resident_id"],
            "assignment_id":
                assignment["assignment_id"],
            "backlog_id":
                backlog["backlog_id"],
            "action":
                "failed",
            "reason":
                "scrum_email_not_confirmed",
            "message_level":
                message_level,
        }

    # -----------------------------------------------------
    # Successful Scrum cycle:
    # decrement counter by one.
    # -----------------------------------------------------

    new_counter = (
        counter - 1
    )

    update_result = (
        update_resident_state(
            sheet_row=
                resident["sheet_row"],
            counter=new_counter,
        )
    )

    return {
        "resident_id":
            resident["resident_id"],
        "assignment_id":
            assignment["assignment_id"],
        "backlog_id":
            backlog["backlog_id"],
        "action":
            "scrum_message_sent",
        "message_level":
            message_level,
        "sent_message_id":
            sent_message_id,
        "old_counter":
            counter,
        "new_counter":
            new_counter,
        "sheet_update":
            update_result,
    }


def process_all_active_residents():
    residents = get_residents()

    active_residents = [
        resident
        for resident in residents
        if resident["status"] == "ACTIVE"
    ]

    results = []

    for resident in active_residents:
        try:
            result = (
                process_scheduled_active_resident(
                    resident
                )
            )

        except Exception as error:
            result = {
                "resident_id":
                    resident["resident_id"],
                "action":
                    "failed",
                "error":
                    str(error),
            }

        results.append(result)

    return {
        "active_resident_count":
            len(active_residents),
        "results":
            results,
    }


# =========================================================
# ACTIVE Resident Reply
#
# Static Scrum response for now.
#
# THIS is the future Scrum AI insertion point.
# =========================================================

def process_active_reply(
    resident,
    message,
):
    work_context = (
        get_current_work_context(
            resident["resident_id"]
        )
    )

    if work_context is None:
        return {
            "resident_id":
                resident["resident_id"],
            "action":
                "failed",
            "reason":
                "active_assignment_not_found",
        }

    assignment = (
        work_context["assignment"]
    )

    backlog = (
        work_context["backlog"]
    )

    first_name = (
        resident["name"]
        .strip()
        .split()[0]
        if resident["name"].strip()
        else "Resident"
    )

    body = f"""Hi {first_name},

Thanks for the update on {backlog['backlog_id']} — {backlog['title']}.

I've noted your progress. Please continue with the Backlog and keep committing your work regularly to GitHub.

If you run into a blocker or need clarification, reply to this thread with the details and we'll work through it.

When the Backlog is complete, send the GitHub repository link in your reply for submission.

Regards,
John Doe
Scrum Master
The Tech Residency Program"""

    sent_message = (
        send_threaded_reply(
            message=message,
            body=body,
        )
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
                "scrum_reply_not_confirmed",
        }

    update_result = (
        update_resident_state(
            sheet_row=
                resident["sheet_row"],
        )
    )

    return {
        "resident_id":
            resident["resident_id"],
        "assignment_id":
            assignment["assignment_id"],
        "backlog_id":
            backlog["backlog_id"],
        "action":
            "scrum_reply_sent",
        "sent_message_id":
            sent_message_id,
        "status":
            "ACTIVE",
        "counter_reset_to":
            3,
        "sheet_update":
            update_result,
    }


# =========================================================
# Gmail Metadata Helpers
# =========================================================

def get_message_metadata(
    message_id
):
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
                "Message-ID",
            ],
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

    sender_name, sender_email = (
        parseaddr(
            headers.get(
                "from",
                "",
            )
        )
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
            headers.get(
                "message-id",
                "",
            ),
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
        errors="replace",
    )


def extract_plain_text_from_payload(
    payload
):
    mime_type = payload.get(
        "mimeType",
        "",
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
        [],
    )

    # Prefer plain text.
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

    # Search nested multipart content.
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
            html_content,
        )

        text = html.unescape(text)

        text = re.sub(
            r"\s+",
            " ",
            text,
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


def get_full_authorized_message(
    message_id
):
    gmail = get_gmail_service()

    message = (
        gmail.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="full",
        )
        .execute()
    )

    payload = message.get(
        "payload",
        {},
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
                "",
            ),
    }


# =========================================================
# Unauthorized Sender Reply
# =========================================================

def send_decline_reply(message):
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

    # Use threaded helper while preserving
    # the decline-specific subject.
    gmail = get_gmail_service()

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

    email_message.set_content(body)

    encoded_message = (
        base64.urlsafe_b64encode(
            email_message.as_bytes()
        )
        .decode()
    )

    return (
        gmail.users()
        .messages()
        .send(
            userId="me",
            body={
                "raw":
                    encoded_message,
                "threadId":
                    message["thread_id"],
            },
        )
        .execute()
    )


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
                pageToken=page_token,
            )
        )

        result = request.execute()

        for history_record in (
            result.get(
                "history",
                [],
            )
        ):
            for added in (
                history_record.get(
                    "messagesAdded",
                    [],
                )
            ):
                message = (
                    added.get(
                        "message",
                        {},
                    )
                )

                message_id = (
                    message.get("id")
                )

                label_ids = (
                    message.get(
                        "labelIds",
                        [],
                    )
                )

                if (
                    message_id
                    and
                    "INBOX" in label_ids
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

    return list(message_ids)


# =========================================================
# Process Exact Gmail Message
#
# This is the inbound state router.
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
                "duplicate_ignored",
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
                "non_inbox_ignored",
        }

    authorization = (
        authorize_sender(
            message["sender_email"]
        )
    )

    # -----------------------------------------------------
    # Unknown sender.
    # Do not retrieve body.
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
                sent_message.get("id"),
        }

    # -----------------------------------------------------
    # Registered resident.
    # Retrieve message body.
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
            full_message["snippet"],
    }

    resident = {
        "resident_id":
            authorization["resident_id"],
        "name":
            authorization["name"],
        "email":
            authorization["email"],
        "status":
            authorization["status"],
        "counter":
            authorization["counter"],
        "mentor":
            authorization["mentor"],
        "sheet_row":
            authorization["sheet_row"],
    }

    # -----------------------------------------------------
    # PENDING -> READY
    # -----------------------------------------------------

    if authorization["status"] == "PENDING":
        onboarding_result = (
            process_pending_reply(
                resident=resident,
                message=message,
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
            "resident_id":
                authorization["resident_id"],
            "resident_name":
                authorization["name"],
            "action":
                "pending_reply_processed",
            "onboarding":
                onboarding_result,
        }

    # -----------------------------------------------------
    # ACTIVE -> Scrum interaction
    # -----------------------------------------------------

    if authorization["status"] == "ACTIVE":
        scrum_result = (
            process_active_reply(
                resident=resident,
                message=message,
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
            "resident_id":
                authorization["resident_id"],
            "resident_name":
                authorization["name"],
            "action":
                "active_reply_processed",
            "scrum":
                scrum_result,
        }

    # -----------------------------------------------------
    # Other registered statuses.
    #
    # Later:
    # READY, SUBMITTED, INACTIVE, DEAD
    # receive their own inbound routing rules.
    # -----------------------------------------------------

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
            "registered_resident_email_extracted",
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
            "running",
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
        "cv": code_verifier
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
                "OAuth state is missing.",
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
                "Invalid OAuth state.",
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

    credentials = (
        flow.credentials
    )

    return {
        "status":
            "authorization_successful",
        "refresh_token_received":
            bool(
                credentials.refresh_token
            ),
        "message":
            "Google authorization "
            "completed successfully.",
    }


# =========================================================
# Connectivity / Development Endpoints
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
            ),
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
            residents,
    }


# =========================================================
# TEST: Process JOINED Residents
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
            **result,
        }

    except HttpError as error:
        return {
            "status":
                "google_api_error",
            "error":
                str(error),
        }

    except Exception as error:
        return {
            "status":
                "processing_error",
            "error":
                str(error),
        }


# =========================================================
# TEST: Process READY Residents
# =========================================================

@app.get("/test/process-ready")
def test_process_ready():
    try:
        result = (
            process_all_ready_residents()
        )

        return {
            "status":
                "ready_processing_complete",
            **result,
        }

    except HttpError as error:
        return {
            "status":
                "google_api_error",
            "error":
                str(error),
        }

    except Exception as error:
        return {
            "status":
                "processing_error",
            "error":
                str(error),
        }


# =========================================================
# TEST: Scheduled ACTIVE Scrum Run
#
# Later this is scheduled automatically for 10:30 AM.
# =========================================================

@app.get("/test/process-active")
def test_process_active():
    try:
        result = (
            process_all_active_residents()
        )

        return {
            "status":
                "active_processing_complete",
            **result,
        }

    except HttpError as error:
        return {
            "status":
                "google_api_error",
            "error":
                str(error),
        }

    except Exception as error:
        return {
            "status":
                "processing_error",
            "error":
                str(error),
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
                {},
            )
        )

        encoded_data = (
            pubsub_message.get(
                "data",
                "",
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
                [],
        }

        # -------------------------------------------------
        # Missing Gmail history ID
        # -------------------------------------------------

        if not incoming_history_id:
            latest_gmail_notification[
                "status"
            ] = "missing_history_id"

            return {
                "status":
                    "notification_received"
            }

        # -------------------------------------------------
        # Establish baseline after restart
        #
        # Temporary pre-pilot behavior.
        # Later the cursor will be persisted.
        # -------------------------------------------------

        if last_history_id is None:
            last_history_id = (
                incoming_history_id
            )

            latest_gmail_notification[
                "status"
            ] = (
                "history_baseline_initialized"
            )

            latest_gmail_notification[
                "new_last_history_id"
            ] = last_history_id

            return {
                "status":
                    "notification_received"
            }

        # -------------------------------------------------
        # Ignore stale / duplicate Pub/Sub notifications.
        #
        # Gmail / Pub/Sub notifications may arrive late
        # or out of order.
        #
        # The history cursor must NEVER move backwards.
        # -------------------------------------------------

        if (
            int(incoming_history_id)
            <= int(last_history_id)
        ):
            latest_gmail_notification[
                "status"
            ] = (
                "stale_notification_ignored"
            )

            latest_gmail_notification[
                "current_history_id"
            ] = last_history_id

            return {
                "status":
                    "notification_received",
                "action":
                    "stale_notification_ignored",
            }

        # -------------------------------------------------
        # Retrieve all new INBOX messages since the
        # previously processed history cursor.
        # -------------------------------------------------

        previous_history_id = (
            last_history_id
        )

        message_ids = (
            get_new_message_ids(
                previous_history_id
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

        # -------------------------------------------------
        # Advance cursor only after message processing.
        #
        # Because incoming_history_id has already been
        # verified as greater than last_history_id,
        # the cursor can only move forward.
        # -------------------------------------------------

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
                str(error),
            "last_history_id":
                last_history_id,
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
                str(error),
            "last_history_id":
                last_history_id,
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
            latest_gmail_notification,
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
            latest_authorized_email,
    }


@app.get("/test/runtime-state")
def get_runtime_state():
    return {
        "last_history_id":
            last_history_id,
        "processed_message_count":
            len(
                processed_message_ids
            ),
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
            "INCLUDE",
    }

    result = (
        gmail.users()
        .watch(
            userId="me",
            body=request_body,
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
            True,
    }
