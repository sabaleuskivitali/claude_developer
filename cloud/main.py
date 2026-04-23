import os, uuid, json, logging
from pathlib import Path
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

log = logging.getLogger("uvicorn.error")
app = FastAPI()

API_KEY = os.environ["CLOUD_API_KEY"]
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
PROFILES_DIR = Path("/app/profiles")
PROFILES_DIR.mkdir(exist_ok=True)


class SignedBootstrapProfile(BaseModel):
    signed_data: str
    signature: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/upload")
def upload(profile: SignedBootstrapProfile, x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = str(uuid.uuid4())
    (PROFILES_DIR / f"{token}.json").write_text(profile.model_dump_json())
    url = f"{PUBLIC_URL}/p/{token}"
    log.info("Profile uploaded → %s", url)
    return {"url": url}


@app.get("/p/{token}")
def download(token: str):
    # Sanitize token — only UUID chars allowed
    import re
    if not re.fullmatch(r'[0-9a-f-]{36}', token):
        raise HTTPException(status_code=400, detail="Invalid token")
    path = PROFILES_DIR / f"{token}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse(content=json.loads(path.read_text()))
