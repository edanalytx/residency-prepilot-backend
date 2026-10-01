from fastapi import FastAPI

app = FastAPI(title="Residency Pre-Pilot Backend")


@app.get("/")
def home():
    return {"service": "Residency Pre-Pilot Backend", "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/oauth2/callback")
def oauth_callback():
    return {"status": "callback_received"}
