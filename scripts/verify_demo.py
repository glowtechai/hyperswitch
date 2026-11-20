#!/usr/bin/env python3
"""Verify an ephemeral, local Hyperswitch v1 demo using only Python's stdlib.

Creates/reuses the repository's public demo account and creates one synthetic
record in the embedded dummy simulator. It does not call the merchant payments
API. All direct HTTP requests must use loopback; proxies and redirects are
disabled. The router's /health/ready implementation additionally performs a
credential-free GET to https://api.stripe.com/healthcheck.

Run after the launcher has waited for services: python3 scripts/verify_demo.py
Evidence contains only selected checks/identifiers, never raw auth responses.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


DEMO_EMAIL = "demo@hyperswitch.com"
DEMO_PASSWORD = "Hyperswitch@123"  # Public fixture from create_default_user.sh.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
EVIDENCE_PATH = Path(__file__).resolve().parents[1] / ".run-evidence/verify-demo.json"


class CheckFailure(Exception):
    """Only fixed, credential-free diagnostic messages belong here."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def loopback_url(url: str, *, base: bool = False) -> str:
    """Reject remote URLs, credentials, fragments, and proxy/DNS surprises."""
    try:
        parts = urlsplit(url)
        require(parts.scheme == "http", "Only local HTTP URLs are supported")
        require(parts.username is None and parts.password is None, "URL credentials are forbidden")
        require(not parts.fragment, "URL fragments are forbidden")
        require(parts.hostname is not None, "A loopback hostname is required")
        port = parts.port or 80
        require(0 < port < 65536, "Invalid local port")
        if parts.hostname != "localhost":
            require(ipaddress.ip_address(parts.hostname).is_loopback, "Remote hosts are forbidden")
        addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
        require(bool(addresses), "Loopback hostname did not resolve")
        require(
            all(ipaddress.ip_address(item[4][0]).is_loopback for item in addresses),
            "Hostname resolved outside loopback",
        )
        if base:
            require(parts.path in ("", "/") and not parts.query, "Base URLs must contain only an origin")
        return url.rstrip("/") if base else url
    except (ValueError, OSError) as exc:
        raise CheckFailure("Invalid or unresolvable loopback URL") from exc


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CheckFailure("HTTP redirect refused; direct local responses are required")


class LocalClient:
    def __init__(self, timeout: float):
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def request(self, url, *, method="GET", payload=None, token=None, dummy_host=False):
        loopback_url(url)
        headers = {"Accept": "*/*", "User-Agent": "Hyperswitch-Local-Smoke/1.0"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        if token:
            headers["Authorization"] = "Bearer " + token
        if dummy_host:
            # The reviewed simulator route has a Host("localhost") guard.
            # This request's actual network destination is still loopback.
            headers["Host"] = "localhost:" + str(urlsplit(url).port or 80)
        req = Request(url, data=data, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(req, timeout=self.timeout)
            except HTTPError as exc:
                response = exc
            with response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                require(len(body) <= MAX_RESPONSE_BYTES, "Local response exceeded size limit")
                return response.status, response.headers, body
        except (URLError, OSError, TimeoutError) as exc:
            raise CheckFailure("Local HTTP connection failed or timed out") from exc

    def json(self, url, **kwargs):
        status, headers, body = self.request(url, **kwargs)
        require(200 <= status < 300, f"Local endpoint returned HTTP {status}")
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise CheckFailure("Local endpoint did not return valid JSON") from exc
        require(isinstance(value, dict), "Local endpoint did not return a JSON object")
        require(not value.get("error"), "Local endpoint returned an API error")
        return value


def identifier(value, label: str) -> str:
    require(
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None,
        f"Missing or invalid {label} identifier",
    )
    return value


def api_health(client, api):
    status, headers, body = client.request(api + "/health")
    require(status == 200 and body.strip() == b"health is good", "API liveness check failed")
    version = headers.get("x-hyperswitch-version", "")
    result = {"http_status": status, "expected_body": True}
    if re.fullmatch(r"[A-Za-z0-9._+-]{1,160}", version):
        result["runtime_version"] = version
    return result


def api_readiness(client, api):
    value = client.json(api + "/health/ready")
    for component in ("database", "redis", "outgoing_request"):
        require(value.get(component) is True, f"Readiness component failed: {component}")
    if "analytics" in value:
        require(value["analytics"] is True, "Readiness component failed: analytics")
    # Some optional components report false when NotApplicable; do not assert
    # that every optional field is true, or include arbitrary server output.
    fields = ("database", "redis", "analytics", "outgoing_request", "vault", "opensearch",
              "unified_connector_service", "decision_engine")
    return {
        "components": {key: value[key] for key in fields if key in value
                       and (isinstance(value[key], bool) or value[key] is None)},
        "router_performs_external_credential_free_health_probe": True,
    }


def dashboard_check(client, dashboard):
    status, headers, body = client.request(dashboard + "/")
    lower = body.lower()
    require(status == 200 and b"<html" in lower and b"<script" in lower,
            "Dashboard did not serve an HTML application document")
    return {"http_status": status, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            "html_with_script_tag": True, "browser_execution_tested": False}


def sdk_check(client, sdk):
    status, headers, body = client.request(sdk + "/HyperLoader.js")
    content_type = headers.get("Content-Type", "").lower()
    is_script = ("javascript" in content_type or "ecmascript" in content_type
                 or b"function" in body[:4096] or b"Hyper" in body[:4096])
    require(status == 200 and len(body) > 64 and b"<html" not in body[:512].lower() and is_script,
            "SDK loader did not serve JavaScript")
    return {"http_status": status, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            "browser_execution_tested": False}


def demo_account(client, api):
    payload = {"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
    status, _, body = client.request(api + "/user/signin", method="POST", payload=payload)
    created = False
    if status in (400, 401):
        # Never reset an existing account or attempt another identity.
        client.json(api + "/user/signup", method="POST", payload=payload)
        created = True
    else:
        require(200 <= status < 300, f"Demo sign-in returned HTTP {status}")
    # Independently sign in after creation; do not trust a signup banner/token.
    auth = client.json(api + "/user/signin", method="POST", payload=payload)
    token = auth.get("token")
    require(isinstance(token, str) and bool(token), "Demo sign-in did not return a token")
    skipped_optional_totp = False
    if auth.get("token_type") == "totp":
        auth = client.json(api + "/user/2fa/terminate?skip_two_factor_auth=true", token=token)
        token = auth.get("token")
        require(isinstance(token, str) and bool(token), "Local demo authentication did not complete")
        skipped_optional_totp = True
    require(auth.get("token_type") == "user_info", "Unsupported demo authentication stage")
    user = client.json(api + "/user", token=token)
    require(user.get("email", "").lower() == DEMO_EMAIL, "Authenticated user is not the demo fixture")
    merchant_id = identifier(user.get("merchant_id"), "merchant")
    profile_id = identifier(user.get("profile_id"), "profile")
    merchant = client.json(api + "/accounts/" + merchant_id, token=token)
    require(merchant.get("merchant_id") == merchant_id, "Merchant retrieval did not match demo user")
    profile = client.json(api + "/account/" + merchant_id + "/business_profile/" + profile_id, token=token)
    require(profile.get("profile_id") == profile_id, "Profile retrieval did not match demo user")
    if "merchant_id" in profile:
        require(profile["merchant_id"] == merchant_id, "Profile belongs to a different merchant")
    return {"fixture": "repository_public_demo", "created": created, "signin_verified": True,
            "authenticated_user_verified": True, "merchant_retrieved": True, "profile_retrieved": True,
            "merchant_id": merchant_id, "profile_id": profile_id,
            "optional_local_totp_step_skipped": skipped_optional_totp}


def dummy_payment(client, api):
    # This exact test number has a deterministic non-3DS success branch in
    # routes/dummy_connector/utils.rs. It is never sent to /payments or a PSP.
    payload = {
        "amount": 100, "currency": "USD", "connector": "paypal_test",
        "return_url": api + "/health",
        "payment_method_data": {"card": {"name": "Local Demo", "number": "4242424242424242",
                                        "expiry_month": "12", "expiry_year": "2035", "cvc": "123"}},
    }
    created = client.json(api + "/dummy-connector/payment", method="POST", payload=payload, dummy_host=True)
    payment_id = identifier(created.get("id"), "synthetic payment")
    retrieved = client.json(api + "/dummy-connector/payments/" + payment_id, dummy_host=True)
    for result in (created, retrieved):
        require(result.get("id") == payment_id and result.get("status") == "succeeded"
                and result.get("amount") == 100 and result.get("currency") == "USD"
                and result.get("payment_method_type") == "card" and result.get("next_action") is None,
                "Embedded dummy payment did not match the expected successful result")
    return {"scope": "embedded_simulator_and_redis", "synthetic_payment_id": payment_id,
            "status_on_create_and_retrieve": "succeeded", "synthetic_amount_minor": 100,
            "currency": "USD", "external_payment_processor_contacted": False,
            "merchant_payment_api_tested": False}


def save_evidence(evidence):
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=EVIDENCE_PATH.parent,
                                     prefix=".verify-demo-", delete=False) as handle:
        temporary = handle.name
        json.dump(evidence, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, EVIDENCE_PATH)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://localhost:8080")
    parser.add_argument("--dashboard-url", default="http://localhost:9000")
    parser.add_argument("--sdk-url", default="http://localhost:9050")
    parser.add_argument("--timeout", type=float, default=15, help="Per-request timeout in seconds (0 < value <= 30)")
    args = parser.parse_args()
    evidence = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(), "checks": [],
        "limitations": [
            "The router readiness endpoint performs a credential-free Stripe health probe; direct verifier requests stay on loopback.",
            "Dashboard/SDK HTTP delivery is checked; browser execution, UI login, and checkout rendering are not tested.",
            "The dummy payment checks only the embedded simulator and Redis, not merchant /payments orchestration, routing, or settlement.",
            "The public demo account and synthetic simulator record persist until the ephemeral runner's local data is removed.",
            "Runtime image equivalence to the fork's source commit is not established by this verifier.",
        ],
    }
    try:
        require(0 < args.timeout <= 30, "Timeout must be greater than zero and at most 30 seconds")
        api, dashboard, sdk = (loopback_url(url, base=True) for url in
                               (args.api_url, args.dashboard_url, args.sdk_url))
        client = LocalClient(args.timeout)
        evidence["endpoints"] = {"api": api, "dashboard": dashboard, "sdk": sdk}
        checks = (
            ("api_liveness", lambda: api_health(client, api)),
            ("api_readiness", lambda: api_readiness(client, api)),
            ("dashboard_http", lambda: dashboard_check(client, dashboard)),
            ("sdk_http", lambda: sdk_check(client, sdk)),
            ("demo_account_and_profile", lambda: demo_account(client, api)),
            ("embedded_dummy_payment", lambda: dummy_payment(client, api)),
        )
        for name, check in checks:
            started = time.monotonic()
            result = {"name": name, "required": True}
            try:
                result.update(status="passed", details=check())
            except CheckFailure as exc:
                result.update(status="failed", reason=str(exc))
            except Exception:
                # Never expose raw HTTP bodies, exception reprs, or auth data.
                result.update(status="failed", reason="Unexpected verifier error; response content withheld")
            result["duration_seconds"] = round(time.monotonic() - started, 3)
            evidence["checks"].append(result)
            print(f"{result['status'].upper()}: {name}", flush=True)
    except CheckFailure as exc:
        evidence["checks"].append({"name": "loopback_configuration", "required": True,
                                   "status": "failed", "reason": str(exc)})
    evidence["status"] = "passed" if all(item["status"] == "passed" for item in evidence["checks"]) else "failed"
    evidence["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    try:
        save_evidence(evidence)
    except OSError:
        print("FAILED: could not write local verification evidence", flush=True)
        return 1
    print(json.dumps({"status": evidence["status"], "evidence": str(EVIDENCE_PATH),
                      "passed": sum(item["status"] == "passed" for item in evidence["checks"]),
                      "failed": sum(item["status"] == "failed" for item in evidence["checks"])}), flush=True)
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
