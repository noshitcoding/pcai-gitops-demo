"""Small chat front end for a model that MLIS serves on HPE Private Cloud AI.

Model access is in backend.py: the endpoint comes from the InferenceService in this
namespace, inference uses the endpoint API token mounted from a Secret.
"""
import os
import urllib.error

import gradio as gr

import backend

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT", "You are a concise assistant running on HPE Private Cloud AI.")
APP_VERSION = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")).read().strip()


def text_of(content):
    # Gradio 6 may store message content as a list of typed parts.
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def chat(message, history, show_reasoning):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in history:
        text = text_of(item.get("content"))
        if item.get("role") in ("user", "assistant") and text:
            messages.append({"role": item["role"], "content": text})
    messages.append({"role": "user", "content": text_of(message)})
    answer = ""
    try:
        for reasoning, answer in backend.stream_chat(messages, show_reasoning):
            shown = answer
            if show_reasoning and reasoning:
                shown = "> " + reasoning.strip().replace("\n", "\n> ") + "\n\n" + answer
            yield shown
    except urllib.error.HTTPError as err:
        hint = " (endpoint API token missing, expired or revoked)" if err.code in (401, 403) else ""
        yield answer + f"\n\nRequest failed: HTTP {err.code}{hint}"
    except Exception as err:
        yield f"Not available: {err}"


THEME = gr.themes.Base(primary_hue=gr.themes.colors.emerald, font=["Arial", "sans-serif"])
CSS = """
#hpe-bar {border-top: 6px solid #01A982; padding-top: 6px}
#hpe-bar h2 {margin: 0}
footer {display: none !important}
"""

with gr.Blocks(title="PCAI GitOps Chat") as demo:
    with gr.Column(elem_id="hpe-bar"):
        gr.Markdown(f"## HPE Private Cloud AI · GitOps chat demo · version {APP_VERSION}\n"
                    "Model served by MLIS, application and model deployed by Argo CD.")
        status = gr.Markdown()
    show_reasoning = gr.Checkbox(label="Show reasoning (Qwen3 thinking mode)", value=False, render=False)
    gr.ChatInterface(chat, additional_inputs=[show_reasoning])
    demo.load(backend.status_text, None, status)

if __name__ == "__main__":
    demo.queue().launch(server_name="0.0.0.0", server_port=7860, theme=THEME, css=CSS,
                        footer_links=[], ssr_mode=False)
