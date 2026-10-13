#!/usr/bin/env python3
"""Reconcile an MLIS registry, packaged model and deployment with model.json.

Runs as an Argo CD PostSync hook inside the AIE project namespace.
Standard library only, so any python:3 image works.
"""
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

SA_DIR = os.environ.get("SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount")
POLL_SECONDS = int(os.environ.get("MLIS_POLL_SECONDS", "15"))


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class ApiError(Exception):
    pass


def call(method, url, token, body=None, ctx=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:400]
        raise ApiError(f"{method} {url} -> HTTP {err.code}: {detail}") from None
    return json.loads(raw) if raw.strip() else None


def kube_url(path):
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}:{port}{path}"


def read_aie_token(ref):
    """AIE keeps a short-lived access token of the user in that user's namespace."""
    with open(os.path.join(SA_DIR, "token")) as f:
        sa_token = f.read().strip()
    ctx = ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))
    secret = call("GET", kube_url(f"/api/v1/namespaces/{ref['namespace']}/secrets/{ref['name']}"),
                  sa_token, ctx=ctx)
    value = (secret.get("data") or {}).get(ref.get("key", "AUTH_TOKEN"), "")
    token = base64.b64decode(value).decode().strip() if value else ""
    if not token:
        raise SystemExit("The AIE access token is empty: sign in once as the technical user.")
    return token


class Mlis:
    def __init__(self, url, token, project):
        self.base = url.rstrip("/") + "/api/v1"
        self.token = token
        self.project = project
        cafile = os.environ.get("MLIS_CA_FILE") or os.environ.get("EZUA_DOMAIN_CA_CERT_PATH")
        self.ctx = ssl.create_default_context(cafile=cafile or None)

    def req(self, method, path, body=None):
        return call(method, self.base + path, self.token, body, self.ctx)

    def find(self, kind, name):
        for item in self.req("GET", f"/{kind}") or []:
            if item.get("name") == name and item.get("project", "") in ("", self.project):
                return item
        return None


def differs(desired, actual):
    """True if any desired field differs; fields MLIS adds on its own are ignored."""
    if isinstance(desired, dict):
        actual = actual if isinstance(actual, dict) else {}
        return any(differs(v, actual.get(k)) for k, v in desired.items())
    if isinstance(desired, list):
        return list(desired) != list(actual or [])
    if desired in ("", None):
        return actual not in ("", None)
    return str(desired) != str(actual)


def wait_status(api, name, wanted, timeout):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        dep = api.find("deployments", name)
        status = dep.get("status") if dep else "Absent"
        if status != last:
            log(f"deployment {name}: {status}")
            last = status
        if status in wanted:
            return dep
        if status in ("Failed", "UpdateFailed"):
            info = (dep.get("state") or {}).get("failureInfo")
            raise SystemExit(f"deployment {name} is {status}: {info}")
        time.sleep(POLL_SECONDS)
    raise SystemExit(f"deployment {name} did not reach {sorted(wanted)} within {timeout}s (last: {last})")


def ensure_registry(api, spec):
    current = api.find("registries", spec["name"])
    body = {k: v for k, v in spec.items() if k != "name"}
    body.update(name=spec["name"], project=api.project)
    secret = os.environ.get("HF_TOKEN", "")
    if current is None:
        if not secret:
            raise SystemExit("Secret hf-token (key token) is missing in the project namespace.")
        log(f"create registry {spec['name']}")
        return api.req("POST", "/registries", dict(body, secretKey=secret))
    if differs({k: v for k, v in body.items() if k != "project"}, current):
        if not secret:
            raise SystemExit("Registry differs but HF_TOKEN is not available to update it.")
        log(f"update registry {spec['name']}")
        return api.req("PUT", f"/registries/{current['id']}", dict(body, secretKey=secret))
    log(f"registry {spec['name']} unchanged")
    return current


def ensure_model(api, spec, registry_id):
    current = api.find("models", spec["name"])
    body = dict(spec, project=api.project, registry=registry_id)
    if current is None:
        log(f"create packaged model {spec['name']}")
        return api.req("POST", "/models", body), True
    compare = {k: v for k, v in body.items() if k not in ("name", "project")}
    if differs(compare, current):
        log(f"update packaged model {spec['name']} (MLIS creates a new version)")
        return api.req("PUT", f"/models/{current['id']}", body), True
    log(f"packaged model {spec['name']} unchanged (version {current.get('version')})")
    return current, False


def ensure_deployment(api, spec, model, model_changed, strategy, timeout):
    current = api.find("deployments", spec["name"])
    goal = spec.get("goalStatus", "Ready")
    body = dict(spec, model=model["name"], project=api.project, goalStatus=goal)
    if current is None:
        log(f"create deployment {spec['name']} in namespace {spec['namespace']}")
        api.req("POST", "/deployments", body)
    else:
        if current.get("namespace") != spec["namespace"]:
            raise SystemExit("The namespace of an existing deployment cannot change; set state absent first.")
        compare = {k: v for k, v in body.items() if k not in ("name", "namespace", "project", "model")}
        # Also catches a model version left over by an interrupted earlier run.
        model_changed = model_changed or current.get("model") not in (model.get("id"), model["name"])
        if not model_changed and not differs(compare, current):
            log(f"deployment {spec['name']} unchanged")
            return wait_status(api, spec["name"], {goal}, timeout)
        update = {k: v for k, v in body.items() if k not in ("name", "namespace")}
        update["canaryTrafficPercent"] = 100
        # MLIS only switches the model of a paused deployment; pausing also frees the only GPU.
        must_pause = model_changed or (strategy == "pause-edit-resume" and goal == "Ready")
        if must_pause and current.get("status") != "Paused":
            log(f"pause deployment {spec['name']} before the update")
            pause = {k: current[k] for k in ("model", "security", "autoScaling", "environment", "arguments")
                     if current.get(k) is not None}
            api.req("PUT", f"/deployments/{current['id']}", dict(pause, goalStatus="Paused"))
            wait_status(api, spec["name"], {"Paused"}, 900)
        log(f"update deployment {spec['name']}")
        api.req("PUT", f"/deployments/{current['id']}", update)
    return wait_status(api, spec["name"], {goal}, timeout)


def remove_all(api, cfg):
    dep = api.find("deployments", cfg["deployment"]["name"])
    if dep:
        log(f"delete deployment {dep['name']}")
        api.req("DELETE", f"/deployments/{dep['id']}")
        wait_status(api, dep["name"], {"Absent"}, 900)
    for _ in range(20):
        model = api.find("models", cfg["model"]["name"])
        if not model:
            break
        log(f"delete packaged model {model['name']} version {model.get('version')}")
        api.req("DELETE", f"/models/{model['id']}")
    reg = api.find("registries", cfg["registry"]["name"])
    if reg:
        log(f"delete registry {reg['name']}")
        api.req("DELETE", f"/registries/{reg['id']}")


def main(path):
    with open(path) as f:
        cfg = json.load(f)
    api = Mlis(cfg["mlisUrl"], read_aie_token(cfg["tokenSecret"]), cfg["project"])
    log(f"MLIS {cfg['mlisUrl']}, project {cfg['project']}, desired state {cfg.get('state', 'present')}")
    if cfg.get("state", "present") == "absent":
        remove_all(api, cfg)
        log("done: all MLIS objects removed")
        return
    registry = ensure_registry(api, cfg["registry"])
    model, changed = ensure_model(api, cfg["model"], registry["id"])
    dep = ensure_deployment(api, cfg["deployment"], model, changed,
                            cfg.get("updateStrategy", "pause-edit-resume"),
                            int(cfg.get("waitTimeoutSeconds", 3600)))
    log(f"done: {dep['name']} is {dep.get('status')}, endpoint {(dep.get('state') or {}).get('endpoint')}")


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "/config/model.json")
    except ApiError as err:
        log(f"ERROR {err}")
        sys.exit(1)
