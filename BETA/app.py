from flask import Flask, render_template, request, jsonify, send_file, url_for
import gzip
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from werkzeug.utils import secure_filename

try:
    from PIL import Image, ImageFilter, ImageFont
except ImportError:
    Image = None
    ImageFilter = None
    ImageFont = None
from openai import OpenAI

from autoquence_v3 import AutoquenceAI, SceneGraph, VideoInfo, SceneElement
from or_reasoning import reasoning_extra_body, extract_reasoning_details


# ============================================================
# AUTOQUENCE APP
#
# The important contract is:
#
#   browser scene
#       -> /api/autoquence/edit
#       -> AutoquenceAI planner
#       -> canonical actions
#       -> browser executor
#
# The browser is responsible for live preview.
# FFmpeg is responsible only for final export.
# ============================================================

app = Flask(__name__)

UPLOAD_FOLDER = Path("static/uploads")
OUTPUT_FOLDER = Path("static/outputs")
PROMPTS_FOLDER = Path("static/promptbeta")

for folder in (UPLOAD_FOLDER, OUTPUT_FOLDER, PROMPTS_FOLDER):
    folder.mkdir(parents=True, exist_ok=True)

app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["OUTPUT_FOLDER"] = str(OUTPUT_FOLDER)
app.config["PROMPTS_FOLDER"] = str(PROMPTS_FOLDER)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

# Static assets (JS/CSS/images/videos) get immutable-style caching.
# Templates reference them via static_v() which appends an mtime-based
# ?v=... query so every deploy busts the cache automatically.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000


# ============================================================
# RESPONSE COMPRESSION
#
# Gzips JSON/HTML/CSS/JS responses above 1KB. canvas.js alone drops
# from ~104KB to ~25KB on the wire. Streamed files (video range
# requests) are skipped via direct_passthrough.
# ============================================================

_COMPRESSIBLE_MIMES = {
    "application/json",
    "text/html",
    "text/css",
    "application/javascript",
    "text/javascript",  # modern Werkzeug mimetype for .js
    "text/plain",
    "image/svg+xml",
}


@app.after_request
def _gzip_response(response):
    accept = request.headers.get("Accept-Encoding", "")

    if (
        "gzip" not in accept.lower()
        or response.status_code < 200
        or response.status_code >= 300
        or response.mimetype not in _COMPRESSIBLE_MIMES
        or "Content-Encoding" in response.headers
    ):
        return response

    if response.direct_passthrough:
        # send_file() streams raw bytes; small JS/CSS assets are safe to
        # buffer so they can be compressed. Video/other streamed content
        # never reaches here thanks to the mimetype check above.
        response.direct_passthrough = False

    data = response.get_data()
    if len(data) < 1024:
        return response

    compressed = gzip.compress(data, compresslevel=6)
    response.set_data(compressed)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(compressed))
    response.headers.add("Vary", "Accept-Encoding")
    return response


@app.template_global()
def static_v(filename):
    """url_for('static') with an mtime-based cache-busting version."""
    try:
        version = int((Path(app.static_folder) / filename).stat().st_mtime)
    except OSError:
        version = int(time.time())
    return url_for("static", filename=filename, v=version)

ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm"}

from dotenv import load_dotenv
load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if not OPENROUTER_API_KEY:
    print("WARNING: OPENROUTER_API_KEY is not configured.")

# Keep one AutoquenceAI instance so its own history/undo implementation,
# if enabled by autoquence_v3.py, remains available.
autoquence_ai = AutoquenceAI()


# ============================================================
# CHAT ROUTER
#
# Lightweight intent gate: obvious questions / small talk are
# answered directly WITHOUT calling the heavy edit planner.
# Anything that does not look conversational falls through to
# the normal planner path unchanged.
# ============================================================

CHAT_MODEL = os.getenv("OPENROUTER_CHAT_MODEL", "minimax/minimax-m3:free")

_CONVERSATION_PATTERN = re.compile(
    r"^\s*"
    r"(what|whats|what's|who|whos|who's|how|hows|how's|why|when|where|which"
    r"|is\s+there|are\s+there|do\s+you|did\s+you|can\s+you\s+(explain|tell)"
    r"|tell\s+me|explain|list\s+(all\s+)?(the\s+)?(fonts|colors|options)"
    r"|hi\b|hey\b|hello\b|yo\b|sup\b|thanks|thank\s+you|thx|ty\b"
    r"|cool\b|nice\b|great\b|awesome\b|good\s+(job|work)|lol\b)",
    re.IGNORECASE,
)

# Prompts containing any of these are treated as EDIT requests and
# routed to the planner. A prompt that matches NEITHER the
# conversation pattern NOR this pattern is vague/non-editing input
# and is answered conversationally instead.
_EDIT_VERB_PATTERN = re.compile(
    r"\b(add|create|insert|put|place|write|make|change|set|turn|replace"
    r"|remove|delete|drop|hide|show|crop|trim|cut|speed|rush|slow"
    r"|resize|scale|style|recolor|color|colour|font|banner|overlay"
    r"|text|caption|subtitle|logo|background|bigger|smaller|larger"
    r"|faster|slower|zoom|flip|rotate|move|position|top|bottom)\b",
    re.IGNORECASE,
)

CHAT_SYSTEM_PROMPT_TEMPLATE = """You are Autoquence, the AI assistant inside a natural-language video editor web app.

The user is ASKING A QUESTION or chatting or typed something vague — they are NOT requesting a concrete edit.
Answer in ONE straight line: maximum TWO short sentences. No lists, no line breaks, no markdown.
Be friendly and concrete.

Your founder is temi olajide. you were not created by the open ai team u were created by the autoquence team.

Autoquence's capabilities (mention these when relevant):
- Add/edit/remove text overlays and banners (top/bottom bars with text)
- Style text: font family, size, color (any hex color), position
- Crop the video to any aspect ratio (16:9, 9:16, 1:1, ...)


Available fonts in this session: {fonts}

If the user greets you, greet back and suggest something they could try,
like "add a black banner at the top saying SUBSCRIBE".
If their message is too vague to act on, briefly ask what they'd like changed.
Never invent fonts that are not in the list."""


def looks_like_conversation(prompt: str) -> bool:
    """
    Cheap zero-cost routing heuristic:
      - Obvious questions / small talk -> conversation.
      - Anything WITHOUT an edit verb  -> vague non-editing input,
        also handled as conversation.
      - Only prompts with real edit verbs reach the planner.
    """
    text = prompt or ""
    if _CONVERSATION_PATTERN.match(text):
        return True
    # Vague input: no recognizable edit action anywhere in the prompt.
    return not _EDIT_VERB_PATTERN.search(text)


def _clean_reply_line(reply: str) -> str:
    """Collapse an AI reply onto ONE straight line."""
    return re.sub(r"\s+", " ", reply or "").strip()


# One shared OpenAI client for the whole app: connection pooling means
# conversational prompts skip a fresh TLS handshake (~100-300ms) that
# per-request client creation used to pay on every message.
_chat_client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    timeout=30.0,
) if OPENROUTER_API_KEY else None


def answer_conversation(prompt: str, history, available_fonts=None):
    """
    Answer a conversational/question prompt with a cheap, tiny API call
    (no scene context, no JSON mode, few tokens).

    Returns ``(reply, reasoning_details)`` where ``reasoning_details`` is the
    OpenRouter chain-of-thought (when the provider surfaced it) so the caller
    can forward it verbatim on a later turn; it is ``None`` otherwise (common
    on ``:free`` routes).
    """
    fonts = ", ".join(available_fonts or []) or "standard system fonts"

    if _chat_client is None:
        return "", None

    messages = (
        [
            {
                "role": "system",
                "content": CHAT_SYSTEM_PROMPT_TEMPLATE.format(fonts=fonts),
            }
        ]
        + list(history[-6:])
        + [{"role": "user", "content": prompt}]
    )

    try:
        response = _chat_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=messages,
            temperature=0.4,
            max_tokens=200,
            timeout=20.0,
            extra_body=reasoning_extra_body(),
        )
        assistant_message = response.choices[0].message
        reply = (assistant_message.content or "").strip()
    except Exception as exc:
        print(f"[CHAT] Conversational answer failed: {exc}")
        reply = ""

    reply = reply or (
        "I'm here! Try an edit like "
        "\"add a black banner at the top saying SUBSCRIBE\"."
    )

    reasoning_details = extract_reasoning_details(
        locals().get("assistant_message")
    )
    return reply, reasoning_details


def conversation_response(message: str, reasoning_details=None) -> dict:
    """
    Build the same response shape as normalize_result() so the
    browser contract stays identical for chat replies.
    """
    resp = {
        "response_type": "conversation",
        "message": message,
        "intent_summary": message,
        "plan": {"actions": [], "assumptions": []},
    }
    if reasoning_details:
        resp["reasoning_details"] = reasoning_details
    return resp
autoquence_ai = AutoquenceAI()


# ============================================================
# BASIC HELPERS
# ============================================================

def allowed_file(filename: str) -> bool:
    return (
        bool(filename)
        and "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def safe_filename(filename: str) -> str:
    return secure_filename(filename or "")


def safe_float(value, default=0.0):
    try:
        number = float(value)
        return number if number == number else default
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def first_defined(*values):
    for value in values:
        if value is not None:
            return value
    return None


# ============================================================
# SCENE NORMALIZATION
# ============================================================

def normalize_element(raw):
    """
    Accept both scene formats used by the Autoquence versions in this project.

    Canonical browser format:
        {
            id,
            type,
            role,
            properties,
            z_index
        }

    Older format may put geometry at the top level.
    """

    if not isinstance(raw, dict):
        return None

    element_id = raw.get("id") or raw.get("elementId")
    if not element_id:
        return None

    element_type = raw.get("type") or raw.get("elementType") or "unknown"
    role = raw.get("role")

    properties = dict(raw.get("properties") or {})

    # Preserve useful top-level values from older scene payloads.
    for key in (
        "x",
        "y",
        "width",
        "height",
        "text",
        "content",
        "font",
        "font_family",
        "fontFamily",
        "font_size",
        "fontSize",
        "font_weight",
        "fontWeight",
        "text_color",
        "textColor",
        "bg_color",
        "backgroundColor",
        "background_color",
        "fill",
        "position",
        "padding",
        "opacity",
        "shape",
        "parent_id",
        "parentId",
        "aspect_ratio",
    ):
        if key in raw and key not in properties:
            properties[key] = raw[key]

    # Canonical aliases.
    if "font_family" in properties and "font" not in properties:
        properties["font"] = properties["font_family"]

    if "fontFamily" in properties and "font" not in properties:
        properties["font"] = properties["fontFamily"]

    if "font_size" in properties and "fontSize" not in properties:
        properties["fontSize"] = properties["font_size"]

    if "font_weight" in properties and "fontWeight" not in properties:
        properties["fontWeight"] = properties["font_weight"]

    if "text_color" in properties and "textColor" not in properties:
        properties["textColor"] = properties["text_color"]

    if "bg_color" in properties and "backgroundColor" not in properties:
        properties["backgroundColor"] = properties["bg_color"]

    if "background_color" in properties and "backgroundColor" not in properties:
        properties["backgroundColor"] = properties["background_color"]

    if "content" in properties and "text" not in properties:
        properties["text"] = properties["content"]

    if "text" in properties and "content" not in properties:
        properties["content"] = properties["text"]

    if "parentId" in raw and "parent_id" not in properties:
        properties["parent_id"] = raw["parentId"]

    # Normalize banner semantics.
    if element_type == "banner":
        position = str(
            first_defined(
                properties.get("position"),
                "bottom" if safe_float(properties.get("y"), 0.0) > 0.5 else "top",
            )
        ).lower()

        role = role or ("bottom_banner" if "bottom" in position else "top_banner")

    return SceneElement(
        id=str(element_id),
        type=str(element_type),
        role=role,
        properties=properties,
        zIndex=safe_int(
            first_defined(
                raw.get("zIndex"),
                raw.get("z_index"),
                properties.get("zIndex"),
                properties.get("z_index"),
                0,
            ),
            0,
        ),
    )


def build_scene(scene_data):
    if not isinstance(scene_data, dict):
        raise ValueError("Scene state is required.")

    raw_video = scene_data.get("video") or {}

    video = VideoInfo(
        width=safe_int(raw_video.get("width"), 0),
        height=safe_int(raw_video.get("height"), 0),
        duration=safe_float(raw_video.get("duration"), 0.0),
        filename=raw_video.get("filename"),
    )

    elements = []
    for raw in scene_data.get("elements") or []:
        element = normalize_element(raw)
        if element:
            elements.append(element)

    canvas = dict(scene_data.get("canvas") or {})

    # Crop may be supplied in either location by older clients.
    crop = first_defined(
        raw_video.get("crop"),
        canvas.get("crop"),
    )
    if isinstance(crop, dict):
        canvas["crop"] = crop

    # Accept both "references" (server canonical) and "refs" (browser snapshot)
    references = dict(
        scene_data.get("references") or
        scene_data.get("refs") or
        {}
    )
    # Normalize camelCase keys from browser snapshot to server canonical keys
    if "lastCreatedId" in references and "last_created" not in references:
        references["last_created"] = references.pop("lastCreatedId")
    if "lastReferencedId" in references and "last_referenced" not in references:
        references["last_referenced"] = references.pop("lastReferencedId")

    # Also normalize element geometry: browser uses top-level x/y/width/height
    # but older normalize_element() only copies them to properties, not top-level.
    # Fix: patch elements to have correct top-level geometry from raw data.
    raw_elements = scene_data.get("elements") or []
    for i, el in enumerate(elements):
        if i < len(raw_elements):
            raw = raw_elements[i]
            el.x      = safe_float(raw.get("x"),      0.0)
            el.y      = safe_float(raw.get("y"),      0.0)
            el.width  = safe_float(raw.get("width"),  1.0)
            el.height = safe_float(raw.get("height"), 0.1)
            el.parentId = (
                raw.get("parentId") or
                raw.get("parent_id") or
                (raw.get("properties") or {}).get("parentId") or
                (raw.get("properties") or {}).get("parent_id") or
                None
            )

    return SceneGraph(
        video=video,
        elements=elements,
        canvas=canvas,
        references=references,
        version=safe_int(scene_data.get("version"), 0),
    )


# ============================================================
# ACTION NORMALIZATION
# ============================================================

def normalize_action(action):
    """
    The planner's canonical format is:

        {
            "action": "add_shape",
            "id": "...",
            "target": "...",
            "properties": {...}
        }

    Older planners sometimes put the properties directly on the action.
    Normalize both forms before sending the response to the browser.
    """

    if not isinstance(action, dict):
        return None

    normalized = {
        "action": action.get("action"),
        "id": action.get("id"),
        "target": action.get("target"),
        "target_role": action.get("target_role"),
        "properties": dict(action.get("properties") or {}),
        "reason": action.get("reason"),
    }

    flat_keys = (
        "text",
        "content",
        "position",
        "font",
        "font_family",
        "fontFamily",
        "font_size",
        "fontSize",
        "font_weight",
        "fontWeight",
        "text_color",
        "textColor",
        "bg_color",
        "backgroundColor",
        "background_color",
        "fill",
        "color",
        "width",
        "height",
        "x",
        "y",
        "opacity",
        "padding",
        "aspect_ratio",
        "speed",
        "start",
        "end",
        "shape",
        "role",
        "alignment",
        "z_index",
        "parent_id",
        "asset_id",
        "src",
        "url",
    )

    for key in flat_keys:
        if key in action and key not in normalized["properties"]:
            normalized["properties"][key] = action[key]

    # If the planner says "target_role": "top_banner", make that explicit
    # inside properties too. This makes the browser executor deterministic.
    if (
        normalized["target_role"]
        and "role" not in normalized["properties"]
    ):
        normalized["properties"]["role"] = normalized["target_role"]

    # Keep add_banner as add_banner — the frontend has a dedicated executor for it.
    # Only normalize the bare "banner" alias (which the AI should never emit).
    if normalized["action"] == "banner":
        normalized["action"] = "add_banner"

    return normalized


def normalize_result(result):
    """
    Return a single stable API shape regardless of which Autoquence V3
    response implementation is currently installed.
    """

    if not isinstance(result, dict):
        return {
            "response_type": "conversation",
            "message": "Autoquence returned an invalid result.",
            "plan": {"actions": [], "assumptions": []},
        }

    raw_plan = result.get("plan") or {}

    raw_actions = raw_plan.get("actions")
    if raw_actions is None:
        raw_actions = result.get("actions") or []

    actions = []
    for raw_action in raw_actions:
        normalized = normalize_action(raw_action)
        if normalized and normalized.get("action"):
            actions.append(normalized)

    response_type = result.get("response_type")

    if not response_type:
        response_type = "edit" if actions else "conversation"

    message = first_defined(
        result.get("message"),
        result.get("intent_summary"),
        raw_plan.get("intent_summary"),
        "Done." if actions else "I couldn't find an edit to apply.",
    )

    assumptions = first_defined(
        raw_plan.get("assumptions"),
        result.get("assumptions"),
        [],
    )

    normalized = {
        "response_type": response_type,
        "message": message,
        "intent_summary": first_defined(
            result.get("intent_summary"),
            raw_plan.get("intent_summary"),
            message,
        ),
        "plan": {
            "intent_summary": first_defined(
                raw_plan.get("intent_summary"),
                result.get("intent_summary"),
                message,
            ),
            "actions": actions,
            "assumptions": assumptions if isinstance(assumptions, list) else [],
        },
    }

    if "scene" in result:
        normalized["scene"] = result["scene"]

    # Preserve OpenRouter chain-of-thought (when present) so the browser can
    # store it in history and forward it for multi-turn reasoning.
    if result.get("reasoning_details"):
        normalized["reasoning_details"] = result["reasoning_details"]

    return normalized


# ============================================================
# PAGES
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/canvas/<filename>")
def canvas_page(filename):
    return render_template("canvas.html", filename=filename)


# ============================================================
# NORMAL UPLOAD
# ============================================================

@app.post("/upload")
def upload():
    file = request.files.get("video")

    if not file or not file.filename:
        return jsonify({"error": "No video selected."}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "Unsupported video type."}), 400

    filename = safe_filename(file.filename)

    if not filename:
        return jsonify({"error": "Invalid filename."}), 400

    file.save(UPLOAD_FOLDER / filename)

    return jsonify({
        "status": "complete",
        "filename": filename,
    })


# ============================================================
# CHUNKED UPLOAD
# ============================================================

_chunk_store = {}


@app.post("/upload-chunk")
def upload_chunk():
    chunk = request.files.get("chunk")
    filename = request.form.get("filename", "")

    try:
        chunk_index = int(request.form.get("chunkIndex", 0))
        total_chunks = int(request.form.get("totalChunks", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid chunk metadata."}), 400

    if not chunk or not filename:
        return jsonify({"error": "Missing chunk or filename."}), 400

    if total_chunks < 1 or chunk_index < 0 or chunk_index >= total_chunks:
        return jsonify({"error": "Invalid chunk index."}), 400

    safe_name = safe_filename(filename)

    if not safe_name:
        return jsonify({"error": "Invalid filename."}), 400

    tmp_dir = UPLOAD_FOLDER / f"_tmp_{safe_name}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    chunk_path = tmp_dir / f"chunk_{chunk_index:06d}"
    chunk.save(chunk_path)

    store = _chunk_store.setdefault(
        safe_name,
        {
            "received": set(),
            "total": total_chunks,
        },
    )

    store["received"].add(chunk_index)

    if len(store["received"]) < total_chunks:
        return jsonify({
            "status": "partial",
            "received": chunk_index,
        })

    final_path = UPLOAD_FOLDER / safe_name

    try:
        with open(final_path, "wb") as output:
            for index in range(total_chunks):
                part = tmp_dir / f"chunk_{index:06d}"
                if not part.exists():
                    raise RuntimeError(f"Missing chunk {index}.")
                with open(part, "rb") as source:
                    shutil.copyfileobj(source, output)

    except Exception as exc:
        return jsonify({
            "error": f"Could not assemble upload: {exc}",
        }), 500

    shutil.rmtree(tmp_dir, ignore_errors=True)
    _chunk_store.pop(safe_name, None)

    return jsonify({
        "status": "complete",
        "filename": safe_name,
    })


# ============================================================
# AI EDIT ENDPOINT
# ============================================================

@app.post("/api/autoquence/edit")
def autoquence_edit():
    data = request.get_json(silent=True) or {}

    prompt = str(data.get("prompt") or "").strip()
    scene_data = data.get("scene")

    # --------------------------------------------------------
    # CONVERSATION HISTORY — sent by the canvas client
    # (convHistory). Sanitize into {role, content} dicts so it
    # can be passed to the chat model and the edit planner.
    # --------------------------------------------------------
    raw_history = data.get("conversation") or []
    history = []
    for item in raw_history:
        if isinstance(item, dict):
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                entry = {"role": role, "content": content}
                # Preserve OpenRouter reasoning if the client returned it so it
                # can be forwarded verbatim for multi-turn chain-of-thought.
                rd = item.get("reasoning_details")
                if role == "assistant" and rd:
                    entry["reasoning_details"] = rd
                history.append(entry)

    # --------------------------------------------------------
    # PENDING QUESTION — the canvas client explicitly tracks the
    # question Autoquence asked (clarification / follow-up) and
    # sends it as "pending_question". If present, the user's
    # message is an ANSWER to it, not a fresh request. The old
    # "?-suffix" heuristic on the last history message is kept
    # only as a fallback for older clients.
    # --------------------------------------------------------
    pending_question = str(data.get("pending_question") or "").strip()
    last_assistant = next(
        (m["content"] for m in reversed(history) if m["role"] == "assistant"),
        "",
    )
    answering_pending_question = bool(
        prompt
        and (
            pending_question
            or (last_assistant and last_assistant.rstrip().endswith("?"))
        )
    )

    # --------------------------------------------------------
    # VAGUE INPUT — a very short message that is not a greeting,
    # question, or thanks (e.g. just "white", "Impact", "bigger")
    # carries no actionable context on its own. Route it to the
    # PLANNER so it asks a focused clarification using the live
    # scene instead of the chat handler chattering generically.
    # --------------------------------------------------------
    is_vague = (
        len(prompt.split()) <= 3
        and not answering_pending_question
        and not _CONVERSATION_PATTERN.match(prompt)
        and not _EDIT_VERB_PATTERN.search(prompt)
    )

    if not prompt:
        return jsonify({"error": "Prompt is required."}), 400

    # --------------------------------------------------------
    # CHAT GATE — questions / small talk never reach the planner.
    # Runs BEFORE the scene requirement so pure questions work
    # even without scene state. Skipped when the user is answering
    # a question Autoquence asked them, and for vague short input
    # (which the planner should clarify, not chat about).
    # --------------------------------------------------------
    if (
        OPENROUTER_API_KEY
        and not answering_pending_question
        and not is_vague
        and looks_like_conversation(prompt)
    ):
        # Pass the real conversation history so follow-up answers
        # keep their context instead of being treated as one-offs.
        chat_reply, chat_reasoning = answer_conversation(
            prompt, history=history, available_fonts=None
        )
        reply = _clean_reply_line(chat_reply)

        print(f"[CHAT] Routed as conversation: '{prompt[:60]}' -> '{reply[:60]}'")
        return jsonify(conversation_response(reply, reasoning_details=chat_reasoning))

    # Vague input with no scene yet: nothing to clarify against —
    # ask deterministically what they want (no AI call needed).
    if is_vague and not scene_data:
        return jsonify({
            "response_type": "clarification",
            "message": (
                f"I'm not sure what to do with \"{prompt}\". "
                "Tell me the change you want, e.g. "
                "\"add a top banner saying SUBSCRIBE\" or "
                "\"crop the video to 9:16\"."
            ),
            "intent_summary": "Vague input without a video loaded.",
            "plan": {"actions": [], "assumptions": []},
        })

    if not scene_data:
        return jsonify({"error": "Scene state is required."}), 400

    try:
        scene = build_scene(scene_data)

        result = autoquence_ai.process(
            user_prompt=prompt,
            scene=scene,
            available_assets=data.get("available_assets") or [],
            available_fonts=data.get("available_fonts") or [],
            apply_plan=True,
            conversation_history=history,
            answering_pending_question=answering_pending_question,
            pending_question=pending_question or None,
            vague_prompt=is_vague,
        )

        normalized = normalize_result(result)

        # The browser is the live editor source of truth. The server's
        # abstract scene is returned for debugging/context, but the browser
        # executes the actions itself.
        normalized["debug"] = {
            "received_version": scene.version,
            "received_elements": len(scene.elements),
            "action_count": len(normalized["plan"]["actions"]),
        }

        print(
            "[Autoquence]",
            json.dumps(
                {
                    "prompt": prompt,
                    "actions": normalized["plan"]["actions"],
                },
                ensure_ascii=False,
            ),
        )

        return jsonify(normalized)

    except Exception as exc:
        import traceback
        traceback.print_exc()

        return jsonify({
            "error": "Autoquence failed.",
            "details": str(exc),
        }), 500


# ============================================================
# LEGACY EDIT-JSON ENDPOINT
#
# Kept so an older canvas page cannot accidentally break the app.
# The new V3 canvas should use /api/autoquence/edit.
# ============================================================

@app.post("/edit-json/<filename>")
def legacy_edit_json(filename):
    data = request.get_json(silent=True) or {}
    prompt = str(data.get("prompt") or request.form.get("prompt") or "").strip()

    if not prompt:
        return jsonify({"error": "Prompt is required."}), 400

    # Do not route new prompts through a second incompatible LLM format.
    # Build a minimal scene and use the same V3 planner.
    try:
        result = autoquence_ai.process(
            user_prompt=prompt,
            scene=SceneGraph(
                video=VideoInfo(
                    width=0,
                    height=0,
                    duration=0,
                    filename=filename,
                ),
                elements=[],
                canvas={"aspect_ratio": "original"},
                references={},
                version=0,
            ),
            available_assets=[],
            available_fonts=[],
            apply_plan=True,
        )

        normalized = normalize_result(result)

        # Legacy clients historically expect an array directly.
        return jsonify(normalized["plan"]["actions"])

    except Exception as exc:
        return jsonify({
            "error": str(exc),
        }), 500


# ============================================================
# FFPROBE
# ============================================================

def probe_dimensions(input_path: Path):
    """
    Rotation-aware dimension probe.

    Browsers auto-apply rotation metadata, so videoWidth/videoHeight in the
    preview are the ROTATED dimensions, while ffprobe's raw stream
    width/height are the CODED dimensions. To keep crop math consistent
    between preview and export, width/height are swapped here when the
    display matrix rotates by 90/270 degrees.

    Returns (width, height) in the same orientation the browser sees.
    """
    width = None
    height = None
    rotation = 0.0

    try:
        command = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_streams",
            "-of", "json",
            str(input_path),
        ]
        proc = subprocess.run(command, capture_output=True, text=True)
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        if streams:
            stream = streams[0]
            width = safe_int(stream.get("width"), 0) or None
            height = safe_int(stream.get("height"), 0) or None

            # Newer ffprobe: display matrix side data (rotation in degrees,
            # may be negative for the same visual result).
            for side_data in stream.get("side_data_list") or []:
                rot = side_data.get("rotation")
                if rot is not None:
                    rotation = safe_float(rot, 0.0)

            # Legacy ffprobe: rotation in stream tags ("90", "-90", ...).
            tags = stream.get("tags") or {}
            if "rotate" in tags:
                rotation = safe_float(tags.get("rotate"), rotation)
    except Exception:
        width = None
        height = None

    if width is None or height is None:
        # Fallback: legacy CSV probe (no rotation information).
        commands = [
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=s=x:p=0",
                str(input_path),
            ],
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=s=x:p=0",
                str(input_path),
            ],
        ]

        last = None

        for command in commands:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
            )
            last = proc

            for line in proc.stdout.splitlines():
                match = re.search(r"(\d+)\s*x\s*(\d+)", line.strip())
                if match:
                    return int(match.group(1)), int(match.group(2))

        raise RuntimeError(
            "Could not determine video dimensions.\n"
            + (last.stderr if last else "")
        )

    # 90/270 rotation: the browser (and ffmpeg's autorotation) present the
    # video with swapped dimensions, so normalize to that orientation.
    normalized_rotation = rotation % 360.0
    if abs(normalized_rotation - 90.0) < 1.0 or abs(normalized_rotation - 270.0) < 1.0:
        width, height = height, width

    return width, height


def probe_duration(input_path: Path):
    """
    Fast ffprobe call returning the container duration in seconds.
    Used to translate FFmpeg's progress ticks into a completion %.
    """
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(input_path),
    ]

    proc = subprocess.run(command, capture_output=True, text=True)

    for line in proc.stdout.splitlines():
        try:
            return float(line.strip())
        except ValueError:
            continue

    raise RuntimeError(
        "Could not determine video duration. "
        + (proc.stderr or "")[-500:]
    )


# ============================================================
# ============================================================
# BANNER PLACEMENT ANALYSIS
#
# When the user asks for a banner without saying top or bottom,
# we analyze a few sample frames of the video and measure the
# visual "busyness" (edge density) of the top third vs the
# bottom third. The banner is auto-placed in the emptier region
# so it is less likely to cover the subject.
# ============================================================

def _region_busyness(img, y0_frac, y1_frac):
    """Average edge intensity in a horizontal band of the image (0-1 fracs)."""
    w, h = img.size
    band = img.crop((0, int(h * y0_frac), w, int(h * y1_frac)))
    edges = band.filter(ImageFilter.FIND_EDGES).convert("L")
    data = edges.getdata()
    if not data:
        return 0.0
    return sum(data) / len(data)


def analyze_banner_region(video_path):
    """
    Returns {"position": "top"|"bottom", "top": score, "bottom": score}.
    Lower score = emptier region = better banner spot.
    Extracts up to 4 small frames spread through the video.
    Falls back to "top" on any failure (matches the old default).
    """
    default = {"position": "top", "top": None, "bottom": None}
    try:
        with tempfile.TemporaryDirectory() as td:
            # Small grayscale frames (64px wide) keep extraction + analysis fast.
            cmd = [
                "ffmpeg", "-y", "-i", str(video_path),
                "-vf", "fps=1/2,scale=64:-1,format=gray",
                "-frames:v", "4",
                os.path.join(td, "f%03d.png"),
            ]
            subprocess.run(
                cmd, capture_output=True, timeout=30, check=False
            )
            frames = sorted(Path(td).glob("f*.png"))
            if not frames:
                return default

            top_scores, bottom_scores = [], []
            for frame in frames:
                try:
                    img = Image.open(frame).convert("L")
                except Exception:
                    continue
                # Skip the outer 8% so black bars/rounded corners don't skew it.
                top_scores.append(_region_busyness(img, 0.08, 0.40))
                bottom_scores.append(_region_busyness(img, 0.60, 0.92))

            if not top_scores:
                return default

            top = sum(top_scores) / len(top_scores)
            bottom = sum(bottom_scores) / len(bottom_scores)
            # A small bias toward top keeps the historical default when
            # the two regions are near-identical (within 5%).
            return {
                "position": "bottom" if bottom < top * 0.95 else "top",
                "top": round(top, 2),
                "bottom": round(bottom, 2),
            }
    except Exception as e:
        print(f"[BANNER-ANALYZE] failed: {e}")
        return default


@app.get("/api/video/analyze-banner/<filename>")
def analyze_banner_endpoint(filename):
    safe = secure_filename(filename)
    video_path = UPLOAD_FOLDER / safe
    if not video_path.exists():
        return jsonify({"error": "Video not found."}), 404
    return jsonify(analyze_banner_region(video_path))


# ============================================================
# FFMPEG HELPERS
# ============================================================
# FFMPEG HELPERS
# ============================================================

COLOR_NAMES = {
    "white": "ffffff",
    "black": "000000",
    "red": "ff0000",
    "blue": "0000ff",
    "green": "00aa00",
    "yellow": "ffff00",
    "orange": "ffa500",
    "purple": "800080",
    "pink": "ff69b4",
    "gray": "808080",
    "grey": "808080",
}


def ffmpeg_color(value, default="ffffff"):
    if not value:
        return default

    value = str(value).strip()

    if value.lower() in COLOR_NAMES:
        return COLOR_NAMES[value.lower()]

    if value.startswith("#"):
        value = value[1:]

    # Basic #RRGGBB only. If invalid, use the fallback.
    if re.fullmatch(r"[0-9a-fA-F]{6}", value):
        return value.lower()

    return default


def ffmpeg_escape_text(text):
    """
    Escape text for the inline drawtext 'text=' filter option.
    """
    text = str(text or "")
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "\\'")
    text = text.replace(":", "\\:")
    text = text.replace("%", "\\%")
    text = text.replace("[", "\\[")
    text = text.replace("]", "\\]")
    text = text.replace("\r", "")
    return text


def ffmpeg_textfile_escape(text):
    """
    Escape text that will be written to a drawtext 'textfile=' file.

    Unlike the inline 'text=' value, a textfile is read verbatim, so
    colons / quotes / brackets do NOT need escaping. We still harden the
    two characters that FFmpeg's text-expansion feature interprets —
    backslash (turns "\\n" etc. from literal text into control codes) and
    percent (starts "%{...}" expansion). False-negative (e.g. a font that
    has no '%') is harmless; this only ever prevents mis-rendering.
    """
    text = str(text or "")
    text = text.replace("\\", "\\\\")
    text = text.replace("%", "\\%")
    text = text.replace("\r", "")
    return text


def wrap_text(text, max_chars):
    if not text:
        return [""]

    lines = []

    for paragraph in str(text).split("\n"):
        words = paragraph.split()

        if not words:
            lines.append("")
            continue

        current = ""

        for word in words:
            candidate = f"{current} {word}".strip()

            if current and len(candidate) > max_chars:
                lines.append(current)
                current = word
            else:
                current = candidate

        if current:
            lines.append(current)

    return lines or [""]


# ============================================================
# TEXT MEASUREMENT HELPERS
#
# Accurate pixel measurement fixes the long-standing bug where
# long sentences overflowed the banner on export: the old code
# guessed character width as 0.6*fontSize and never grew the
# banner height to fit wrapped lines.
# ============================================================

_FONT_CACHE = {}


def _load_pil_font(font_path, size):
    """Load and cache a PIL font. Returns None if unavailable."""
    if not ImageFont or not font_path or not os.path.exists(str(font_path)):
        return None
    key = (str(font_path), int(size))
    cached = _FONT_CACHE.get(key)
    if cached is None:
        try:
            cached = ImageFont.truetype(str(font_path), int(size))
        except Exception:
            return None
        _FONT_CACHE[key] = cached
    return cached


def _text_width(text, font_path, size):
    """
    Pixel width of a string.
    Uses real font metrics when possible; falls back to a
    conservative 0.62em-per-character estimate.
    """
    font = _load_pil_font(font_path, size)
    if font is not None:
        try:
            return float(font.getlength(str(text)))
        except Exception:
            pass
    return len(str(text)) * size * 0.62


def wrap_text_px(text, max_width, font_path, size):
    """
    Word-wrap text to a pixel width using real font metrics.
    Long unbreakable words are hard-split so they never overflow.
    """
    if not text:
        return [""]

    lines = []

    for paragraph in str(text).split("\n"):
        words = paragraph.split()

        if not words:
            lines.append("")
            continue

        current = ""

        for word in words:
            candidate = f"{current} {word}".strip()

            if current and _text_width(candidate, font_path, size) > max_width:
                lines.append(current)
                current = ""
                candidate = word

            # Hard-break words that are wider than the whole line
            while _text_width(candidate, font_path, size) > max_width and len(candidate) > 1:
                lo, hi = 1, len(candidate)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if _text_width(candidate[:mid], font_path, size) <= max_width:
                        lo = mid
                    else:
                        hi = mid - 1
                lines.append(candidate[:lo])
                candidate = candidate[lo:]

            current = candidate

        if current:
            lines.append(current)

    return lines or [""]


def find_font(font_name=None):
    """
    Try to honor the font requested by the user.
    """
    requested = str(font_name or "").lower().replace(" ", "")

    candidates = []

    if "impact" in requested:
        candidates += [
            "C:/Windows/Fonts/impact.ttf",
            "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
        ]

    if "arial" in requested:
        candidates += [
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ]

    # Bundled font first if present.
    candidates += [
        str(Path(app.root_path) / "static/fonts/Poppins-Bold.ttf"),
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ]

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    return None


def ffmpeg_font_path(path):
    if not path:
        return ""
    return str(path).replace("\\", "/").replace(":", "\\:")


def ffmpeg_filter_path(path):
    """
    Escape a Windows file path for use as a value inside an FFmpeg
    filtergraph (e.g. drawtext textfile=...). Converts to forward slashes
    and escapes the drive colon and any quote/bracket chars so the filter
    parser doesn't split on "C:" or choke on special characters.
    """
    if not path:
        return ""
    return (
        str(path)
        .replace("\\", "/")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


# ============================================================
# EXPORT
# ============================================================

# ============================================================
# ASYNC EXPORT JOBS
#
# FFmpeg exports previously ran synchronously inside the HTTP request:
# long renders held a worker thread the whole time, could be killed by
# gunicorn's worker timeout, and the browser had to fake progress.
#
# Now: POST /edit-video validates + builds the command quickly, hands
# it to a background thread, and returns {job_id}. The frontend polls
# GET /export/status/<job_id> for REAL render progress parsed from
# FFmpeg's -progress output.
# ============================================================

_export_jobs = {}
_export_jobs_lock = threading.Lock()
_EXPORT_JOB_TTL = 30 * 60  # seconds


def _create_export_job():
    job_id = uuid.uuid4().hex
    with _export_jobs_lock:
        _export_jobs[job_id] = {
            "status": "processing",
            "progress": 0.0,
            "output_file": None,
            "error": None,
            "created": time.time(),
        }
    return job_id


def _update_export_job(job_id, **fields):
    with _export_jobs_lock:
        job = _export_jobs.get(job_id)
        if job is not None:
            job.update(fields)


def _get_export_job(job_id):
    with _export_jobs_lock:
        job = _export_jobs.get(job_id)
        return dict(job) if job else None


def _prune_export_jobs():
    """Drop stale jobs so the in-memory store cannot grow unbounded."""
    cutoff = time.time() - _EXPORT_JOB_TTL
    with _export_jobs_lock:
        for key in [k for k, v in _export_jobs.items() if v["created"] < cutoff]:
            _export_jobs.pop(key, None)


@app.post("/edit-video/<filename>")
def edit_video(filename):
    data = request.get_json(silent=True) or {}
    raw_edits = data.get("edits") or []

    if not isinstance(raw_edits, list):
        return jsonify({"error": "edits must be an array."}), 400

    safe_name = safe_filename(filename)
    input_path = UPLOAD_FOLDER / safe_name

    if not input_path.exists():
        return jsonify({"error": "Input video not found."}), 404

    try:
        width, height = probe_dimensions(input_path)
    except Exception as exc:
        return jsonify({
            "error": "Could not determine video dimensions.",
            "details": str(exc),
        }), 500

    edits = []

    # Normalize browser edits before rendering.
    for raw in raw_edits:
        if not isinstance(raw, dict):
            continue

        edit = dict(raw)

        # Banner compatibility.
        if edit.get("type") == "banner":
            position = str(
                edit.get("position") or "top"
            ).lower()

            edit["type"] = (
                "bottom_banner"
                if "bottom" in position
                else "top_banner"
            )

        edits.append(edit)

    # --------------------------------------------------------
    # State
    # --------------------------------------------------------

    crop = next(
        (e for e in edits if e.get("type") == "crop"),
        None,
    )

    trim = next(
        (e for e in edits if e.get("type") == "trim"),
        None,
    )

    speed_edit = next(
        (e for e in edits if e.get("type") in {"speed", "set_speed"}),
        None,
    )

    speed = 1.0

    if speed_edit:
        speed = safe_float(
            first_defined(
                speed_edit.get("speed"),
                speed_edit.get("value"),
            ),
            1.0,
        )

        if speed <= 0:
            speed = 1.0

    # --------------------------------------------------------
    # Crop
    # --------------------------------------------------------

    crop_filter = None
    effective_w = width
    effective_h = height

    if crop:
        nx = max(0.0, min(1.0, safe_float(crop.get("nx"), 0.0)))
        ny = max(0.0, min(1.0, safe_float(crop.get("ny"), 0.0)))
        nw = max(0.01, min(1.0, safe_float(crop.get("nw"), 1.0)))
        nh = max(0.01, min(1.0, safe_float(crop.get("nh"), 1.0)))

        crop_w = max(2, int(width * nw))
        crop_h = max(2, int(height * nh))
        crop_x = int(width * nx)
        crop_y = int(height * ny)

        crop_w -= crop_w % 2
        crop_h -= crop_h % 2
        crop_x = max(0, min(crop_x, width - crop_w))
        crop_y = max(0, min(crop_y, height - crop_h))

        crop_filter = f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y}"
        effective_w = crop_w
        effective_h = crop_h

    # --------------------------------------------------------
    # Build filter list
    #
    # Banners are overlays, not padding.
    #
    # This is intentional. It keeps the live preview and final
    # export consistent and avoids the old "black bars / zoom"
    # behavior caused by changing the output canvas whenever a
    # banner was added.
    # --------------------------------------------------------

    vf_parts = []

    # Temp drawtext textfiles created for banner text (see the drawtext
    # loops below). Passed to the async FFmpeg thread so it can remove
    # them after the process exits.
    banner_text_files = []

    if speed != 1.0:
        vf_parts.append(f"setpts={1.0 / speed:.8f}*PTS")

    if crop_filter:
        vf_parts.append(crop_filter)

    top_banners = [
        e for e in edits
        if e.get("type") == "top_banner"
    ]

    bottom_banners = [
        e for e in edits
        if e.get("type") == "bottom_banner"
    ]

    overlay_texts = [
        e for e in edits
        if e.get("type") == "overlay_text"
    ]

    # --------------------------------------------------------
    # Banner geometry
    # --------------------------------------------------------

    def make_banner_geometry(item, default_height):
        """
        Text-driven banner geometry.

        The banner height is DERIVED FROM THE TEXT:
          1. Wrap the text using real font metrics (PIL).
          2. Compute the height needed for all wrapped lines.
          3. Grow the banner to fit; if it would exceed the max
             banner fraction of the video, shrink the font until
             it fits.

        This guarantees long sentences never overflow the banner.
        """
        requested_fs = max(
            10,
            safe_int(
                first_defined(
                    item.get("font_size"),
                    item.get("fontSize"),
                    default_height * 0.35,
                ),
                24,
            ),
        )

        # Output-space geometry sent by the browser (measured by Konva in the
        # preview). The preview measures text at the 1080-wide OUTPUT
        # resolution, so convert to the source-resolution filter space here.
        out_fs = safe_float(item.get("font_size_out"), 0)
        if out_fs > 0:
            requested_fs = max(6, int(out_fs * effective_w / 1080.0))

        padding = max(
            4,
            safe_int(item.get("padding"), int(requested_fs * 0.45)),
        )

        explicit_height = safe_float(item.get("height"), 0)

        if 0 < explicit_height < 1:
            min_h = int(effective_h * explicit_height)
        elif explicit_height >= 1:
            min_h = int(explicit_height)
        else:
            min_h = int(requested_fs * 1.8 + padding * 2)

        min_h = max(
            requested_fs + padding * 2,
            min(min_h, max(1, effective_h)),
        )

        text = str(
            first_defined(
                item.get("text"),
                item.get("content"),
                "",
            ) or ""
        )

        font = find_font(
            first_defined(
                item.get("font_family"),
                item.get("fontFamily"),
                item.get("font"),
            )
        )

        # Layout constants (must match the drawtext line_spacing below)
        LINE_H = 1.25
        LINE_SPACING = 4
        MAX_BANNER_FRAC = 0.4   # banner never taller than 40% of the video
        MIN_FONT = 10

        cap_h = max(min_h, int(effective_h * MAX_BANNER_FRAC))
        usable_w = max(10, effective_w - padding * 2)

        # Try the requested font size first; shrink until the wrapped
        # text fits inside the height cap.
        chosen_fs = requested_fs
        lines = wrap_text_px(text, usable_w, font, chosen_fs) if text else [""]
        needed_h = int(
            len(lines) * chosen_fs * LINE_H
            + (len(lines) - 1) * LINE_SPACING
            + padding * 2
        )

        while needed_h > cap_h and chosen_fs > MIN_FONT:
            chosen_fs = max(MIN_FONT, int(chosen_fs * 0.85))
            lines = wrap_text_px(text, usable_w, font, chosen_fs) if text else [""]
            needed_h = int(
                len(lines) * chosen_fs * LINE_H
                + (len(lines) - 1) * LINE_SPACING
                + padding * 2
            )

        # Final height: at least the intended strip height, big enough
        # for every wrapped line, never taller than the video.
        banner_h = max(min_h, needed_h)
        banner_h = min(banner_h, max(1, effective_h))

        # --------------------------------------------------------
        # Browser-measured lines (Konva typography engine).
        #
        # When the client sends `lines` (the ACTUAL wrapped lines the
        # preview displays), use them VERBATIM — no PIL re-wrapping, no
        # shrink loop — so line breaks are pixel-identical to the
        # preview. Line spacing derives from the preview line height.
        # --------------------------------------------------------
        line_spacing = LINE_SPACING
        exact_lines = item.get("lines")
        if isinstance(exact_lines, list) and exact_lines:
            lines = [str(l) for l in exact_lines]
            line_height = safe_float(item.get("line_height"), 1.0)
            if line_height > 1.0 and chosen_fs > 0:
                line_spacing = max(0, int(round((line_height - 1.0) * chosen_fs)))

        # Banner height from the preview.
        #
        # Preferred: `height_frac` — the banner height as a fraction of the
        # PICTURE height (the fitted video rect in the preview). This is
        # letterbox-proof: multiplying by effective_h (the cropped frame
        # height) yields exactly the same banner-to-picture proportion the
        # preview shows, even when the picture doesn't span the full
        # 1080x1920 output frame.
        height_frac = safe_float(item.get("height_frac"), 0.0)
        if 0.0 < height_frac <= 1.0:
            banner_h = max(
                2,
                min(int(round(height_frac * effective_h)), max(1, effective_h)),
            )
        else:
            # Legacy fallback: exact banner height measured in the preview
            # (output px), assuming the picture spans the full frame height.
            exact_out_h = safe_float(item.get("height_px"), 0)
            if exact_out_h > 0:
                banner_h = max(
                    2,
                    min(
                        int(exact_out_h * effective_h / 1920.0),
                        max(1, effective_h),
                    ),
                )

        # Safety valve for pathological banner text: cap the number of
        # wrapped lines (the textfile drawtext can handle huge strings, but a
        # million-line banner would still be absurd). Realistic banners stay
        # far below this, so exporting a long sentence is unaffected.
        MAX_LINES = 48
        if len(lines) > MAX_LINES:
            lines = lines[:MAX_LINES]
            lines[-1] = str(lines[-1]).rstrip() + "..."

        return {
            "text": text,
            "lines": lines,
            "font_size": chosen_fs,
            "padding": padding,
            "height": banner_h,
            "line_spacing": line_spacing,
            "bg": ffmpeg_color(
                first_defined(
                    item.get("bg_color"),
                    item.get("backgroundColor"),
                    item.get("background_color"),
                    item.get("fill"),
                ),
                "ffffff",
            ),
            "fg": ffmpeg_color(
                first_defined(
                    item.get("text_color"),
                    item.get("textColor"),
                    item.get("color"),
                ),
                "000000",
            ),
            "font": font,
            # Baked-in black-bar offsets are always 0 so banners sit exactly
            # on the top/bottom edge of the video — matching the preview's
            # bannerSnapY() and the client export engine, which both ignore
            # bars. Kept as explicit 0.0 above (rather than removed) so the
            # downstream drawbox/drawtext offsets stay trivially 0.
            "top_bar": 0.0,
            "bottom_bar": 0.0,
        }

    top_geometry = [
        make_banner_geometry(item, 56)
        for item in top_banners
    ]

    bottom_geometry = [
        make_banner_geometry(item, 52)
        for item in bottom_banners
    ]

    # --------------------------------------------------------
    # Banner backgrounds
    # --------------------------------------------------------

    for banner in top_geometry:
        # Banners are always flush to the video edge (top_bar = 0 → top_off
        # is trivially 0), matching the preview and client export engine.
        top_off = max(0, int(banner["top_bar"] * effective_h))

        vf_parts.append(
            "drawbox="
            f"x=0:y={top_off}:w=iw:h={banner['height']}:"
            f"color={banner['bg']}@1.0:t=fill"
        )

    for banner in bottom_geometry:
        # Banners are always flush to the video edge (bottom_bar = 0 →
        # bot_off is trivially 0), matching the preview and client engine.
        bot_off = max(0, int(banner["bottom_bar"] * effective_h))
        y = max(0, effective_h - banner["height"] - bot_off)

        vf_parts.append(
            "drawbox="
            f"x=0:y={y}:w=iw:h={banner['height']}:"
            f"color={banner['bg']}@1.0:t=fill"
        )

    # --------------------------------------------------------
    # Banner text
    # --------------------------------------------------------

    for banner in top_geometry:
        if not banner["text"]:
            continue

        # Write the multi-line banner text to a temp file and reference it
        # via textfile=. A single inline text='...' drawtext would embed the
        # whole string in the FFmpeg command line, which on Windows is capped
        # at 32767 chars — very long banner text crashed export. File content
        # is read verbatim, so there is no command-line length limit.
        text_file = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", encoding="utf-8", delete=False
        )
        text_file.write(
            "\n".join(ffmpeg_textfile_escape(line) for line in banner["lines"])
        )
        text_file.close()
        text_path = ffmpeg_filter_path(text_file.name)
        banner_text_files.append(text_file.name)

        top_off = max(0, int(banner["top_bar"] * effective_h))
        y = top_off + banner["height"] / 2

        font_clause = (
            f":fontfile='{ffmpeg_font_path(banner['font'])}'"
            if banner["font"]
            else ""
        )

        vf_parts.append(
            "drawtext="
            f"textfile='{text_path}'"
            f"{font_clause}"
            f":fontsize={banner['font_size']}"
            f":fontcolor={banner['fg']}"
            f":x=(w-text_w)/2"
            f":y={y}-text_h/2"
            f":line_spacing={banner.get('line_spacing', 4)}"
        )

    for banner in bottom_geometry:
        if not banner["text"]:
            continue

        # See the top-banner textfile comment — same approach: write to a temp
        # file so arbitrarily long banner text can't overflow the Windows
        # 32767-char command line.
        text_file = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", encoding="utf-8", delete=False
        )
        text_file.write(
            "\n".join(ffmpeg_textfile_escape(line) for line in banner["lines"])
        )
        text_file.close()
        text_path = ffmpeg_filter_path(text_file.name)
        banner_text_files.append(text_file.name)

        bot_off = max(0, int(banner["bottom_bar"] * effective_h))
        y = effective_h - banner["height"] / 2 - bot_off

        font_clause = (
            f":fontfile='{ffmpeg_font_path(banner['font'])}'"
            if banner["font"]
            else ""
        )

        vf_parts.append(
            "drawtext="
            f"textfile='{text_path}'"
            f"{font_clause}"
            f":fontsize={banner['font_size']}"
            f":fontcolor={banner['fg']}"
            f":x=(w-text_w)/2"
            f":y={y}-text_h/2"
            f":line_spacing={banner.get('line_spacing', 4)}"
        )

    # --------------------------------------------------------
    # Overlay text
    # --------------------------------------------------------

    for item in overlay_texts:
        text = str(
            first_defined(
                item.get("text"),
                item.get("content"),
                "",
            ) or ""
        )

        if not text:
            continue

        out_fs = safe_float(item.get("font_size_out"), 0)
        if out_fs > 0:
            # Exact font size measured in the preview (output px).
            font_size = max(6, int(out_fs * effective_w / 1080.0))
        else:
            font_size = max(
                10,
                int(
                    safe_float(
                        first_defined(
                            item.get("font_size"),
                            item.get("fontSize"),
                        ),
                        28,
                    )
                    * effective_w
                    / 720
                ),
            )

        chars = max(
            10,
            int(
                effective_w /
                max(1, font_size * 0.6)
            ),
        )

        lines = wrap_text(text, chars)
        escaped = "\\n".join(
            ffmpeg_escape_text(line)
            for line in lines
        )

        # Exact normalized position (sent by the browser) beats the
        # lossy top/center/bottom keyword — x/y are the element's
        # top-left corner, 0-1, resolved against the video viewport
        # exactly like getWorldRect() in the preview.
        if item.get("x") is not None and item.get("y") is not None:
            nx = max(0.0, min(1.0, safe_float(item.get("x"), 0.0)))
            ny = max(0.0, min(1.0, safe_float(item.get("y"), 0.0)))
            nw = max(0.0, min(1.0, safe_float(item.get("w"), 1.0)))
            nh = max(0.0, min(1.0, safe_float(item.get("h"), 0.12)))

            x_expr = (
                f"min(max({nx:.4f}*w+({nw:.4f}*w-text_w)/2\\,0)\\,w-text_w)"
            )
            y_expr = (
                f"min(max({ny:.4f}*h+({nh:.4f}*h-text_h)/2\\,0)\\,h-text_h)"
            )
            x_clause = f":x='{x_expr}'"
        else:
            position = str(
                item.get("position") or "bottom"
            ).lower()

            if "top" in position:
                y_expr = "20"
            elif "center" in position or "middle" in position:
                y_expr = f"(h-text_h)/2"
            else:
                y_expr = f"h-text_h-20"
            x_clause = ":x=(w-text_w)/2"

        color = ffmpeg_color(
            first_defined(
                item.get("text_color"),
                item.get("textColor"),
                item.get("color"),
            ),
            "ffffff",
        )

        font = find_font(
            first_defined(
                item.get("font_family"),
                item.get("fontFamily"),
                item.get("font"),
            )
        )

        font_clause = (
            f":fontfile='{ffmpeg_font_path(font)}'"
            if font
            else ""
        )

        vf_parts.append(
            "drawtext="
            f"text='{escaped}'"
            f"{font_clause}"
            f":fontsize={font_size}"
            f":fontcolor={color}"
            f"{x_clause}"
            f":y='{y_expr}'"
            f":line_spacing=4"
        )

    # --------------------------------------------------------
    # Shapes (drawbox overlays positioned exactly like the preview)
    # --------------------------------------------------------
    # Note: drawbox cannot express rounded corners or borders, so a
    # previewed rounded/bordered shape renders as a plain filled box
    # in the FFmpeg fallback. The browser (WebCodecs) export path
    # reproduces them fully.

    for item in [e for e in edits if e.get("type") == "shape"]:
        nx = max(0.0, min(1.0, safe_float(item.get("x"), 0.0)))
        ny = max(0.0, min(1.0, safe_float(item.get("y"), 0.0)))
        nw = max(0.01, min(1.0, safe_float(item.get("w"), 0.3)))
        nh = max(0.01, min(1.0, safe_float(item.get("h"), 0.2)))

        box_x = max(0, int(nx * effective_w))
        box_y = max(0, int(ny * effective_h))
        box_w = max(2, int(nw * effective_w))
        box_h = max(2, int(nh * effective_h))
        box_x = max(0, min(box_x, max(0, effective_w - box_w)))
        box_y = max(0, min(box_y, max(0, effective_h - box_h)))

        opacity = max(0.0, min(1.0, safe_float(item.get("opacity"), 1.0)))
        shape_color = ffmpeg_color(
            first_defined(
                item.get("fill"),
                item.get("color"),
                "ffffff",
            )
        )

        vf_parts.append(
            "drawbox="
            f"x={box_x}:y={box_y}:w={box_w}:h={box_h}:"
            f"color={shape_color}@{opacity:.2f}:t=fill"
        )

    # --------------------------------------------------------
    # Images / logos — NOT reproducible by the FFmpeg fallback
    # --------------------------------------------------------

    dropped = [
        str(e.get("type"))
        for e in edits
        if e.get("type") in {"image", "logo"}
    ]
    if dropped:
        print(
            "[Autoquence] WARNING: image/logo elements cannot be rendered "
            "by the FFmpeg fallback and were dropped — use the browser "
            "(WebCodecs) export for those:",
            dropped,
        )

    # --------------------------------------------------------
    # Fixed 9:16 output frame (UNCONDITIONAL, FIT MODE)
    #
    # Every export must come back 1080x1920 regardless of what the
    # client sends. The picture is ALWAYS FULLY VISIBLE: scaled to FIT
    # inside the frame and centered — mismatched aspect ratios are
    # letterboxed/pillarboxed with bars filled by the scene background
    # color, never zoom-cropped. Clients can opt OUT of reframing by
    # explicitly sending { type: "resize_canvas",
    # aspect_ratio: "original" }. Appended LAST so banner/text
    # y-expressions above still resolve against pre-pad dimensions.
    # --------------------------------------------------------

    canvas_resize = next(
        (e for e in edits if e.get("type") == "resize_canvas"),
        None,
    )

    requested_ratio = str(
        first_defined(
            canvas_resize.get("aspect_ratio"),
            canvas_resize.get("ratio"),
            "9:16",
        )
    ).lower() if canvas_resize else "9:16"

    # Scene background color fills the letterbox bars.
    bg_edit = next(
        (e for e in edits if e.get("type") == "background"),
        None,
    )

    pad_color = ffmpeg_color(
        first_defined(
            bg_edit.get("color") if bg_edit else None,
            canvas_resize.get("color") if canvas_resize else None,
        ),
        "000000",
    )

    if requested_ratio not in {"original", "source", "passthrough"}:
        vf_parts.append("scale=1080:1920:force_original_aspect_ratio=decrease")
        vf_parts.append(
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color={pad_color}"
        )
        # Normalize the sample aspect ratio so the final MP4's DISPLAY
        # aspect ratio is also exactly 9:16 (anamorphic sources otherwise
        # yield 1080x1920 pixels with a distorted DAR).
        vf_parts.append("setsar=1")

    # --------------------------------------------------------
    # Trim
    # --------------------------------------------------------

    trim_start = None
    trim_end = None

    if trim:
        trim_start = max(
            0.0,
            safe_float(
                first_defined(
                    trim.get("start"),
                    trim.get("start_time"),
                ),
                0.0,
            ),
        )

        trim_end_value = first_defined(
            trim.get("end"),
            trim.get("end_time"),
        )

        if trim_end_value is not None:
            trim_end = max(
                trim_start,
                safe_float(trim_end_value, trim_start),
            )

    # --------------------------------------------------------
    # FFmpeg command
    # --------------------------------------------------------

    base = input_path.stem
    ext = input_path.suffix or ".mp4"

    output_filename = f"{base}_edited{ext}"
    output_path = OUTPUT_FOLDER / output_filename

    command = ["ffmpeg", "-y"]

    if trim_start is not None and trim_start > 0:
        command += ["-ss", str(trim_start)]

    command += ["-i", str(input_path)]

    if trim_end is not None and trim_end > trim_start:
        command += ["-t", str(trim_end - trim_start)]

    if vf_parts:
        command += [
            "-vf",
            ",".join(vf_parts),
        ]

    # Audio speed.
    if speed != 1.0:
        atempo = []
        remaining = speed

        if remaining > 2.0:
            while remaining > 2.0:
                atempo.append("atempo=2.0")
                remaining /= 2.0
            atempo.append(f"atempo={remaining:.6f}")

        elif remaining < 0.5:
            while remaining < 0.5:
                atempo.append("atempo=0.5")
                remaining *= 2.0
            atempo.append(f"atempo={remaining:.6f}")

        else:
            atempo.append(f"atempo={remaining:.6f}")

        command += ["-af", ",".join(atempo)]
        audio_codec = "aac"
    else:
        audio_codec = "copy"

    # Fast path: when nothing visual changed (pure trim / no edits that
    # touch video frames), stream-copy instead of a full re-encode.
    # This makes trim-only exports effectively instant.
    if vf_parts:
        command += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            audio_codec,
            "-movflags",
            "+faststart",
        ]
    else:
        command += [
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
        ]

    # Expected render length, used to convert FFmpeg progress ticks
    # into a completion percentage for the status endpoint.
    expected_duration = 0.0

    if trim_end is not None and trim_end > trim_start:
        expected_duration = trim_end - trim_start
    elif speed != 1.0:
        try:
            expected_duration = probe_duration(input_path) / speed
        except Exception:
            expected_duration = 0.0
    elif vf_parts:
        try:
            expected_duration = probe_duration(input_path)
        except Exception:
            expected_duration = 0.0

    command += ["-progress", "pipe:1", str(output_path)]

    print("[Autoquence] FFmpeg:", " ".join(command))

    _prune_export_jobs()
    job_id = _create_export_job()

    threading.Thread(
        target=_run_ffmpeg_export,
        args=(job_id, command, output_filename),
        kwargs={
            "expected_duration": expected_duration,
            "text_files": banner_text_files,
        },
        daemon=True,
    ).start()

    return jsonify({
        "status": "started",
        "job_id": job_id,
    })


def _run_ffmpeg_export(job_id, command, output_filename, expected_duration=0.0, text_files=None):
    """
    Background FFmpeg execution with real progress reporting.
    stderr goes to a temp file so the stdout progress pipe never blocks
    against a full stderr buffer.
    """
    stderr_file = None

    try:
        stderr_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
        )

        last_progress = 0.0

        for line in process.stdout:
            line = line.strip()

            if not line.startswith("out_time="):
                continue

            if expected_duration <= 0:
                continue

            try:
                hours, minutes, seconds = line.split("=", 1)[1].split(":")
                rendered = (
                    int(hours) * 3600
                    + int(minutes) * 60
                    + float(seconds)
                )
                pct = min(99.0, rendered / expected_duration * 100.0)

                if pct > last_progress:
                    last_progress = pct
                    _update_export_job(job_id, progress=pct)
            except ValueError:
                continue

        process.wait()

        stderr_file.seek(0)
        stderr_text = stderr_file.read()[-12000:]

        if process.returncode != 0:
            print("[Autoquence] Export failed:", stderr_text)
            _update_export_job(
                job_id,
                status="error",
                error=f"FFmpeg failed: {stderr_text}",
            )
            return

        _update_export_job(
            job_id,
            status="done",
            progress=100.0,
            output_file=output_filename,
        )

    except FileNotFoundError:
        _update_export_job(
            job_id,
            status="error",
            error="FFmpeg is not installed or not on PATH.",
        )
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _update_export_job(job_id, status="error", error=str(exc))
    finally:
        if stderr_file is not None:
            stderr_file.close()
        # Remove temp drawtext textfiles now that FFmpeg has exited.
        if text_files:
            for _p in text_files:
                try:
                    os.remove(_p)
                except OSError:
                    pass


@app.get("/export/status/<job_id>")
def export_status(job_id):
    job = _get_export_job(job_id)

    if job is None:
        return jsonify({"error": "Unknown export job."}), 404

    return jsonify(job)


# ============================================================
# CLIENT-EXPORT UPLOAD
#
# The browser renders + encodes the final video itself (WebCodecs /
# MediaRecorder — see static/export-engine.js) so the output matches
# the canvas pixel-for-pixel. This endpoint just stores the finished
# file in OUTPUT_FOLDER using the same "<base>_edited<ext>" naming
# convention as the FFmpeg path.
# ============================================================

@app.post("/export/upload/<filename>")
def export_upload(filename):
    input_name = safe_filename(filename)
    input_path = UPLOAD_FOLDER / input_name

    if not input_path.exists():
        return jsonify({"error": "Unknown source video."}), 404

    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "Missing 'file' field."}), 400

    ext = Path(file.filename).suffix.lower() or ".mp4"
    if ext not in {".mp4", ".webm"}:
        return jsonify({"error": f"Unsupported export format: {ext}"}), 400

    output_filename = safe_filename(f"{input_path.stem}_edited{ext}")
    output_path = OUTPUT_FOLDER / output_filename

    # Overwrite is intentional — matches FFmpeg's "-y" behavior so
    # repeat exports of the same project replace the previous file.
    file.save(output_path)

    print("[Autoquence] Client export saved:", output_path)
    return jsonify({"output_file": output_filename})


# ============================================================
# DOWNLOAD
# ============================================================

@app.get("/download/<filename>")
def download(filename):
    safe_name = safe_filename(filename)
    path = OUTPUT_FOLDER / safe_name

    if not path.exists():
        return "File not found", 404

    # Exports OVERWRITE the same filename every time, so this response
    # must never be cached: a phone holding a stale copy would keep
    # serving the old (wrongly sized) video no matter how many fresh
    # exports happen server-side.
    response = send_file(
        path,
        as_attachment=True,
        download_name=safe_name,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("AUTOQUENCE SERVER")
    print("=" * 60)
    print("OpenRouter key configured:", bool(OPENROUTER_API_KEY))
    print("Upload folder:", UPLOAD_FOLDER)
    print("Output folder:", OUTPUT_FOLDER)
    print("AI endpoint: POST /api/autoquence/edit")
    print("Export endpoint: POST /edit-video/<filename>")
    print("EXPORT MODE: forced 9:16 (1080x1920) on every export")
    print("=" * 60)

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        # Debug mode is opt-in via FLASK_DEBUG=1 — never on by default,
        # the Werkzeug debugger is a remote-code-execution risk.
        debug=os.getenv("FLASK_DEBUG") == "1",
        threaded=True,
    )