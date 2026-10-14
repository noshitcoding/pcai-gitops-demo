"""Model access of the chat front end, without the web UI.

The endpoint comes from the KServe InferenceService that MLIS creates in this namespace,
read with the pod's own ServiceAccount; inference uses the endpoint API token. No AIE user
token is needed, so the app keeps working whatever happens to the technical user.
"""
import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.request

SA_DIR = os.environ.get("SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount")
DEPLOYMENT = os.environ.get("MLIS_DEPLOYMENT", "")
MODEL_ENDPOINT = os.environ.get("MODEL_ENDPOINT", "").rstrip("/")
MODEL_TOKEN_FILE = os.environ.get("MODEL_TOKEN_FILE", "")
WARN_DAYS = int(os.environ.get("TOKEN_WARN_DAYS", "30"))
# TLS_VERIFY=false skips the certificate check; only for tests with self-signed certificates.
TLS_VERIFY = os.environ.get("TLS_VERIFY", "true").lower() != "false"


def tls_context(cafile=None):
    if not TLS_VERIFY:
        return ssl._create_unverified_context()
    return ssl.create_default_context(cafile=cafile)


# Kyverno in AI Essentials mounts the platform CA and sets this variable.
TLS = tls_context(os.environ.get("EZUA_DOMAIN_CA_CERT_PATH") or None)

_cache = {}


def cached(key, ttl, load):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = load()
    _cache[key] = (time.time(), value)
    return value


def get_json(url, token, ctx):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return json.load(resp)


def namespace():
    if os.environ.get("MODEL_NAMESPACE"):
        return os.environ["MODEL_NAMESPACE"]
    with open(os.path.join(SA_DIR, "namespace")) as f:
        return f.read().strip()


def inference_service():
    def load():
        # The kubelet rotates the ServiceAccount token: read it on every call.
        with open(os.path.join(SA_DIR, "token")) as f:
            sa_token = f.read().strip()
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if ":" in host:
            host = f"[{host}]"
        url = (f"https://{host}:{port}/apis/serving.kserve.io/v1beta1/namespaces/{namespace()}"
               f"/inferenceservices/{DEPLOYMENT}")
        try:
            return get_json(url, sa_token, tls_context(os.path.join(SA_DIR, "ca.crt")))
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            raise
    return cached("isvc", 20, load)


def is_ready(isvc):
    conditions = (isvc.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def endpoint():
    if MODEL_ENDPOINT:
        return MODEL_ENDPOINT
    isvc = inference_service()
    if isvc is None:
        raise RuntimeError(f"The model deployment '{DEPLOYMENT}' does not exist yet.")
    url = ((isvc.get("status") or {}).get("url") or "").rstrip("/")
    if not is_ready(isvc) or not url:
        raise RuntimeError("The model is not ready yet.")
    return url


def model_token():
    # Read on every request: Kubernetes updates the mounted Secret after a rotation.
    if MODEL_TOKEN_FILE and os.path.exists(MODEL_TOKEN_FILE):
        with open(MODEL_TOKEN_FILE) as f:
            token = f.read().strip()
        if token:
            return token
    raise RuntimeError("No endpoint API token: create the Secret model-api-token (key token).")


def token_expiry():
    """Expiry (epoch seconds) of the endpoint API token, or None."""
    try:
        part = model_token().split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("exp")
    except (RuntimeError, ValueError, IndexError):
        return None


def model_id(url):
    return cached("model:" + url, 300,
                  lambda: get_json(url + "/v1/models", model_token(), TLS)["data"][0]["id"])


def token_text():
    exp = token_expiry()
    if exp is None:
        return "API token: none or expiry unknown"
    days = (exp - time.time()) / 86400
    date = time.strftime("%Y-%m-%d", time.gmtime(exp))
    if days < 0:
        return f"**API token expired on {date}**"
    if days < WARN_DAYS:
        return f"**API token expires on {date} ({days:.0f} days): renew it**"
    return f"API token valid until {date}"


def status_text():
    try:
        if MODEL_ENDPOINT:
            model = f"Endpoint `{MODEL_ENDPOINT}` (fixed)"
        else:
            isvc = inference_service()
            if isvc is None:
                model = f"Model deployment `{DEPLOYMENT}`: not created yet"
            else:
                state = "Ready" if is_ready(isvc) else "not ready"
                url = (isvc.get("status") or {}).get("url") or "-"
                model = f"Model deployment `{DEPLOYMENT}` · **{state}** · `{url}`"
    except Exception as err:  # shown in the UI, never fatal
        model = f"Model status not available: {err}"
    return f"{model} · {token_text()}"


def stream_chat(messages, show_reasoning, max_tokens=1024):
    """Yields (reasoning, answer) as the model streams."""
    url = endpoint()
    body = {
        "model": model_id(url), "messages": messages, "stream": True,
        "max_tokens": max_tokens, "temperature": 0.7, "top_p": 0.8,
        "chat_template_kwargs": {"enable_thinking": bool(show_reasoning)},
    }
    req = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + model_token(),
                                          "Content-Type": "application/json"})
    reasoning, answer = "", ""
    with urllib.request.urlopen(req, context=TLS, timeout=600) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            delta = json.loads(data)["choices"][0].get("delta") or {}
            reasoning += delta.get("reasoning_content") or delta.get("reasoning") or ""
            answer += delta.get("content") or ""
            yield reasoning, answer
