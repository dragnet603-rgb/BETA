from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file
import os
import json
import subprocess
from werkzeug.utils import secure_filename
from openai import OpenAI
import time

app = Flask(__name__)

UPLOAD_FOLDER = "static/uploads"
OUTPUT_FOLDER = "static/outputs"
PROMPTS_FOLDER = "static/promptbeta"

for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER, PROMPTS_FOLDER]:
    os.makedirs(folder, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["OUTPUT_FOLDER"] = OUTPUT_FOLDER
app.config["PROMPTS_FOLDER"] = PROMPTS_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB cap, adjust as needed

from dotenv import load_dotenv
load_dotenv()

client = OpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1"
)

@app.route("/")
def index():
    return render_template("index.html")

ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "mkv"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("video")
    if not file or file.filename == "" or not allowed_file(file.filename):
        return jsonify({"error": "Invalid file type"}), 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    return jsonify({"status": "complete", "filename": filename})


@app.route("/canvas/<filename>")
def canvas_page(filename):
    return render_template("canvas.html", filename=filename)

@app.route("/edit-json/<filename>", methods=["POST"])
def edit_json(filename):
    prompt = request.form.get("prompt")
    if not prompt:
        return "No prompt provided", 400

    timestamp = int(time.time() * 1000)
    with open(os.path.join(PROMPTS_FOLDER, f"prompt_{timestamp}.txt"), "w", encoding="utf-8") as f:
        f.write(prompt)

    system_prompt = """
Turn user text into JSON for video editing. Return ONLY a valid JSON array. No markdown, no explanation, no backticks.

Each item is one of these types: top_banner, bottom_banner, overlay_text, crop.

--- TOP BANNER ---
Adds a white bar ABOVE the video with black text (classic meme format).
{
  "type": "top_banner",
  "text": "your text here",
  "font_size": 28,
  "bg_color": "white",
  "text_color": "black",
  "padding": 20
}
padding = pixels of space above/below text inside the banner. Default 20.
font_size: 24–36 for short text, 18–26 for long text.

--- BOTTOM BANNER ---
Adds a colored bar BELOW the video with text.
{
  "type": "bottom_banner",
  "text": "caption here",
  "font_size": 24,
  "bg_color": "black",
  "text_color": "white",
  "padding": 16
}

--- OVERLAY TEXT ---
Draws text directly on top of the video at a normalized position.
{
  "type": "overlay_text",
  "text": "on-screen text",
  "position": "top" | "center" | "bottom",
  "font_size": 28,
  "text_color": "white",
  "bg_color": "black",
  "bg_opacity": 0.5
}

--- CROP ---
{
  "type": "crop",
  "aspect_ratio": "16:9" | "9:16" | "1:1" | "4:3"
}

Use top_banner for any request like "add text above", "meme caption", "title at top".
Use bottom_banner for "subtitle", "caption below", "text at bottom".
Use overlay_text for "text on video", "watermark", "label on screen".

Examples:
"add a meme caption saying hello world" →
[{"type":"top_banner","text":"hello world","font_size":28,"bg_color":"white","text_color":"black","padding":20}]

"add subtitle that says check this out" →
[{"type":"bottom_banner","text":"check this out","font_size":24,"bg_color":"black","text_color":"white","padding":16}]
"""

    response = client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
    )

    raw = response.choices[0].message.content.strip()
    print("Raw LLM response:", raw)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start == -1 or end == 0:
            return jsonify({"error": "Could not parse JSON", "raw": raw}), 500
        parsed = json.loads(raw[start:end])

    return jsonify(parsed)


def wrap_text_to_lines(text, max_chars):
    """
    Greedy word-wrap: builds lines up to ~max_chars wide.
    This is what makes drawtext actually break long captions into
    multiple lines instead of rendering one long overflowing line —
    ffmpeg's drawtext filter never wraps text on its own; you have to
    hand it literal '\\n' characters between lines.
    """
    if not text:
        return [""]

    lines = []
    for paragraph in text.split("\n"):
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


@app.route("/edit-video/<filename>", methods=["POST"])
def edit_video(filename):
    edits = request.json.get("edits", [])
    input_path = os.path.join(UPLOAD_FOLDER, filename).replace("\\", "/")

    base, ext = os.path.splitext(filename)
    output_filename = f"{base}_edited{ext}"
    output_path = os.path.join(OUTPUT_FOLDER, output_filename).replace("\\", "/")

    if not os.path.exists(input_path):
        return jsonify({"error": "Input file not found"}), 404

    # Get video dimensions.
    # Mobile-recorded files sometimes report an odd stream layout (extra
    # thumbnail/cover-art track, ambiguous "v:0" index, or a blank field on
    # one line) that a naive single split("x") can't handle. probe_dims()
    # scans every returned line for the first clean "WIDTHxHEIGHT" pair, and
    # if pinning to stream v:0 comes back empty we retry against "v" (any
    # video stream) before giving up.
    def probe_dims(select_streams):
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", select_streams,
             "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", input_path],
            capture_output=True, text=True
        )
        found = None
        for line in p.stdout.strip().splitlines():
            parts = line.strip().split("x")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                found = (int(parts[0]), int(parts[1]))
                break
        return found, p

    dims, probe = probe_dims("v:0")
    if dims is None:
        dims, probe = probe_dims("v")

    if dims is None:
        print("ffprobe stdout:", repr(probe.stdout))
        print("ffprobe stderr:", repr(probe.stderr))
        return jsonify({
            "error": "Could not determine video dimensions",
            "ffprobe_stdout": probe.stdout,
            "ffprobe_stderr": probe.stderr
        }), 500

    w, h = dims
    print(f"Video dimensions: {w}x{h}")

    # Font detection — bundled font first, OS fonts as fallback only
    font_candidates = [
        os.path.join(app.root_path, "static/fonts/Poppins-Bold.ttf"),
        "C:/Windows/Fonts/impact.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    fontfile = next((f for f in font_candidates if os.path.exists(f)), None)
    safe_font = fontfile.replace("\\", "/").replace(":", "\\:") if fontfile else None
    color_map = {
        "white": "ffffff", "black": "000000", "red": "ff0000",
        "blue": "0000ff", "green": "00ff00", "yellow": "ffff00",
        "orange": "ffa500", "purple": "800080", "gray": "808080",
    }

    def hex_color(name):
        return color_map.get(name.lower(), name.lstrip("#"))

    def sanitize(text):
        text = text.replace("\\", "\\\\")
        text = text.replace("'",  "\u2019")
        text = text.replace(":",  "\\:")
        text = text.replace("%",  "\\%")
        text = text.replace("[",  "\\[")
        text = text.replace("]",  "\\]")
        return text

    # ── Build filter graph ──────────────────────────────────────────────────
    # Strategy: collect pad_top and pad_bottom first, then build one filter chain.
    pad_top    = 0
    pad_bottom = 0
    top_banners    = []
    bottom_banners = []
    overlay_texts  = []
    crop_filter    = None

    for item in edits:
        t = item.get("type")

        if t == "crop":
            # Freeform crop: nx,ny,nw,nh are normalized 0-1 relative to video dims
            # JS always sends these now (set by the drag crop UI)
            nx = item.get("nx", 0.0)
            ny = item.get("ny", 0.0)
            nw = item.get("nw", 1.0)
            nh = item.get("nh", 1.0)

            crop_x = int(nx * w)
            crop_y = int(ny * h)
            crop_w = int(nw * w)
            crop_h = int(nh * h)

            # Ensure even dimensions (required by libx264)
            crop_w = crop_w if crop_w % 2 == 0 else crop_w - 1
            crop_h = crop_h if crop_h % 2 == 0 else crop_h - 1
            crop_x = max(0, min(crop_x, w - crop_w))
            crop_y = max(0, min(crop_y, h - crop_h))

            crop_filter = f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y}"
            print(f"Freeform crop: {crop_w}x{crop_h} at ({crop_x},{crop_y})")

        elif t == "top_banner":
            fsize   = int(item.get("font_size", 28) * (w / 720))
            padding = item.get("padding", 20)
            text    = item.get("text", "")
            chars_per_line = max(20, int(w / (fsize * 0.6)))
            lines   = wrap_text_to_lines(text, chars_per_line)
            banner_h = int(len(lines) * fsize * 1.4 + padding * 2)
            banner_h = max(banner_h, fsize + padding * 2)
            pad_top += banner_h
            top_banners.append({
                "text": text,
                "lines": lines,
                "fsize": fsize,
                "padding": padding,
                "banner_h": banner_h,
                "bg": hex_color(item.get("bg_color", "white")),
                "fg": hex_color(item.get("text_color", "black")),
                "y_offset": pad_top - banner_h,  # cumulative offset from top of padded frame
            })

        elif t == "bottom_banner":
            fsize   = int(item.get("font_size", 24) * (w / 720))
            padding = item.get("padding", 16)
            text    = item.get("text", "")
            chars_per_line = max(20, int(w / (fsize * 0.6)))
            lines   = wrap_text_to_lines(text, chars_per_line)
            banner_h = int(len(lines) * fsize * 1.4 + padding * 2)
            banner_h = max(banner_h, fsize + padding * 2)
            bottom_banners.append({
                "text": text,
                "lines": lines,
                "fsize": fsize,
                "padding": padding,
                "banner_h": banner_h,
                "bg": hex_color(item.get("bg_color", "black")),
                "fg": hex_color(item.get("text_color", "white")),
                "y_offset": pad_bottom,
            })
            pad_bottom += banner_h

        elif t == "overlay_text":
            overlay_texts.append(item)

    # After crop, effective video dims change — use these for pad/text
    eff_w = crop_w if crop_filter else w
    eff_h = crop_h if crop_filter else h

    # Re-scale banner font sizes to cropped width, and re-wrap text at the
    # new width (line breaks depend on how many chars fit per line, which
    # changes once the width changes).
    for b in top_banners:
        b["fsize"] = max(10, int(b["fsize"] * eff_w / max(w, 1)))
        chars_per_line = max(10, int(eff_w / (b["fsize"] * 0.6)))
        b["lines"] = wrap_text_to_lines(b["text"], chars_per_line)
        b["banner_h"] = max(b["fsize"] + b["padding"] * 2,
                            int(len(b["lines"]) * b["fsize"] * 1.4 + b["padding"] * 2))
    # Recalculate pad_top with updated banner heights
    pad_top = 0
    for b in top_banners:
        b["y_offset"] = pad_top
        pad_top += b["banner_h"]

    pad_bottom = 0
    for b in bottom_banners:
        b["fsize"] = max(10, int(b["fsize"] * eff_w / max(w, 1)))
        chars_per_line = max(10, int(eff_w / (b["fsize"] * 0.6)))
        b["lines"] = wrap_text_to_lines(b["text"], chars_per_line)
        b["banner_h"] = max(b["fsize"] + b["padding"] * 2,
                            int(len(b["lines"]) * b["fsize"] * 1.4 + b["padding"] * 2))
        b["y_offset"] = pad_bottom
        pad_bottom += b["banner_h"]

    out_w = eff_w
    out_h = eff_h + pad_top + pad_bottom

    # Build the vf filter chain
    vf_parts = []

    # 1. Crop first
    if crop_filter:
        vf_parts.append(crop_filter)

    # 2. Pad to add banner space above/below cropped video
    if pad_top > 0 or pad_bottom > 0:
        vf_parts.append(
            f"pad=width={out_w}:height={out_h}:x=0:y={pad_top}:color=white"
        )

    # 3. Top banner backgrounds
    for b in top_banners:
        vf_parts.append(
            f"drawbox=x=0:y={b['y_offset']}:w={out_w}:h={b['banner_h']}"
            f":color={b['bg']}@1.0:t=fill"
        )

    # 4. Bottom banner backgrounds
    for b in bottom_banners:
        y = pad_top + eff_h + b["y_offset"]
        vf_parts.append(
            f"drawbox=x=0:y={y}:w={out_w}:h={b['banner_h']}"
            f":color={b['bg']}@1.0:t=fill"
        )

    # Horizontal text padding: keep text inset from banner edges
    h_pad = max(16, int(out_w * 0.04))  # 4% of width, min 16px

    if safe_font:
        # 5. Top banner text — centered, with horizontal padding.
        # Join the wrapped lines with a literal '\n' so drawtext actually
        # renders multiple lines instead of one long overflowing line.
        for b in top_banners:
            txt    = "\n".join(sanitize(line) for line in b["lines"])
            text_y = b["y_offset"] + b["banner_h"] // 2
            max_w  = out_w - h_pad * 2
            vf_parts.append(
                f"drawtext=text='{txt}':fontfile='{safe_font}'"
                f":fontsize={b['fsize']}:fontcolor={b['fg']}"
                f":x=(w-text_w)/2"
                f":y={text_y}-text_h/2"
                f":line_spacing=6"
            )

        # 6. Bottom banner text
        for b in bottom_banners:
            txt = "\n".join(sanitize(line) for line in b["lines"])
            y   = pad_top + eff_h + b["y_offset"] + b["banner_h"] // 2
            vf_parts.append(
                f"drawtext=text='{txt}':fontfile='{safe_font}'"
                f":fontsize={b['fsize']}:fontcolor={b['fg']}"
                f":x=(w-text_w)/2"
                f":y={y}-text_h/2"
                f":line_spacing=6"
            )

        # 7. Overlay texts on video area (also wrapped, using the video's
        # visible width as the wrap boundary)
        for item in overlay_texts:
            fsize = max(10, int(item.get("font_size", 28) * (eff_w / 720)))
            fg    = hex_color(item.get("text_color", "white"))
            pos   = item.get("position", "bottom")
            chars_per_line = max(10, int((eff_w - h_pad * 2) / (fsize * 0.6)))
            lines = wrap_text_to_lines(item.get("text", ""), chars_per_line)
            txt   = "\n".join(sanitize(line) for line in lines)
            if pos == "top":
                y_expr = f"{pad_top + 20}"
            elif pos == "center":
                y_expr = f"{pad_top + eff_h // 2}-text_h/2"
            else:
                y_expr = f"{pad_top + eff_h - 20}-text_h"
            vf_parts.append(
                f"drawtext=text='{txt}':fontfile='{safe_font}'"
                f":fontsize={fsize}:fontcolor={fg}"
                f":x=(w-text_w)/2:y={y_expr}"
                f":line_spacing=6"
            )

    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-vf", ",".join(vf_parts),
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
           "-c:a", "copy", output_path]

    print("FFmpeg command:", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("FFmpeg stderr:", result.stderr)
        return jsonify({"error": "FFmpeg failed", "details": result.stderr}), 500

    return jsonify({"status": "done", "output_file": output_filename})


@app.route("/download/<filename>")
def download(filename):
    path = os.path.join(OUTPUT_FOLDER, filename)
    if not os.path.exists(path):
        return "File not found", 404
    return send_file(path, as_attachment=True)


print("OPENROUTER_API_KEY exists:", bool(os.getenv("OPENROUTER_API_KEY")))

if __name__ == "__main__":
    print("Server running...")
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)