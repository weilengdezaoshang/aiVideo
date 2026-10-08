"""Loopback-only OIDC provider for the local development environment.

Started by scripts/dev_stack.py on https://localhost:8444. It auto-approves the
API's authorization request (PKCE S256, client `dev-browser`) and signs a
short-lived RS256 id_token, so the production login flow runs end to end with
zero external identity infrastructure. Never expose it beyond loopback.
"""
import base64
import hashlib
import os
import secrets
import time
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from joserfc import jwt
from joserfc.jwk import RSAKey

app = FastAPI()
API_PORT = os.environ.get("AIVERO_DEV_API_PORT", "8443")
PORT = os.environ.get("AIVERO_DEV_OIDC_PORT", "8444")
CLIENT_ID = os.environ.get("AIVERO_DEV_OIDC_CLIENT_ID", "dev-browser")
CLIENT_SECRET = os.environ.get("AIVERO_DEV_OIDC_CLIENT_SECRET", "dev-browser-secret")
issuer = f"https://localhost:{PORT}"
callback = f"https://localhost:{API_PORT}/auth/callback"
key = RSAKey.generate_key(2048, parameters={"kid": "aivideo-dev"})
codes: dict[str, dict] = {}


@app.get("/.well-known/openid-configuration")
def metadata():
    return {"issuer": issuer, "authorization_endpoint": issuer + "/authorize",
            "token_endpoint": issuer + "/token", "jwks_uri": issuer + "/jwks",
            "response_types_supported": ["code"], "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic"]}


@app.get("/jwks")
def jwks():
    return {"keys": [key.as_dict(private=False)]}


@app.get("/authorize")
def authorize(request: Request):
    values = dict(request.query_params)
    if (values.get("client_id") != CLIENT_ID or values.get("redirect_uri") != callback
            or values.get("code_challenge_method") != "S256" or not values.get("nonce")):
        raise HTTPException(400, "Invalid development client request")
    code = secrets.token_urlsafe(32)
    codes[code] = {**values, "issued": time.time()}
    return RedirectResponse(callback + "?" + urlencode({"code": code, "state": values["state"]}))


@app.post("/token")
async def token(request: Request):
    expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    if request.headers.get("authorization") != expected:
        raise HTTPException(401, "Invalid development client secret")
    values = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    original = codes.pop(values.get("code", ""), None)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(values.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
    if (not original or time.time() - original["issued"] > 60
            or challenge != original["code_challenge"] or values.get("redirect_uri") != callback):
        raise HTTPException(400, "Invalid code or PKCE verifier")
    signed = jwt.encode({"alg": "RS256", "kid": "aivideo-dev"}, {
        "iss": issuer, "sub": "dev-user", "aud": CLIENT_ID,
        "iat": int(time.time()), "exp": int(time.time()) + 300, "nonce": original["nonce"]}, key)
    return {"access_token": "dev-only", "token_type": "Bearer", "id_token": signed}
