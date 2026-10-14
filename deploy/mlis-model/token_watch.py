#!/usr/bin/env python3
"""Daily check of the tokens this setup depends on. Exits 1 when action is needed.

Runs as the CronJob token-watch; Argo CD shows the application Degraded after a failed run.
"""
import json
import os
import sys
import time

from mlis_sync import AieToken, ApiError, Mlis, call, jwt_claims, log, tls_context, utc

MIN_DAYS = int(os.environ.get("MIN_DAYS", "30"))
API_TOKEN_FILE = os.environ.get("API_TOKEN_FILE", "/model-api/token")

problems = []


def problem(message):
    log("FAIL " + message)
    problems.append(message)


def check_aie_token(cfg):
    """Returns the endpoint of the MLIS deployment, or None."""
    name = cfg["deployment"]["name"]
    try:
        token = AieToken(cfg["tokenSecret"])
        exp = jwt_claims(token.get()).get("exp", 0)
        log(f"OK   AIE token of the technical user is renewed, valid until {utc(exp)}")
        dep = Mlis(cfg["mlisUrl"], token, cfg["project"]).find("deployments", name)
    except (SystemExit, ApiError) as err:
        problem(f"AIE token or MLIS: {err}")
        return None
    if dep is None:
        log(f"INFO MLIS deployment {name} does not exist")
        return None
    log(f"OK   MLIS accepts the AIE token; deployment {name} is {dep.get('status')}")
    return ((dep.get("state") or {}).get("endpoint") or "").rstrip("/") or None


def check_api_token(endpoint):
    if not os.path.exists(API_TOKEN_FILE):
        log("INFO no endpoint API token mounted (Secret model-api-token)")
        return
    with open(API_TOKEN_FILE) as f:
        token = f.read().strip()
    try:
        exp = jwt_claims(token).get("exp")
    except (ValueError, IndexError):
        exp = None
    if exp is None:
        log("INFO expiry of the endpoint API token unknown")
    else:
        days = (exp - time.time()) / 86400
        if days < MIN_DAYS:
            problem(f"endpoint API token expires {utc(exp)}, in {days:.0f} days (limit {MIN_DAYS}): "
                    "an administrator generates a new one, then replace the Secret model-api-token")
        else:
            log(f"OK   endpoint API token valid until {utc(exp)}, {days:.0f} days")
    if endpoint:
        cafile = os.environ.get("EZUA_DOMAIN_CA_CERT_PATH")
        try:
            call("GET", endpoint + "/v1/models", token, ctx=tls_context(cafile or None))
            log(f"OK   endpoint {endpoint} accepts the API token")
        except ApiError as err:
            problem(f"endpoint rejects the API token: {err}")


def main(path):
    with open(path) as f:
        cfg = json.load(f)
    check_api_token(check_aie_token(cfg))
    if problems:
        log(f"{len(problems)} problem(s): see the FAIL lines above")
        sys.exit(1)
    log("all tokens OK")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/config/model.json")
