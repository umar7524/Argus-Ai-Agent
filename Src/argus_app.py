"""
Argus - Chat UI (Streamlit)

A chat interface on top of argus_brain.py, with two separate agents:
  Research -> searches the live web (argus_search_server.py)
  Vault    -> reads only your private folder Argus\\vault (vault_server.py)
The brain is shared; each mode only ever gets its own tools.

Run (or double-click Argus.bat / the desktop icon):
  python -m streamlit run argus_app.py
"""

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st

import argus_brain as brain
import downloader
import emailer

st.set_page_config(page_title="Argus", page_icon="👁", layout="centered")

# ------------------------------------------------------------------ style
# Argus's hundred eyes ended up on the peacock's tail in the old myth, so the
# palette is peacock: deep teal-navy, feather green, and eye-spot gold.
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;700&display=swap');
    /* Font only on text elements: a broad selector would also replace
       Streamlit's icon font, turning icons into words like "arrow_down". */
    .stMarkdown, .stMarkdown p, .stMarkdown li, .stCaption, .stButton p,
    .stChatInput textarea, .stExpander summary p, [data-testid="stStatusWidget"] p {
        font-family: 'Manrope', system-ui, sans-serif; }
    .argus-head { display: flex; align-items: center; gap: 14px; margin: 4px 0 2px; }
    .argus-head h1 { font-size: 2.1rem; font-weight: 700; margin: 0; padding: 0;
                     letter-spacing: -0.02em; color: #E9F1EF; }
    .argus-sub { color: #9DB5B1; margin: 0 0 14px 0; font-size: 0.98rem; }
    .vault-note { border: 1px solid #2E8B6E; border-radius: 10px; padding: 10px 14px;
                  color: #CFE3DE; font-size: 0.9rem; margin: 0 0 16px 0; }
    .stChatMessage p, .stChatMessage li { line-height: 1.65; max-width: 72ch; }
    /* Each paragraph picks its own direction: Urdu lines read right-to-left,
       English and Roman Urdu stay left-to-right. */
    .stChatMessage p, .stChatMessage li, .stChatMessage td, .stChatMessage h1,
    .stChatMessage h2, .stChatMessage h3 { unicode-bidi: plaintext; text-align: start; }
    .stChatMessage a { color: #D9B44A; }
    header[data-testid="stHeader"] { background: transparent; }
    [data-testid="stDecoration"] { display: none; }
    </style>
    """,
    unsafe_allow_html=True,
)

EYE_SVG = """
<svg width="44" height="44" viewBox="0 0 44 44" aria-hidden="true">
  <ellipse cx="22" cy="22" rx="20" ry="13" fill="#123F3C" stroke="#2E8B6E" stroke-width="2"/>
  <circle cx="22" cy="22" r="8.5" fill="#1F5F7A"/>
  <circle cx="22" cy="22" r="4.5" fill="#D9B44A"/>
  <circle cx="24" cy="20" r="1.4" fill="#F4F0E0"/>
</svg>
"""

MODE_UI = {
    "research": {
        "title": "Argus",
        "icon": "🌐",
        "sub": "Ask a research question. Argus searches the live web and answers "
               "with sources you can check.",
        "input": "Ask Argus a research question",
        "examples": [
            "Compare SQA jobs in Lahore and Karachi",
            "What are Pakistan's IT exports in 2026?",
            "What's the starting salary for Data Science freshers in Lahore?",
        ],
    },
    "vault": {
        "title": "Argus Vault",
        "icon": "🔒",
        "sub": "Ask about your own files. Vault reads only the Argus\\vault folder "
               "and names the file behind every answer.",
        "input": "Ask about your files",
        "examples": [
            "What files are in my vault?",
            "What skills are listed in my CV?",
            "Give me a short summary of my notes",
        ],
    },
}

# ------------------------------------------------------------------ chat storage
# Chats are saved as JSON files in Argus\\data\\chats, so they survive closing
# the app (session_state alone lives only in memory, like a whiteboard).
CHATS_DIR = brain.PROJECT_ROOT / "data" / "chats"


def new_chat_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def greeting() -> str:
    hour = datetime.now().hour
    if hour < 5:
        return "Good night"
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    if hour < 21:
        return "Good evening"
    return "Good night"


def start_new_chat(mode: str) -> None:
    st.session_state.history = []
    st.session_state.chat_id = new_chat_id()
    st.session_state.mode = mode


def save_chat() -> None:
    history = st.session_state.history
    if not history:
        return
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    path = CHATS_DIR / f"{st.session_state.chat_id}.json"

    # Keep whatever title is already stored (including a manual rename);
    # only derive one from the first question on a brand-new chat's first save.
    existing_title = None
    if path.exists():
        try:
            existing_title = json.loads(path.read_text(encoding="utf-8")).get("title")
        except (OSError, ValueError):
            pass
    if not existing_title:
        existing_title = next((t["content"] for t in history if t["role"] == "user"), "Chat")[:60]

    record = {
        "id": st.session_state.chat_id,
        "mode": st.session_state.mode,
        "title": existing_title,
        "updated": datetime.now().isoformat(timespec="seconds"),
        "history": history,
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def rename_chat(chat_id: str, new_title: str) -> None:
    path = CHATS_DIR / f"{chat_id}.json"
    if not path.exists() or not new_title.strip():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    data["title"] = new_title.strip()[:60]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_chats(limit: int = 20) -> list[dict]:
    if not CHATS_DIR.exists():
        return []
    chats = []
    for f in CHATS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            chats.append({"id": data["id"], "title": data.get("title", "Chat"),
                          "mode": data.get("mode", "research"),
                          "updated": data.get("updated", "")})
        except (OSError, ValueError, KeyError):
            continue          # skip a damaged file instead of crashing the app
    return sorted(chats, key=lambda c: c["updated"], reverse=True)[:limit]


def load_chat(chat_id: str) -> None:
    data = json.loads((CHATS_DIR / f"{chat_id}.json").read_text(encoding="utf-8"))
    st.session_state.chat_id = chat_id
    st.session_state.history = data.get("history", [])
    st.session_state.mode = data.get("mode", "research")


# Small settings file so choices like the reply language survive a restart.
SETTINGS_FILE = brain.PROJECT_ROOT / "data" / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def delete_chat(chat_id: str) -> None:
    path = CHATS_DIR / f"{chat_id}.json"
    if path.exists():
        path.unlink()


def free_name(target: Path) -> Path:
    """Never overwrite a vault file: 'cv.docx' -> 'cv (2).docx'."""
    n = 2
    while target.exists():
        target = target.with_name(f"{target.stem.split(' (')[0]} ({n}){target.suffix}")
        n += 1
    return target


def add_uploads_to_vault(files) -> list[str]:
    """Save uploaded files into the vault (once each), never overwriting."""
    saved = []
    done = st.session_state.setdefault("uploaded_ids", set())
    for f in files or []:
        key = getattr(f, "file_id", None) or f"{f.name}:{f.size}"
        if key in done:
            continue                       # Streamlit re-sends files on every rerun
        brain.VAULT_DIR.mkdir(parents=True, exist_ok=True)
        target = free_name(brain.VAULT_DIR / Path(f.name).name)   # strip any folders
        target.write_bytes(f.getbuffer())
        done.add(key)
        saved.append(target.name)
    return saved


if "history" not in st.session_state:
    start_new_chat("research")
if "language" not in st.session_state:
    st.session_state.language = load_settings().get("language", "auto")
    if st.session_state.language not in ("auto", "english"):
        st.session_state.language = "auto"        # old choices (e.g. Urdu script) retired

# Resolved BEFORE the sidebar (not after) so the sidebar can be shown disabled
# for the rest of this run the instant a heavy question is submitted -- see
# "busy" below. st.chat_input always renders pinned to the bottom of the page
# regardless of where in the script it's called from.
mode = st.session_state.mode
ui = MODE_UI[mode]
typed = st.chat_input(ui["input"])
pending = st.session_state.pop("pending_question", None)
# True only for the one run that just received a question to process: Streamlit
# cancels a still-running script the instant the user clicks anything else, so
# disabling the sidebar's own buttons for that run stops a click from ever
# reaching the backend and interrupting a task that's mid-flight (e.g. losing
# an in-progress CV save because a chat got deleted while Argus was working).
st.session_state.busy = bool(typed or pending)

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    busy = st.session_state.busy
    labels = {"research": "🌐 Research", "vault": "🔒 Vault"}
    picked_mode = st.radio(
        "Mode", options=list(labels), format_func=labels.get, horizontal=True,
        index=list(labels).index(st.session_state.mode), label_visibility="collapsed",
        disabled=busy,
    )
    if picked_mode != st.session_state.mode:
        start_new_chat(picked_mode)      # never mix web and private in one chat
        st.rerun()

    if st.button("New chat", use_container_width=True, type="primary", disabled=busy):
        start_new_chat(st.session_state.mode)
        st.rerun()

    past = list_chats()
    if past:
        st.caption("Recent" if not busy else "Recent (locked while Argus is working)")
        for chat in past:
            is_open = chat["id"] == st.session_state.chat_id
            icon = MODE_UI.get(chat["mode"], MODE_UI["research"])["icon"]
            col_open, col_menu = st.columns([6, 1])
            with col_open:
                if st.button(f"{icon} {chat['title']}", key=f"chat_{chat['id']}",
                             use_container_width=True, disabled=is_open or busy):
                    load_chat(chat["id"])
                    st.rerun()
            with col_menu:
                with st.popover("⋮", use_container_width=True, disabled=busy):
                    new_title = st.text_input(
                        "Rename", value=chat["title"], key=f"rn_input_{chat['id']}",
                        label_visibility="collapsed", placeholder="Rename chat",
                    )
                    if st.button("Save name", key=f"rn_save_{chat['id']}", use_container_width=True):
                        rename_chat(chat["id"], new_title)
                        st.rerun()
                    st.divider()
                    confirm_key = f"del_confirm_{chat['id']}"
                    if st.session_state.get(confirm_key):
                        st.caption("Delete for good?")
                        dc1, dc2 = st.columns(2)
                        if dc1.button("Delete", key=f"del_yes_{chat['id']}", use_container_width=True):
                            delete_chat(chat["id"])
                            st.session_state.pop(confirm_key, None)
                            if is_open:
                                start_new_chat(st.session_state.mode)
                            st.rerun()
                        if dc2.button("Cancel", key=f"del_no_{chat['id']}", use_container_width=True):
                            st.session_state.pop(confirm_key, None)
                            st.rerun()
                    elif st.button("Delete chat", key=f"del_trigger_{chat['id']}", use_container_width=True):
                        st.session_state[confirm_key] = True
                        st.rerun()

    if st.session_state.mode == "vault":
        st.write("")
        st.caption("Your vault")
        uploads = st.file_uploader("Add files to your vault", accept_multiple_files=True,
                                   label_visibility="collapsed")
        added = add_uploads_to_vault(uploads)
        if added:
            st.success("Added to vault: " + ", ".join(added))
        if hasattr(os, "startfile") and st.button("Open vault folder", use_container_width=True):
            brain.VAULT_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(brain.VAULT_DIR)          # opens File Explorer (Windows)

    st.write("")
    language_labels = {"auto": "Same as my message", "english": "English"}
    picked_language = st.selectbox(
        "Reply language", options=list(language_labels), format_func=language_labels.get,
        index=list(language_labels).index(st.session_state.language),
    )
    if picked_language != st.session_state.language:
        st.session_state.language = picked_language
        save_settings({**load_settings(), "language": picked_language})

    picked_name = st.text_input("Your name (signs your emails)",
                                value=load_settings().get("sender_name", ""),
                                placeholder="e.g. Umar Sarwar")
    if picked_name.strip() != load_settings().get("sender_name", ""):
        save_settings({**load_settings(), "sender_name": picked_name.strip()})

    st.caption(f"Code version: {brain.CODE_VERSION}")
    with st.expander("How Argus works"):
        st.markdown(
            "**Research** searches the live web, then answers with a source "
            "link for every fact.\n\n"
            "**Vault** works only inside `Argus\\vault` and has no internet tools. "
            "It can read, edit, copy, move, rename and delete files, and nothing "
            "is ever lost: when a file is edited or deleted, the old version goes "
            "to `vault\\.trash`, from where Vault can restore it."
        )
    cfg = brain.MODES[st.session_state.mode]
    st.caption(f"Powered by {cfg['model']}")

# ------------------------------------------------------------------ header
st.markdown(
    f'<div class="argus-head">{EYE_SVG}<h1>{ui["title"]}</h1></div>',
    unsafe_allow_html=True,
)
if st.session_state.history:
    st.markdown(f'<p class="argus-sub">{ui["sub"]}</p>', unsafe_allow_html=True)
if mode == "vault":
    host = urlparse(cfg["base_url"]).hostname or ""
    where = ("stays on this laptop" if host in ("localhost", "127.0.0.1")
             else f"is sent to {host} to write the answer")
    st.markdown(
        f'<div class="vault-note">No internet tools in this mode. '
        f"Text from the files Vault reads {where}, so keep very sensitive "
        f"documents out of the vault for now.</div>",
        unsafe_allow_html=True,
    )

AVATARS = {"user": "🧑", "assistant": "👁️"}


def tokens_line(usage: dict | None) -> str:
    """'Tokens used: 4,512 (in 3,800 + out 712, 1 model call)' from the provider's own numbers."""
    if not usage or not usage.get("calls"):
        return ""
    total = usage["prompt"] + usage["completion"]
    calls = usage["calls"]
    thinking = usage.get("reasoning") or 0
    extra = f", of which hidden thinking {thinking:,}" if thinking else ""
    return (f"Tokens used: {total:,} (in {usage['prompt']:,} + out {usage['completion']:,}{extra}, "
            f"{calls} model call{'s' if calls != 1 else ''})")

# ------------------------------------------------------------------ history
for turn in st.session_state.history:
    with st.chat_message(turn["role"], avatar=AVATARS[turn["role"]]):
        if turn.get("steps"):
            with st.expander(f"Steps ({len(turn['steps'])})"):
                for line in turn["steps"]:
                    st.markdown(f"- {line}")
        st.markdown(turn["content"])
        if turn.get("report"):
            st.caption(f"Report saved: {turn['report']}")
        if tokens_line(turn.get("tokens")):
            st.caption(tokens_line(turn.get("tokens")))

# ------------------------------------------------------------------ vault -> research handoff
# Vault has no internet on purpose. When it gets an internet task it adds a
# [[RESEARCH]] tag; the user can then move the question to Research in one click.
HANDOFF_TAG = "[[RESEARCH]]"
last = st.session_state.history[-1] if st.session_state.history else None
if (mode == "vault" and last and last["role"] == "assistant"
        and last.get("handoff") and len(st.session_state.history) >= 2):
    asked = st.session_state.history[-2]["content"]
    if st.button("🌐 Run this in Research mode", type="primary"):
        start_new_chat("research")
        st.session_state.pending_question = asked
        st.rerun()

# ------------------------------------------------------------------ download approvals
# The AI can only ASK to download; nothing is fetched until the user clicks.
if st.session_state.get("download_msg"):
    kind, text = st.session_state.pop("download_msg")
    (st.success if kind == "ok" else st.error)(text)

for req in downloader.list_pending():
    with st.container(border=True):
        size = (f"{req['size_bytes'] / 1_048_576:.2f} MB" if req.get("size_bytes")
                else "size unknown")
        st.markdown(f"**Download request:** `{req['filename']}` ({size}) from **{req['site']}**")
        if req.get("reason"):
            st.caption(req["reason"])
        c1, c2 = st.columns(2)
        if c1.button("Download to vault", key=f"dl_{req['id']}", type="primary",
                     use_container_width=True):
            try:
                saved = downloader.perform_download(req["id"])
                st.session_state.download_msg = (
                    "ok", f"Saved to vault: {saved.relative_to(brain.VAULT_DIR)}")
            except Exception as e:
                st.session_state.download_msg = ("error", f"Download failed: {e}")
            st.rerun()
        if c2.button("Cancel", key=f"cancel_{req['id']}", use_container_width=True):
            downloader.cancel(req["id"])
            st.rerun()

# ------------------------------------------------------------------ email approvals
# The AI can only DRAFT an email; nothing is sent until the user reviews the
# card and clicks Send. Fields are editable right on the card.
if st.session_state.get("email_msg"):
    kind, text = st.session_state.pop("email_msg")
    (st.success if kind == "ok" else st.error)(text)

pending_emails = list(reversed(emailer.list_pending()))   # newest first, so it's never buried
for i, req in enumerate(pending_emails):
    with st.container(border=True):
        stale = " ⚠️ older draft, check it's still wanted" if i > 0 else ""
        st.markdown(f"**✉️ {req['subject']}** → {req['to']}"
                    + (f"  📎 {req['attachment']}" if req.get("attachment") else ""),
                    unsafe_allow_html=True)
        st.caption(f"{emailer.age_str(req)}{stale}" + (f" · {req['reason']}" if req.get("reason") else ""))
        to_val = st.text_input("To", req["to"], key=f"to_{req['id']}")
        cc_val = st.text_input("Cc (optional)", req.get("cc", ""), key=f"cc_{req['id']}")
        subject_val = st.text_input("Subject", req["subject"], key=f"subj_{req['id']}")
        body_val = st.text_area("Body", req["body"], key=f"body_{req['id']}", height=160)
        if req.get("reply_to"):
            st.caption("↩ Sent as a reply inside the same Gmail conversation")
        # Only files Argus made in vault\tailored_cvs can be attached; the person decides.
        no_file = "(no attachment)"
        attach_options = [no_file] + emailer.list_attachments()
        current = req.get("attachment") or no_file
        if current not in attach_options:
            attach_options.append(current)
        attach_val = st.selectbox("Attachment (from vault\\tailored_cvs)", attach_options,
                                  index=attach_options.index(current), key=f"att_{req['id']}")

        left_over = emailer.find_placeholders(subject_val + "\n" + body_val)
        if left_over:
            st.warning("Replace these placeholders before sending: " + ", ".join(left_over))
        c1, c2 = st.columns(2)
        if c1.button("Send", key=f"send_{req['id']}", type="primary", use_container_width=True,
                     disabled=bool(left_over)):
            try:
                emailer.update(req["id"], to=to_val, cc=cc_val, subject=subject_val, body=body_val,
                               attachment="" if attach_val == no_file else attach_val)
                sent = emailer.perform_send(req["id"])
                st.session_state.email_msg = ("ok", f"Sent to {sent['sent_to']}: {sent['subject']}"
                                              + (f" (with {sent['attachment']})" if sent.get("attachment") else ""))
            except Exception as e:
                st.session_state.email_msg = ("error", f"Could not send: {e}")
            st.rerun()
        if c2.button("Cancel", key=f"emailcancel_{req['id']}", use_container_width=True):
            emailer.cancel(req["id"])
            st.rerun()

picked = None
if not st.session_state.history and typed is None and pending is None:
    st.markdown(f"**{greeting()}.** {ui['sub']}")
    st.caption("Try asking")
    for ex in ui["examples"]:
        if st.button(ex, use_container_width=True):
            picked = ex

question = typed or pending or picked

# ------------------------------------------------------------------ run
STEP_TEXT = {
    "THINKING": "Planning the next step",
    "WRITING": "Writing the answer",
}


def root_cause(exc: BaseException) -> BaseException:
    """asyncio wraps errors in ExceptionGroups; unwrap to the real one."""
    while getattr(exc, "exceptions", None):
        exc = exc.exceptions[0]
    return exc


def friendly_error(exc: BaseException) -> str:
    """Explain what went wrong and what to do, in one or two sentences."""
    api_err = brain.find_api_error(exc)
    if api_err is not None:
        if api_err.status_code == 429:
            return ("Groq's free-tier limit was reached, and it didn't clear even after "
                    "Argus waited and retried automatically. Wait a minute or two, then ask again.")
        if api_err.status_code == 413:
            return ("This single request is too large for Groq's free tier to handle in "
                    "one go -- waiting won't fix this specific error, it needs to be smaller. "
                    "Try again with a shorter job description (just the requirements), or "
                    "split it into two messages: first \"read my CV and profile\", then "
                    "separately \"now build the tailored CV for this job: ...\".")
        if api_err.status_code == 404:
            return ("The model name in `.env` is not available. "
                    "Check the provider's model list and update it.")
        if api_err.status_code == 401:
            return "The API key was rejected. Check the key in `.env`."
        return f"The model service returned an error (HTTP {api_err.status_code}): {api_err.message}"
    cause = root_cause(exc)
    if isinstance(cause, RuntimeError):
        return str(cause)
    if type(cause).__name__ == "APIConnectionError":
        return ("Couldn't reach Groq after a few tries. Check your internet "
                "connection (and that no antivirus/firewall is blocking "
                "Argus), then ask again.")
    return f"Something went wrong: {type(cause).__name__}: {cause}"


if question:
    st.session_state.history.append({"role": "user", "content": question})
    with st.chat_message("user", avatar=AVATARS["user"]):
        st.markdown(question)

    with st.chat_message("assistant", avatar=AVATARS["assistant"]):
        status = st.status("Working ...", expanded=True)
        calls: list[str] = []

        def on_step(tag: str, text: str) -> None:
            if tag == "SEARCH":
                calls.append(text)
                status.write(text)
            elif tag == "TOOL ERROR":
                status.write(f"Blocked or failed: {text}")
            elif tag == "WAITING":
                status.write(text)
                status.update(label="Waiting for the free-tier limit to clear ...")
            elif tag in STEP_TEXT:
                status.update(label=STEP_TEXT[tag] + " ...")

        started = time.time()
        try:
            result = asyncio.run(brain.run(
                question, on_step=on_step, mode=mode,
                history=st.session_state.history[:-1],   # earlier turns, not this question
                language=st.session_state.language,
            ))
        except BaseException as exc:          # noqa: BLE001 - shown to the user
            status.update(label="Stopped", state="error", expanded=False)
            message = friendly_error(exc)
            st.error(message)
            st.session_state.history.append({"role": "assistant", "content": message})
            save_chat()
            st.rerun()
        else:
            seconds = round(time.time() - started)
            n = len(calls)
            if mode == "vault":
                label = f"Checked your files in {n} {'step' if n == 1 else 'steps'}, {seconds} seconds"
            else:
                label = f"Researched with {n} {'search' if n == 1 else 'searches'} in {seconds} seconds"
            status.update(label=label, state="complete", expanded=False)
            handoff = HANDOFF_TAG in result["answer"]
            result["answer"] = result["answer"].replace(HANDOFF_TAG, "").strip()
            st.markdown(result["answer"])
            if result.get("report"):
                st.caption(f"Report saved: {result['report']}")
            if tokens_line(result.get("tokens")):
                st.caption(tokens_line(result.get("tokens")))
            st.session_state.history.append({
                "role": "assistant",
                "content": result["answer"],
                "steps": result["steps"],
                "report": result.get("report"),
                "tokens": result.get("tokens"),
                "handoff": handoff,
            })
            save_chat()
            st.rerun()        # refresh the sidebar so this chat appears in "Recent"
