"""Small chat front end for a model that MLIS serves on HPE Private Cloud AI.

The app finds the model endpoint through the MLIS API and authenticates with the
access token that AI Essentials keeps for the technical user.
"""
import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.request

import gradio as gr

MLIS_URL = os.environ.get("MLIS_URL", "").rstrip("/")
PROJECT = os.environ.get("MLIS_PROJECT", "")
DEPLOYMENT = os.environ.get("MLIS_DEPLOYMENT", "")
TOKEN_NAMESPACE = os.environ.get("TOKEN_NAMESPACE", "")
TOKEN_SECRET = os.environ.get("TOKEN_SECRET", "access-token")
MODEL_ENDPOINT = os.environ.get("MODEL_ENDPOINT", "").rstrip("/")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT", "You are a concise assistant running on HPE Private Cloud AI.")
SA_DIR = os.environ.get("SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount")

# Kyverno in AI Essentials mounts the platform CA and sets this variable.
TLS = ssl.create_default_context(cafile=os.environ.get("EZUA_DOMAIN_CA_CERT_PATH") or None)

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


def aie_token():
    def load():
        if os.environ.get("AIE_TOKEN"):
            return os.environ["AIE_TOKEN"]
        with open(os.path.join(SA_DIR, "token")) as f:
            sa_token = f.read().strip()
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if ":" in host:
            host = f"[{host}]"
        ctx = ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))
        secret = get_json(f"https://{host}:{port}/api/v1/namespaces/{TOKEN_NAMESPACE}"
                          f"/secrets/{TOKEN_SECRET}", sa_token, ctx)
        return base64.b64decode(secret["data"]["AUTH_TOKEN"]).decode().strip()
    return cached("token", 60, load)


def deployment():
    def load():
        for dep in get_json(MLIS_URL + "/api/v1/deployments", aie_token(), TLS):
            if dep.get("name") == DEPLOYMENT and dep.get("project", "") in ("", PROJECT):
                return dep
        return None
    return cached("deployment", 20, load)


def endpoint():
    if MODEL_ENDPOINT:
        return MODEL_ENDPOINT
    dep = deployment()
    if dep is None:
        raise RuntimeError(f"MLIS deployment '{DEPLOYMENT}' does not exist yet.")
    url = ((dep.get("state") or {}).get("endpoint") or "").rstrip("/")
    if dep.get("status") != "Ready" or not url:
        raise RuntimeError(f"The model is not ready yet (MLIS status: {dep.get('status')}).")
    return url


def model_id(url):
    return cached("model:" + url, 300,
                  lambda: get_json(url + "/v1/models", aie_token(), TLS)["data"][0]["id"])


def status_text():
    try:
        dep = deployment() if not MODEL_ENDPOINT else None
        if MODEL_ENDPOINT:
            return f"Endpoint `{MODEL_ENDPOINT}` (fixed)"
        if dep is None:
            return f"MLIS deployment `{DEPLOYMENT}`: not created yet"
        url = (dep.get("state") or {}).get("endpoint") or "-"
        return f"MLIS deployment `{DEPLOYMENT}` · status **{dep.get('status')}** · `{url}`"
    except Exception as err:  # shown in the UI, never fatal
        return f"MLIS not reachable: {err}"


def chat(message, history, show_reasoning):
    try:
        url = endpoint()
        model = model_id(url)
    except Exception as err:
        yield f"Not available: {err}"
        return
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in history:
        if item.get("role") in ("user", "assistant") and isinstance(item.get("content"), str):
            messages.append({"role": item["role"], "content": item["content"]})
    messages.append({"role": "user", "content": message})
    body = {
        "model": model, "messages": messages, "stream": True,
        "max_tokens": 1024, "temperature": 0.7, "top_p": 0.8,
        "chat_template_kwargs": {"enable_thinking": bool(show_reasoning)},
    }
    req = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + aie_token(),
                                          "Content-Type": "application/json"})
    reasoning, answer = "", ""
    try:
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
                shown = answer
                if show_reasoning and reasoning:
                    shown = "> " + reasoning.strip().replace("\n", "\n> ") + "\n\n" + answer
                yield shown
    except urllib.error.HTTPError as err:
        yield answer + f"\n\nRequest failed: HTTP {err.code}"


THEME = gr.themes.Base(primary_hue=gr.themes.colors.emerald, font=["Arial", "sans-serif"])
CSS = """
#hpe-bar {border-top: 6px solid #01A982; padding-top: 6px}
#hpe-bar h2 {margin: 0}
footer {display: none !important}
"""

with gr.Blocks(title="PCAI GitOps Chat") as demo:
    with gr.Column(elem_id="hpe-bar"):
        gr.Markdown("## HPE Private Cloud AI · GitOps chat demo\n"
                    "Model served by MLIS, application and model deployed by Argo CD.")
        status = gr.Markdown()
    show_reasoning = gr.Checkbox(label="Show reasoning (Qwen3 thinking mode)", value=False, render=False)
    gr.ChatInterface(chat, additional_inputs=[show_reasoning])
    demo.load(status_text, None, status)

if __name__ == "__main__":
    demo.queue().launch(server_name="0.0.0.0", server_port=7860, theme=THEME, css=CSS,
                        show_api=False)
