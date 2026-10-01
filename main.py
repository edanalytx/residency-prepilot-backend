import os

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

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    return RedirectResponse(authorization_url)


@app.get("/oauth2/callback")
def oauth_callback(request: Request):
    flow = create_flow()

    flow.fetch_token(
        authorization_response=str(request.url)
    )

    credentials = flow.credentials

    return {
        "status": "authorization_successful",
        "refresh_token_received": bool(credentials.refresh_token),
        "message": "Google authorization completed successfully."
    }
