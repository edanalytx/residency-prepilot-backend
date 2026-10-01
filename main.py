import os
import secrets
import hashlib
import base64
import json

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow


app = FastAPI(title="Residency Pre-Pilot Backend")

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REDIRECT_URI = os.environ["GOOGLE_REDIRECT_URI"]

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify"
]


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


@app.get("/")
def home():
    return {
        "service": "Residency Pre-Pilot Backend",
        "status": "running"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/auth/google")
def google_auth():
    flow = create_flow()

    # Generate PKCE code verifier
    code_verifier = secrets.token_urlsafe(64)

    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = (
        base64.urlsafe_b64encode(digest)
        .decode()
        .rstrip("=")
    )

    # Carry the verifier through the OAuth round trip
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
            base64.urlsafe_b64decode(padded_state).decode()
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
        "refresh_token_received": bool(credentials.refresh_token),
        "message": "Google authorization completed successfully."
    }
