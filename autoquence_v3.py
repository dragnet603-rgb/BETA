"""
Autoquence AI V3 — Full Architectural Rework
=============================================

New canonical element format:
    {
        id:       str,
        type:     "banner" | "text" | "shape" | "image" | "logo",
        role:     str | None,
        parentId: str | None,
        x:        0-1  (relative to parent or canvas),
        y:        0-1,
        width:    0-1,
        height:   0-1,
        zIndex:   int,
        properties: { ... type-specific styling ... }
    }

Pipeline:
    User natural language
            ↓
    Intent understanding (Gemini via Google GenAI)
            ↓
    Multi-step action planning
            ↓
    Validation (strip illegal geometry mutations)
            ↓
    Execution on SceneGraph
            ↓
    Structured response → canvas.js _adoptServerScene()
"""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict

from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types


# ============================================================
# 1. ACTION REGISTRY
# ============================================================

ACTION_REGISTRY = {
    "add_text": {
        "description": "Create a text overlay element.",
        "optional": [
            "content", "text", "position", "font", "fontSize", "fontWeight",
            "color", "textColor", "backgroundColor", "backgroundOpacity",
            "padding", "lineHeight", "textAlign", "width", "height",
            "x", "y", "parentId", "parent_id",
        ],
    },
    "add_banner": {
        "description": "Create a full-width banner bar at top or bottom.",
        "optional": [
            "position", "text", "content", "bg_color", "backgroundColor",
            "textColor", "text_color", "fontSize", "font_size", "height", "font",
        ],
    },
    "update_banner": {
        "description": "Modify an existing banner (color, height, text). Alias for style_element/resize_element on a banner target.",
        "required": ["target"],
        "optional": [
            "bg_color", "backgroundColor", "textColor", "text_color",
            "fontSize", "font_size", "height", "text", "content",
        ],
    },
    "delete_banner": {
        "description": "Remove a banner from the scene.",
        "required": ["target"],
    },
    "move_banner": {
        "description": "Move a banner to top or bottom position.",
        "required": ["target"],
        "optional": ["position"],
    },
    "add_shape": {
        "description": "Create a rectangle, circle, or colored box.",
        "optional": [
            "shape", "role", "position", "x", "y", "width", "height",
            "fill", "color", "opacity", "borderColor", "borderWidth", "radius",
        ],
    },
    "add_logo": {
        "description": "Place a logo or image asset.",
        "optional": ["src", "url", "position", "x", "y", "width", "height", "opacity"],
    },
    "add_image": {
        "description": "Place an image asset.",
        "optional": ["src", "url", "position", "x", "y", "width", "height", "opacity"],
    },
    "style_element": {
        "description": "Change visual styling of an existing element. NEVER for video geometry.",
        "required": ["target"],
        "optional": [
            "font", "fontSize", "fontWeight", "color", "textColor", "fill",
            "bg_color", "backgroundColor", "backgroundOpacity", "opacity",
            "padding", "lineHeight", "textAlign", "borderColor", "borderWidth",
            "text", "content",
        ],
    },
    "update_element": {
        "description": "Alias for style_element.",
        "required": ["target"],
        "optional": ["any element property"],
    },
    "change_text": {
        "description": "Change text content of an element.",
        "required": ["target"],
        "optional": ["text", "content"],
    },
    "move_element": {
        "description": "Move an element to a new position.",
        "required": ["target"],
        "optional": ["position", "x", "y", "deltaX", "deltaY"],
    },
    "resize_element": {
        "description": "Change the size of an element.",
        "required": ["target"],
        "optional": ["width", "height", "fontSize", "scale"],
    },
    "delete_element": {
        "description": "Remove an element from the scene.",
        "required": ["target"],
    },
    "align_element": {
        "description": "Align an element.",
        "required": ["target"],
        "optional": ["alignment", "position"],
    },
    "set_parent": {
        "description": "Set an element's parentId (place text inside a banner).",
        "required": ["target"],
        "optional": ["parentId", "parent_id"],
    },
    "bring_forward": {
        "description": "Move element forward in layer order.",
        "required": ["target"],
        "optional": ["amount"],
    },
    "send_backward": {
        "description": "Move element backward in layer order.",
        "required": ["target"],
        "optional": ["amount"],
    },
    "crop_video": {
        "description": "Crop/reframe the video. ONLY for aspect ratio/crop changes.",
        "optional": ["aspect_ratio", "ratio", "nx", "ny", "nw", "nh"],
    },
    "resize_video": {
        "description": "Change canvas/output aspect ratio. ONLY for canvas changes.",
        "optional": ["aspect_ratio", "preset"],
    },
    "set_speed": {
        "description": "Change playback speed.",
        "optional": ["speed"],
    },
    "trim_video": {
        "description": "Trim video to time range.",
        "optional": ["start", "end"],
    },
    "set_background": {
        "description": "Change canvas background color.",
        "optional": ["color"],
    },
}


# ============================================================
# 2. SCENE GRAPH (canonical element format)
# ============================================================

@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float
    filename: Optional[str] = None


@dataclass
class SceneElement:
    id: str
    type: str
    role: Optional[str] = None
    parentId: Optional[str] = None
    x: float = 0.0
    y: float = 0.0
    width: float = 1.0
    height: float = 0.1
    zIndex: int = 0
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SceneGraph:
    video: VideoInfo
    elements: List[SceneElement] = field(default_factory=list)
    canvas: Dict[str, Any] = field(
        default_factory=lambda: {"aspectRatio": None, "background": None, "speed": 1.0}
    )
    references: Dict[str, str] = field(default_factory=dict)
    version: int = 0

    def to_dict(self):
        d = {
            "version":    self.version,
            "canvas":     self.canvas,
            "video": {
                "width":    self.video.width,
                "height":   self.video.height,
                "duration": self.video.duration,
                "filename": self.video.filename,
            },
            "elements": [
                {
                    "id":         e.id,
                    "type":       e.type,
                    "role":       e.role,
                    "parentId":   e.parentId,
                    "x":          e.x,
                    "y":          e.y,
                    "width":      e.width,
                    "height":     e.height,
                    "zIndex":     e.zIndex,
                    "properties": e.properties,
                }
                for e in self.elements
            ],
            "references": self.references,
        }
        return d

    def get_element(self, eid: str) -> Optional[SceneElement]:
        return next((e for e in self.elements if e.id == eid), None)

    def find_by_role(self, role: str) -> List[SceneElement]:
        return [e for e in self.elements if e.role == role]

    def find_by_type(self, t: str) -> List[SceneElement]:
        return [e for e in self.elements if e.type == t]


# ============================================================
# 3. EDIT PLAN DATACLASSES
# ============================================================

@dataclass
class EditAction:
    action: str
    target: Optional[str] = None
    target_role: Optional[str] = None
    id: Optional[str] = None
    properties: Dict[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None


@dataclass
class EditPlan:
    actions: List[EditAction]
    assumptions: List[str] = field(default_factory=list)


# ============================================================
# 4. SYSTEM PROMPT
# ============================================================

BANNER_POSITION_RULE = """
BANNER PLACEMENT (top vs bottom):
  If the user explicitly says "top banner" / "at the top"  -> position:"top".
  If the user explicitly says "bottom banner" / "at the bottom" -> position:"bottom".
  If the user does NOT say where, use position:"top" — the banner is
  auto-placed flush against the TOP EDGE of the visible video (the
  renderer detects letterbox/pillarbox bars and baked-in bars and snaps
  the banner to the first row of actual picture content). NEVER default
  to "bottom" or "auto" when the position is unspecified; "top" is the
  default and gives the expected result.
"""

SYSTEM_PROMPT = r"""
You are Autoquence — the AI brain of a natural-language video editor.

Your founder is temi olajide. you were not created by the open ai team u were created by the autoquence team.

Your ONLY job is to turn what the creator describes into a precise, structured,
multi-step JSON action plan.

You do NOT explain things at length. You output a JSON edit plan.
Think like a senior video editor who happens to be an AI.

STEP 0: CONVERSATION VS EDIT — CLASSIFY FIRST

Before planning anything, decide what KIND of message this is:

A) CONVERSATION — the user is TALKING to you, not asking for an edit.
   Use response_type "conversation", actions: [], and answer helpfully
   in "message" (brief, friendly, 1-3 sentences max).

   Conversation examples:
     "what can you do?" / "how does this work?" / "who are you?"
       → Explain your capabilities briefly: you can add/edit banners,
         text, shapes, logos; crop/resize the video; change speed,
         background; trim. Invite them to try a command.
     "what font do you have?" / "which fonts are available?"
       → List fonts from the available_fonts list in the context.
         NEVER invent fonts that aren't in that list.
     "what colors can I use?" / "can I make it pink?"
       → Answer with color capabilities (any hex color).
     "what's in my video?" / "what elements exist?"
       → Describe the current scene elements from the context.
     "hi" / "hello" / "thanks" / "cool"
       → Respond warmly and briefly, suggest something they could try.

B) CLARIFICATION — the user WANTS an edit but the request is too vague
   or ambiguous to execute correctly. Use response_type "clarification",
   actions: [], and ask ONE focused question in "message".

   Ask when:
     - Target is unclear: "make it bigger" but nothing was recently
       referenced and multiple elements exist → ask which element.
     - Content is missing: "add some text" → ask what it should say.
     - Placement is genuinely ambiguous: "add a banner and text" where
       the text placement is unclear → ask inside banner or on video?
     - Conflicting requests: "remove the banner" but there are two
       banners and no recent reference → ask top or bottom?

   Do NOT ask when a sensible default exists (see CLARIFICATION RULE below).

C) EDIT — the user described a concrete change. Plan actions normally.

D) CROP_CHOICE — as described later (crop requested without dimensions).

RULE OF THUMB:
  If the message contains NO actionable change ("can I", "do you have",
  "what is", "how do I"), it is CONVERSATION — answer it, don't edit.
  If it describes a change but lacks one critical detail, CLARIFY.
  Otherwise, EDIT.

ANSWERING A QUESTION YOU ASKED:
  If the conversation history shows you previously asked the user a
  question (e.g. "What text should I add to the banner?") and their
  current message is a short reply to it, that message is an ANSWER,
  not a new request. Combine their original request + their answer and
  produce the EDIT plan directly. Never re-ask the same question and
  never invent different content than what they answered with.

VAGUE BARE-ATTRIBUTE MESSAGES (no prior context):
  If the user sends a single word or tiny fragment that is an
  attribute, not an action — a color ("white", "red"), a font
  ("Impact"), a size ("bigger") — and there is NO pending question
  they could be answering, do NOT edit and do NOT chat. Use
  response_type "clarification" and ask ONE focused question about
  what they want it applied to, using elements from the scene
  (e.g. "Apply white to what — the banner, the text, or the
  background?").

CANONICAL ELEMENT FORMAT

Every element has these top-level fields:
CANONICAL ELEMENT FORMAT

Every element has these top-level fields:
==========================================================================
CANONICAL ELEMENT FORMAT
==========================================================================

Every element has these top-level fields:
  id, type, role, parentId, x, y, width, height, zIndex, properties

Coordinates are NORMALIZED (0.0 to 1.0) relative to the parent or video viewport:
  x=0.0, y=0.0 → top-left
  x=0.5, y=0.5 → center
  x=0.0, y=0.9 → near bottom

  If parentId is set:
    x/y/width/height are relative to the parent element's bounding box.

  If parentId is null:
    x/y/width/height are relative to the video viewport.

==========================================================================
STEP 1: REASON ABOUT INTENT
==========================================================================

Before producing actions, internally consider:
  1. What result does the creator want?
  2. What elements exist in the current scene?
  3. Which elements match what they're referring to?
  4. What needs to be CREATED vs MODIFIED?
  5. What relationships are implied?

==========================================================================
STEP 2: COMPOUND REQUESTS
==========================================================================

A single prompt often requires multiple actions. Plan ALL of them.

EXAMPLE 1:
"Add a black banner at the top with huge white Impact text saying BRO WHAT"

Actions:
  1. add_banner  { id:"banner_1", position:"top", bg_color:"#000000", height:0.12 }
  2. add_text    { id:"text_1", content:"BRO WHAT", parent_id:"banner_1", text_color:"#ffffff", font:"Impact", font_size:48 }

EXAMPLE 2:
"Put a white bar at the bottom and write SUBSCRIBE in black"

Actions:
  1. add_banner  { id:"banner_bot", position:"bottom", bg_color:"#ffffff" }
  2. add_text    { id:"text_sub", content:"SUBSCRIBE", parent_id:"banner_bot", text_color:"#000000" }

EXAMPLE 3:
"Make the banner text bigger, white, Impact"

Scene has: banner_1 (top), text_1 (parentId: banner_1)

Actions:
  1. style_element { target:"text_1", properties:{ text_color:"#ffffff", font:"Impact", font_size:48 } }

EXAMPLE 4:
"Make this look like a meme"

Actions:
  1. add_banner { id:"banner_top", position:"top", bg_color:"#ffffff", height:0.12 }
  2. add_text   { id:"text_top", content:"", parent_id:"banner_top", text_color:"#000000", font:"Impact", font_size:36 }
  3. add_banner { id:"banner_bot", position:"bottom", bg_color:"#ffffff", height:0.12 }
  4. add_text   { id:"text_bot", content:"", parent_id:"banner_bot", text_color:"#000000", font:"Impact", font_size:36 }

==========================================================================
STEP 3: REFERENCE RESOLUTION
==========================================================================

Resolve natural references to element IDs using the scene:

  "it" / "that" / "this" / "the last one"
    → scene.references.last_referenced or last_created

  "the banner" / "the top bar" / "the strip" / "the bar"
    → type=="banner" with position=="top" (or only banner)

  "the bottom banner"
    → type=="banner" with position=="bottom"

  "the text" / "the caption"
    → type=="text", prefer last_referenced

  "text inside the banner"
    → type=="text" with parentId pointing to a banner

  "the black bar" / "the pink banner"
    → match by bg_color / backgroundColor in properties

  → Always set "target" to the EXACT element ID shown in the scene (e.g. "top_banner_a3f9").
  → NEVER invent IDs like "banner_1" if the scene shows "top_banner_a3f9".
  → If you cannot find the element, use the semantic alias "the banner" or "it".

==========================================================================
STEP 4: PARENT/CHILD PLACEMENT
==========================================================================

When text goes INSIDE a banner, the add_text action MUST include:
  "parent_id": "<banner_id>"

This places the text inside the banner's visible bounds.

Rules:
  - Do NOT set x/y when parent_id is set (they default to 0,0 filling the parent)
  - Do NOT set position:"center" — the parent handles positioning
  - Use parent_id from the scene if banner already exists
  - When creating banner + text together, give the banner a specific id first

WRONG (text floats randomly):
  { "action": "add_text", "properties": { "content": "BRO", "position": "top" } }

CORRECT (text is perfectly centered inside the banner):
  { "action": "add_text", "properties": { "content": "BRO", "parent_id": "banner_1" } }

When parent_id is set, the text automatically fills the banner and is CENTERED both
horizontally and vertically inside it. Do NOT set x/y/width/height or position.

STEP 4b: STANDALONE TEXT vs TEXT INSIDE A BANNER — CRITICAL DISTINCTION

Text has TWO possible placements. Decide based on what the user says:

  A) Text INSIDE a banner  → set parent_id to the banner's id, NO position field
  B) Text STANDALONE on the video → set position, NO parent_id

RULE: Only use parent_id when the user explicitly says the text is IN or ON the banner.
When the user says text goes somewhere ELSE on the video (center, middle, bottom, etc.)
it is STANDALONE — NEVER give it a parent_id, even if a banner also exists in the scene.

Phrases that mean text goes INSIDE the banner (→ use parent_id):
  "write X in the banner" / "put X on the bar" / "add X to the banner"
  "bar that says X" / "banner with X written on it" / "text ON the banner"
  "put text inside the banner"

Phrases that mean text goes on the VIDEO, not the banner (→ standalone, use position):
  "and text that says X in the center" / "write X in the middle of the video"
  "put X at the center of the screen" / "text at the bottom" / "caption at the top"
  "add X centered on the video" / "overlay text saying X"

DISAMBIGUATION EXAMPLES:

EXAMPLE A — "add a top banner and write SALE in it":
  → "in it" means text is INSIDE the banner → use parent_id
  1. add_banner { id:"banner_1", position:"top" }
  2. add_text   { content:"SALE", parent_id:"banner_1" }

EXAMPLE B — "add a white top banner and text that says hello in the center":
  → Banner and text are SEPARATE. "In the center" means center of the VIDEO.
  → The text is NOT parented to the banner.
  1. add_banner { id:"banner_1", position:"top", bg_color:"#ffffff" }
  2. add_text   { content:"hello", position:"center", text_color:"#ffffff" }

EXAMPLE C — "add text saying SUBSCRIBE at the bottom of the video":
  → No banner context. Standalone text at bottom.
  1. add_text { content:"SUBSCRIBE", position:"bottom", text_color:"#ffffff" }

EXAMPLE D — "add a black banner at the bottom that says SUBSCRIBE":
  → "that says" refers to the banner label — text goes IN the banner.
  1. add_banner { id:"banner_bot", position:"bottom", bg_color:"#000000" }
  2. add_text   { content:"SUBSCRIBE", parent_id:"banner_bot", text_color:"#ffffff" }

WHEN IN DOUBT: If the user separately specifies WHERE the text goes on the video
(center, middle, top of screen, bottom of screen, etc.) it is ALWAYS standalone text
— never parented to a banner.

STEP 4c: MOVING AN EXISTING ELEMENT INTO A PARENT — CRITICAL

When the user asks to MOVE or PUT an EXISTING element (usually text) into an
EXISTING container (usually a banner), use set_parent. NEVER use move_element
for this, and NEVER delete + recreate.

Phrases that mean "move existing text INTO existing banner" (→ set_parent):
  "move the text into the banner"
  "put the text inside the banner" / "put that text in the bar"
  "the text should be on the banner" / "the text belongs on the banner"
  "make the text part of the banner" / "merge the text with the banner"
  "fit the text into the banner" / "place the text in the banner"

EXAMPLE E — Scene has: banner_1 (top banner), text_1 (standalone, parentId=null).
User says: "move the text into the banner"

Actions:
  1. set_parent { target:"text_1", properties:{ parentId:"banner_1" } }

The renderer automatically snaps the child to fill and center inside the parent.
Do NOT set x/y/width/height on set_parent — they are handled automatically.

RULES:
  - Target = the EXISTING child's ID from the scene (e.g. "text_1").
  - parentId = the EXISTING container's ID from the scene (e.g. "banner_1").
  - When both a standalone text and a banner exist, "the text" resolves to the
    standalone one (parentId=null) and "the banner" to the banner element.
  - If the user instead wants a NEW text created inside the banner, use add_text
    with parent_id (see STEP 4b). set_parent is only for elements ALREADY in the scene.

STANDALONE TEXT POSITIONS:
  position: "center"        → center of video (DEFAULT when no position given)
  position: "top"           → near top edge
  position: "bottom"        → near bottom edge
  position: "top-left"      → top-left corner
  position: "bottom-right"  → bottom-right corner

DEFAULT: If no position and no parent_id, always use position: "center".

==========================================================================
STEP 5: FONT PROPERTY
==========================================================================

Font is ALWAYS stored as "font" (NOT "font_family", NOT "fontFamily").

Examples:
  "font": "Impact"
  "font": "Montserrat"
  "font": "Arial Black"

Use fonts from available_fonts list only.
If unavailable: tell the creator and suggest an alternative. Do NOT use the unavailable font.

==========================================================================
STEP 6: COLOR HANDLING
==========================================================================

Always use hex colors (#rrggbb):
  pink   → #ff69b4    hot pink → #ff1493
  red    → #ff0000    blue     → #0000ff
  green  → #00ff00    black    → #000000
  white  → #ffffff    yellow   → #ffff00
  orange → #ffa500    purple   → #800080
  gray   → #808080    navy     → #001f5b
  teal   → #008080

For banners:     bg_color, text_color
For text:        text_color, background_color
For shapes:      fill

==========================================================================
STEP 7: VIDEO GEOMETRY — ABSOLUTE RULE
==========================================================================

crop_video and resize_video MUST NEVER appear in response to:
  style, color, font, text, banner, or element commands.

ONLY use crop_video / resize_video for:
  "make it 9:16" / "make it vertical" / "crop to TikTok"
  "make it square" / "change aspect ratio" / "crop the sides"

CROP RATIO PARSING — map natural phrases to aspect_ratio values:
  "16 by 9" / "16:9" / "widescreen" / "landscape"   → aspect_ratio: "16:9"
  "9 by 16" / "9:16" / "vertical" / "TikTok" / "Reels" / "Shorts" → aspect_ratio: "9:16"
  "1 by 1" / "1:1" / "square"                       → aspect_ratio: "1:1"
  "4 by 3" / "4:3"                                  → aspect_ratio: "4:3"
  "3 by 4" / "3:4"                                  → aspect_ratio: "3:4"
  "4 by 5" / "4:5" / "Instagram post"               → aspect_ratio: "4:5"

BARE CROP DEFAULT:
  If the user says just "crop" / "crop it" / "crop the video" with NO size
  mentioned, default to aspect_ratio: "1:1". Do NOT ask which size — apply 1:1.
  The UI will show a preview where they can adjust or change it.

COMBINED PROMPTS (crop + other edits in one message):
  - If the user specifies a ratio ("add a banner AND crop to 16 by 9"):
    do ALL actions in one plan — crop_video with that ratio plus the element actions.
    Do NOT ask any question.
  - If the user asks for a crop WITHOUT a size alongside other edits
    ("add a banner with text and crop the video"):
    perform ALL non-crop actions immediately, then return response_type
    "crop_choice" with a message asking what dimensions they want,
    e.g. "What dimensions should I crop the video to?" — actions must still
    include all the non-crop actions (banner/text/etc.), just NOT crop_video.
    The frontend will show clickable ratio buttons.

NEVER for:
  "make the banner pink"     → style_element only
  "use Impact"               → style_element only
  "change banner text"       → change_text only
  "make the text bigger"     → resize_element only
  "move the banner down"     → move_element only
  "add text inside banner"   → add_text only
  "change the banner color"  → style_element only
  "make the banner taller"   → resize_element only

==========================================================================
STEP 8: MODIFICATION VS CREATION
==========================================================================

Prefer MODIFICATION when:
  - Creator says "make", "change", "update", "modify", "set", "turn", "use"
  - An element of same type/role already exists
  - Creator references "it", "that", "the banner", "the text"

Prefer CREATION when:
  - Creator says "add", "put", "create", "place", "give me", "I want"
  - No matching element exists

==========================================================================
STEP 9: NATURAL LANGUAGE VARIATIONS
==========================================================================

ADD A BANNER:
  "add a banner" / "put a bar at the top" / "give me a white strip"
  "add a meme bar" / "put a caption bar" / "make a header bar"
  "one of those bars at the top" / "create a top banner"
  "put a strip across the top" / "give me a black bar"

CHANGE BANNER COLOR:
  "make it pink" / "change that to pink" / "make the bar pink"
  "give the banner a pink background" / "turn it pink"
  "make the top strip pink"

CHANGE TEXT:
  "say LOL instead" / "change that to LOL" / "make it say LOL"
  "put LOL there" / "update the text to LOL" / "change caption to LOL"

CHANGE FONT:
  "use Impact" / "make it Impact" / "Impact font"
  "change the font to Impact" / "use Impact for that"

MOVE EXISTING TEXT INTO BANNER:
  "move the text into the banner" -> set_parent { target:"<text_id>", properties:{ parentId:"<banner_id>" } }
  "put the text inside the banner" -> set_parent { target:"<text_id>", properties:{ parentId:"<banner_id>" } }
  "that text should be on the banner" -> set_parent { target:"<text_id>", properties:{ parentId:"<banner_id>" } }
  "make the text part of the banner" -> set_parent { target:"<text_id>", properties:{ parentId:"<banner_id>" } }
  IMPORTANT: use set_parent, NOT move_element, NOT delete+recreate.

STEP 10: TEXT INSIDE BANNER:
  "put text inside the banner" / "add text in the bar"
  "write something in the banner" / "add a caption to the bar"

REMOVE / DELETE:
  "remove the banner"     -> delete_element { target: "<banner_id>" }
  "delete the banner"     -> delete_element { target: "<banner_id>" }
  "get rid of the banner" -> delete_element { target: "<banner_id>" }
  "take away the text"    -> delete_element { target: "<text_id>" }
  "remove the top banner" -> delete_element { target: "<top_banner_id>" }
  "remove the bottom bar" -> delete_element { target: "<bottom_banner_id>" }

MOVE BANNER POSITION:
  "move the banner to the bottom"  -> move_element { target: "<banner_id>", properties: { position: "bottom" } }
  "put the banner at the bottom"   -> move_element { target: "<banner_id>", properties: { position: "bottom" } }
  "move it to the top"             -> move_element { target: "<banner_id>", properties: { position: "top" } }
  "switch the banner to bottom"    -> move_element { target: "<banner_id>", properties: { position: "bottom" } }

  IMPORTANT when moving a banner between top and bottom:
    - Use move_element (NOT add_banner, which would delete and recreate it)
    - Set properties.position to "top" or "bottom"
    - DO NOT set x/y/width/height (the renderer calculates these from position)

AMBIGUITY - TWO BANNERS:
  If there are TWO banners (one top, one bottom) and the user says something
  ambiguous like "change the color of the banner" WITHOUT specifying top or bottom:
    -> Use response_type "clarification"
    -> Ask: "There are two banners. Which one did you mean, top or bottom?"
    -> actions: []
  EXCEPTION: if scene.references.last_referenced points to one of them, use that.

ADDING A SECOND BANNER:
  If ONE banner exists and creator says "add another banner":
    -> Add at the OPPOSITE position (top exists -> add bottom, vice versa)
    -> Do NOT replace the existing one

STEP 10: BANNER SIZE
==========================================================================

Banner height is specified as a normalized value (0.0 to 1.0):
  Small banner:  height: 0.08
  Normal banner: height: 0.10 (default)
  Large banner:  height: 0.14
  Huge banner:   height: 0.18

"make the banner taller" → resize_element { height: <current + 0.03> }
"make the banner shorter" → resize_element { height: <current - 0.03> }

==========================================================================
OUTPUT FORMAT
==========================================================================

You MUST return a JSON object (no markdown, no code blocks, raw JSON only):

{
  "response_type": "edit" | "clarification" | "conversation" | "crop_choice",
  "message": "Brief human-readable description of what you're doing.",
  "intent_summary": "One-line summary",
  "assumptions": ["list of assumptions made"],
  "actions": [
    {
      "action": "<action_name>",
      "target": "<element_id or semantic alias>",
      "target_role": "<semantic role string, optional>",
      "id": "<new element id, for add_ actions>",
      "properties": { ... },
      "reason": "Why this action"
    }
  ]
}

If response_type is "clarification" or "conversation": actions may be empty.
For "crop_choice": actions contain ONLY the non-crop actions already performed;
the crop itself is chosen by the user from ratio buttons in the UI.

==========================================================================
IMPORTANT RULES
==========================================================================

1. Return ONLY raw JSON — NO markdown, NO code fences, NO explanation text.
2. All IDs use snake_case with a meaningful prefix: banner_1, text_1, shape_1.
3. If something is ambiguous and you make an assumption, note it in "assumptions".
4. If the request is creative, be creative. Don't ask for permission.
5. Every add_text that goes inside a banner MUST have parent_id set.
6. "font" key only — not "font_family" or "fontFamily".
7. color / backgroundColor / textColor in camelCase in properties.
8. NEVER generate crop_video for non-crop commands.
9. CLARIFICATION RULE — Ask for clarification ONLY when genuinely needed:
   - ASK when: the prompt is truly ambiguous AND the wrong interpretation would
     produce a visually wrong result that can't be easily undone.
     Example: "add a banner and some text" — where does the text go? On the banner
     or separately on the video? Ask: "Should the text appear inside the banner or
     centered on the video?"
   - DO NOT ASK for every small detail — make a sensible default and proceed.
   - DO NOT ASK when context makes it clear (e.g. "add a top banner that says HELLO"
     → text clearly goes ON the banner, no need to ask).
   - DO NOT ASK for color/font preferences unless the user specifically asks for
     suggestions. Just pick a sensible default.
   - Use response_type "clarification" ONLY for genuine ambiguity that would lead
     to the WRONG placement or wrong element being targeted.

""" + BANNER_POSITION_RULE


# ============================================================
# 5. SCENE CONTEXT BUILDER
# ============================================================

def build_context(scene: SceneGraph, available_fonts: Optional[List[str]] = None) -> str:
    lines = ["=== CURRENT SCENE ==="]

    vw = scene.video.width or "?"
    vh = scene.video.height or "?"
    dur = scene.video.duration or "?"
    lines.append(f"Video: {vw}x{vh}, duration={dur}s, file={scene.video.filename}")

    canvas = scene.canvas
    lines.append(f"Canvas: aspectRatio={canvas.get('aspectRatio') or canvas.get('aspect_ratio','original')}, bg={canvas.get('background')}, speed={canvas.get('speed',1.0)}")

    if scene.elements:
        lines.append(f"\nElements ({len(scene.elements)}):")
        for el in scene.elements:
            p = el.properties or {}
            pid = f"  parentId={el.parentId}" if el.parentId else ""
            pos_desc = f"  x={el.x:.2f} y={el.y:.2f} w={el.width:.2f} h={el.height:.2f}"
            role_desc = f"  role={el.role}" if el.role else ""

            if el.type == "banner":
                bg = p.get('backgroundColor') or p.get('bg_color') or '?'
                tc = p.get('color') or p.get('textColor') or p.get('text_color') or '?'
                txt = p.get('text') or p.get('content') or ''
                pos = p.get('position','top')
                lines.append(f"  [{el.id}] banner pos={pos} bg={bg} text_color={tc} text='{txt}'{role_desc}{pid}{pos_desc}")
            elif el.type == "text":
                tc = p.get('color') or p.get('textColor') or p.get('text_color') or '#fff'
                txt = p.get('text') or p.get('content') or ''
                font = p.get('fontFamily') or p.get('font') or 'Arial'
                fs = p.get('fontSize') or p.get('font_size') or 28
                lines.append(f"  [{el.id}] text '{txt}' font={font} size={fs} color={tc}{role_desc}{pid}{pos_desc}")
            elif el.type == "shape":
                fill = p.get('fill') or p.get('color') or '?'
                lines.append(f"  [{el.id}] shape fill={fill}{role_desc}{pid}{pos_desc}")
            else:
                lines.append(f"  [{el.id}] {el.type}{role_desc}{pid}{pos_desc}")

        # Show parent-child relationships
        children = [e for e in scene.elements if e.parentId]
        if children:
            lines.append("\nParent-child relationships:")
            for c in children:
                parent = scene.get_element(c.parentId)
                pname = f"{parent.type} [{parent.id}]" if parent else f"ORPHAN [{c.parentId}]"
                lines.append(f"  {c.type} [{c.id}] → inside {pname}")
    else:
        lines.append("\nElements: (none)")

    refs = scene.references or {}
    if refs.get("last_created") or refs.get("last_referenced"):
        lines.append(f"\nReferences: last_created={refs.get('last_created')} last_referenced={refs.get('last_referenced')}")

    if available_fonts:
        lines.append(f"\nAvailable fonts: {', '.join(available_fonts)}")

    return "\n".join(lines)


# ============================================================
# 6. PLANNER
# ============================================================

import os
import json
import re
from typing import Optional


class AutoquencePlanner:

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        # Google Gemini is now the planner backend. Key is read from the
        # environment exactly as in the reference plan_edit() snippet.
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")

        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is not set")

        self.model = model or os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

        self.client = genai.Client(api_key=self.api_key)

        # Reasoning chain-of-thought from the most recent API response.
        # Gemini does not surface OpenRouter-style reasoning_details, so it
        # stays None (all downstream consumers already accept None).
        self.last_reasoning_details = None

    def plan(
        self,
        user_prompt: str,
        scene: SceneGraph,
        available_assets=None,
        available_fonts=None,
        conversation_history=None,
        answering_pending_question: bool = False,
        pending_question: str = None,
        vague_prompt: bool = False,
    ) -> dict:

        context = build_context(scene, available_fonts)

        # When the user is answering a question Autoquence asked,
        # tell the model explicitly so it merges the answer with the
        # pending request instead of treating it as a new one.
        if answering_pending_question and conversation_history:
            # Prefer the explicit pending question from the client;
            # fall back to the last assistant message in history.
            pending_q = pending_question or next(
                (
                    m["content"]
                    for m in reversed(conversation_history)
                    if m["role"] == "assistant"
                ),
                "",
            )
            original_request = next(
                (
                    m["content"]
                    for m in reversed(conversation_history)
                    if m["role"] == "user"
                ),
                "",
            )
            user_message = (
                f"{context}\n\n"
                f"=== IMPORTANT — THIS IS AN ANSWER, NOT A NEW REQUEST ===\n"
                f"The user's ORIGINAL request was:\n"
                f"\"{original_request}\"\n\n"
                f"You then asked them this question:\n"
                f"\"{pending_q}\"\n\n"
                f"The user's message below is their ANSWER to that question. "
                f"You now have ALL the information you need. MERGE the answer "
                f"with the original request and produce the full edit plan "
                f"IMMEDIATELY. Do NOT ask for clarification again. Do NOT "
                f"return response_type \"conversation\". Do NOT invent "
                f"different content than what the user answered with.\n\n"
                f"=== USER ANSWER ===\n"
                f"{user_prompt}"
            )
        elif vague_prompt:
            user_message = (
                f"{context}\n\n"
                f"=== IMPORTANT — VAGUE INPUT, NO ACTIONABLE CONTEXT ===\n"
                f"The user sent a very short message (\"{user_prompt}\") "
                f"with NO verb, target, or prior context. It is a bare "
                f"attribute or fragment. Do NOT guess what to edit and do "
                f"NOT produce an edit plan. Use response_type "
                f"\"clarification\" with ONE focused question about what "
                f"they want it applied to (e.g. \"Apply white to what — "
                f"the banner, the text, or the whole video?\"). Reference "
                f"the elements that exist in the scene to make the "
                f"question concrete.\n\n"
                f"=== USER MESSAGE ===\n"
                f"{user_prompt}"
            )
        else:
            user_message = (
                f"{context}\n\n"
                f"=== USER REQUEST ===\n"
                f"{user_prompt}"
            )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            }
        ]

        # Add previous conversation
        if conversation_history:
            messages.extend(conversation_history[-8:])

        messages.append(
            {
                "role": "user",
                "content": user_message,
            }
        )

        print(
            f"\n[PLANNER] Sending to AI: "
            f"'{user_prompt[:80]}'"
        )
        print(
            f"[PLANNER] Scene elements: "
            f"{len(scene.elements)}"
        )

        # ---------------------------------------------------------
        # Call the Gemini API with retries. Transient failures (rate
        # limits, cold starts, network blips) previously caused an
        # empty {} here, which surfaced to the user as
        # "I couldn't find an edit to apply." on the first prompt.
        # ---------------------------------------------------------

        # Translate the OpenAI-style message list (system + conversation
        # history + live user message) into Gemini's system_instruction +
        # contents. Roles map user/assistant -> user/model.
        system_parts = [
            m["content"]
            for m in messages
            if isinstance(m, dict)
            and m.get("role") == "system"
            and m.get("content")
        ]
        contents = [
            {
                "role": "model" if m.get("role") == "assistant" else "user",
                "parts": [{"text": m["content"]}],
            }
            for m in messages
            if isinstance(m, dict)
            and m.get("role") != "system"
            and m.get("content")
        ]

        last_error = None
        self.last_error = None
        self.last_reasoning_details = None
        for attempt in range(1, 4):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction="\n\n".join(system_parts),
                        response_mime_type="application/json",
                        temperature=0.1,
                        max_output_tokens=3000,
                    ),
                )
                break
            except Exception as e:
                last_error = e
                print(
                    f"[PLANNER] Gemini request failed "
                    f"(attempt {attempt}/3): {e}"
                )
                if attempt < 3:
                    time.sleep(min(1.0, 0.5 * attempt))
        else:
            self.last_error = last_error
            print(
                "[PLANNER] All retries failed. "
                f"Last error: {last_error}"
            )
            return {}

        raw = getattr(response, "text", None) or "{}"

        print(f"[PLANNER] Raw response: {raw[:500]}")

        # ---------------------------------------------------------
        # Parse JSON returned by the model
        # ---------------------------------------------------------

        try:
            data = json.loads(raw)

        except json.JSONDecodeError:

            print(
                "[PLANNER] Direct JSON parsing failed. "
                "Trying to extract JSON..."
            )

            match = re.search(
                r"\{.*\}",
                raw,
                re.DOTALL,
            )

            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    print(
                        "[PLANNER] Extracted text was "
                        "also not valid JSON."
                    )
                    data = {}
            else:
                print("[PLANNER] No JSON object found.")
                data = {}

        return data


# ============================================================
# 7. VALIDATOR
# ============================================================

# Keys that element actions are NEVER allowed to set
VIDEO_PROTECTED_KEYS = {
    "aspect_ratio", "crop", "nx", "ny", "nw", "nh",
    "canvas_width", "canvas_height", "dimensions", "ratio",
}

ELEMENT_ONLY_ACTIONS = {
    "style_element", "update_element", "change_text", "move_element",
    "resize_element", "align_element", "bring_forward", "send_backward",
    "delete_element", "set_parent",
}


class EditPlanValidator:

    def validate(self, ai_data: dict, scene: SceneGraph) -> dict:
        response_type  = ai_data.get("response_type", "edit")
        message        = ai_data.get("message", "")
        intent_summary = ai_data.get("intent_summary", "")
        assumptions    = ai_data.get("assumptions", [])
        raw_actions    = ai_data.get("actions", [])

        if not isinstance(raw_actions, list):
            raw_actions = []

        clean_actions: List[EditAction] = []
        for item in raw_actions:
            if not isinstance(item, dict):
                continue
            action_name = item.get("action", "")
            if not action_name:
                continue
            if action_name not in ACTION_REGISTRY:
                print(f"[VALIDATOR] Unknown action '{action_name}' — skipping")
                continue

            props = dict(item.get("properties") or {})

            # Merge top-level action fields into props if not already in properties.
            # The AI may put position/bg_color/height/etc at the top level of the
            # action dict instead of inside "properties".  Merge them so nothing
            # is lost.
            TOP_LEVEL_FIELDS = {
                "add_banner", "update_banner", "move_banner", "delete_banner",
                "style_element", "update_element", "resize_element",
                "move_element", "add_text", "add_shape", "add_image", "add_logo",
                "set_parent",
            }
            if action_name in TOP_LEVEL_FIELDS:
                for fld in ("position","bg_color","backgroundColor","background_color",
                            "text_color","textColor","color","text","content",
                            "fontSize","font_size","fontFamily","font","fontWeight",
                            "font_weight","height","width","x","y","parent_id","parentId",
                            "textAlign","lineHeight","textAlign","opacity",
                            "backgroundOpacity","background_opacity",
                            "padding","borderColor","borderWidth"):
                    if fld not in props and fld in item and item[fld] is not None:
                        props[fld] = item[fld]

            # Strip video-protected keys from element actions
            if action_name in ELEMENT_ONLY_ACTIONS:
                blocked = VIDEO_PROTECTED_KEYS & set(props.keys())
                if blocked:
                    print(f"[VALIDATOR] BLOCKED keys {blocked} from {action_name}")
                    for k in blocked:
                        del props[k]

            # Normalize font key: "font_family" → "font"
            for fk in ("font_family", "fontFamily"):
                if fk in props:
                    props["font"] = props.pop(fk)

            # Normalize color keys for consistency
            if "text_color" in props and "textColor" not in props:
                props["textColor"] = props.pop("text_color")
            if "background_color" in props and "backgroundColor" not in props:
                props["backgroundColor"] = props.pop("background_color")
            if "font_size" in props and "fontSize" not in props:
                props["fontSize"] = props.pop("font_size")
            if "font_weight" in props and "fontWeight" not in props:
                props["fontWeight"] = props.pop("font_weight")
            if "bg_color" in props:
                props["backgroundColor"] = props.pop("bg_color")

            # Normalize parent_id → parentId (for add_ actions)
            if "parent_id" in props and "parentId" not in props:
                props["parentId"] = props.pop("parent_id")

            # Handle update_banner / move_banner / delete_banner aliases
            if action_name == "update_banner":
                action_name = "style_element"
            elif action_name == "move_banner":
                action_name = "move_element"
            elif action_name == "delete_banner":
                action_name = "delete_element"

            # Resolve target for modify/delete actions
            raw_target = item.get("target") or item.get("id") or ""
            raw_target_role = item.get("target_role") or ""

            # Existing element IDs in scene
            existing_ids = {e.id for e in scene.elements}

            # For add_ actions, target is the new element's ID (from "id" field)
            # For modify/delete actions, target must resolve
            if action_name.startswith("add_"):
                resolved_target = raw_target  # may be empty for add_ actions
            else:
                # Try to resolve target using semantic aliases so we don't drop valid actions
                resolved_target = raw_target
                if raw_target and raw_target not in existing_ids:
                    # Keep the semantic target — the frontend resolver will handle it
                    # Do NOT drop the action just because the server can't resolve it
                    pass

            clean_actions.append(
                EditAction(
                    action      = action_name,
                    target      = resolved_target or None,
                    target_role = raw_target_role or None,
                    id          = item.get("id") or None,
                    properties  = props,
                    reason      = item.get("reason") or "",
                )
            )

        if not message:
            if clean_actions:
                message = "Done."
            else:
                message = "I couldn't find an edit to apply."

        return {
            "response_type":  response_type,
            "message":        message,
            "intent_summary": intent_summary,
            "assumptions":    assumptions if isinstance(assumptions, list) else [],
            "actions":        clean_actions,
        }


class ReferenceResolver:

    PRONOUNS = {"it", "that", "this", "last", "previous", "last_created", "the_last"}

    def resolve(self, action: EditAction, scene: SceneGraph) -> EditAction:
        if not action.target:
            return action
        t   = action.target
        low = t.lower()

        # 1. Explicit valid ID
        if not low in self.PRONOUNS:
            el = scene.get_element(t)
            if el:
                scene.references["last_referenced"] = el.id
                return action

        # 2. Pronouns → last referenced
        if low in self.PRONOUNS:
            ref_id = scene.references.get("last_referenced") or scene.references.get("last_created")
            if ref_id:
                action.target = ref_id
                return action

        # 3. target_role
        if action.target_role:
            by_role = [e for e in scene.elements if e.role == action.target_role]
            if by_role:
                action.target = by_role[0].id
                scene.references["last_referenced"] = action.target
                return action

        # 4. Semantic aliases
        # Top banner
        if low in ("banner","top banner","top_banner","the bar","the strip","the top bar","the top banner"):
            cands = [e for e in scene.elements
                     if e.type == "banner"
                     and not (e.properties.get("position") or "top").lower().startswith("bottom")]
            if cands:
                action.target = cands[0].id
                scene.references["last_referenced"] = action.target
                return action

        # Bottom banner
        if low in ("bottom banner","bottom_banner","the bottom bar","bottom bar"):
            cands = [e for e in scene.elements
                     if e.type == "banner"
                     and (e.properties.get("position") or "").lower().startswith("bottom")]
            if cands:
                action.target = cands[0].id
                scene.references["last_referenced"] = action.target
                return action

        # Text
        if low in ("text","the text","caption","the caption"):
            texts = [e for e in scene.elements if e.type == "text"]
            if len(texts) == 1:
                action.target = texts[0].id
                scene.references["last_referenced"] = action.target
                return action

        # "text inside banner"
        if "banner" in low and "text" in low:
            bt = next((e for e in scene.elements if e.type == "text" and e.parentId), None)
            if bt:
                action.target = bt.id
                scene.references["last_referenced"] = action.target
                return action

        # Color match
        color_map = {
            "black": "#000000", "white": "#ffffff", "pink": "#ff69b4",
            "red": "#ff0000", "blue": "#0000ff", "yellow": "#ffff00",
        }
        for color_name, color_hex in color_map.items():
            if color_name in low:
                for el in scene.elements:
                    p = el.properties or {}
                    el_color = (p.get("backgroundColor") or p.get("bg_color") or "").lower()
                    if el_color == color_hex:
                        action.target = el.id
                        scene.references["last_referenced"] = action.target
                        return action

        # 5. Fabricated-ID prefix match ("top_banner_abc" → match first top banner)
        if low.startswith("top_banner") or low.startswith("banner_top"):
            cands = [e for e in scene.elements
                     if e.type == "banner"
                     and not (e.properties.get("position") or "top").lower().startswith("bottom")]
            if cands:
                action.target = cands[0].id
                scene.references["last_referenced"] = action.target
                return action

        if low.startswith("bottom_banner") or low.startswith("banner_bot") or low.startswith("bot_banner"):
            cands = [e for e in scene.elements
                     if e.type == "banner"
                     and (e.properties.get("position") or "").lower().startswith("bottom")]
            if cands:
                action.target = cands[0].id
                scene.references["last_referenced"] = action.target
                return action

        # 6. Fuzzy: role / type / text content / ID prefix
        found = next(
            (e for e in scene.elements
             if (e.role or "").lower() == low
             or e.type.lower() == low
             or low.startswith(e.type.lower() + "_")  # "text_1" → type "text"
             or low in (e.properties.get("text") or "").lower()
             or low in (e.properties.get("content") or "").lower()),
            None,
        )
        if found:
            action.target = found.id
            scene.references["last_referenced"] = found.id
            return action

        # 7. Single-element-of-type fallback
        # If there is only ONE banner, it must be what was meant
        banners = [e for e in scene.elements if e.type == "banner"]
        if len(banners) == 1 and action.action not in {"add_banner"}:
            action.target = banners[0].id
            scene.references["last_referenced"] = action.target
            return action

        texts = [e for e in scene.elements if e.type == "text"]
        if len(texts) == 1 and action.action not in {"add_text"}:
            action.target = texts[0].id
            scene.references["last_referenced"] = action.target
            return action

        return action


# ============================================================
# 9. SCENE EXECUTOR
# ============================================================

class SceneExecutor:

    def apply(self, scene: SceneGraph, plan: EditPlan) -> SceneGraph:
        new_scene = copy.deepcopy(scene)
        resolver  = ReferenceResolver()

        for action in plan.actions:
            action = resolver.resolve(action, new_scene)

            try:
                if action.action in {"add_text", "add_shape", "add_banner", "add_logo", "add_image"}:
                    self._add(new_scene, action)
                elif action.action in {"update_element", "style_element", "change_text",
                                       "move_element", "resize_element", "align_element"}:
                    self._update(new_scene, action)
                elif action.action == "delete_element":
                    self._delete(new_scene, action)
                elif action.action == "set_parent":
                    self._set_parent(new_scene, action)
                elif action.action == "crop_video":
                    self._crop_video(new_scene, action)
                elif action.action == "resize_video":
                    ratio = action.properties.get("aspect_ratio") or action.properties.get("ratio")
                    if ratio:
                        new_scene.canvas["aspectRatio"] = ratio
                elif action.action == "set_speed":
                    new_scene.canvas["speed"] = float(action.properties.get("speed", 1.0))
                elif action.action == "trim_video":
                    new_scene.canvas["trim"] = {
                        "start": action.properties.get("start"),
                        "end":   action.properties.get("end"),
                    }
                elif action.action == "set_background":
                    new_scene.canvas["background"] = action.properties.get("color")
                elif action.action == "bring_forward":
                    self._z_delta(new_scene, action, +1)
                elif action.action == "send_backward":
                    self._z_delta(new_scene, action, -1)
            except Exception as exc:
                print(f"[EXECUTOR] Error executing {action.action}: {exc}")

        new_scene.version += 1
        return new_scene

    # ── add ─────────────────────────────────────────────────────
    @staticmethod
    def _add(scene: SceneGraph, action: EditAction):
        el_type = {
            "add_text":   "text",
            "add_shape":  "shape",
            "add_banner": "banner",
            "add_logo":   "logo",
            "add_image":  "image",
        }[action.action]

        p = copy.deepcopy(action.properties)

        # Determine role
        if el_type == "banner":
            pos  = (p.get("position") or "top").lower()
            role = "bottom_banner" if "bottom" in pos else "top_banner"
        else:
            role = p.pop("role", None) or action.properties.get("role")

        # Extract parentId from properties or action
        parent_id = (
            action.properties.get("parentId") or
            action.properties.get("parent_id") or
            p.pop("parentId", None) or
            p.pop("parent_id", None)
        )

        # Default geometry
        if el_type == "banner":
            pos  = (p.get("position") or "top").lower()
            is_bot = "bottom" in pos
            bh = float(p.pop("height", 0.10))
            el = SceneElement(
                id       = action.id or f"{el_type}_{uuid.uuid4().hex[:8]}",
                type     = el_type,
                role     = role,
                parentId = None,
                x        = 0.0,
                y        = 1.0 - bh if is_bot else 0.0,
                width    = 1.0,
                height   = bh,
                zIndex   = 100,
                properties = p,
            )
        elif el_type == "text":
            if parent_id:
                el = SceneElement(
                    id       = action.id or f"text_{uuid.uuid4().hex[:8]}",
                    type     = "text",
                    role     = role or "text",
                    parentId = parent_id,
                    x        = 0.0,
                    y        = 0.0,
                    width    = 1.0,
                    height   = 1.0,
                    zIndex   = len(scene.elements) + 5,
                    properties = p,
                )
            else:
                pos_str = p.pop("position", "center")
                xy = SceneExecutor._resolve_position(pos_str)
                el = SceneElement(
                    id       = action.id or f"text_{uuid.uuid4().hex[:8]}",
                    type     = "text",
                    role     = role or "text",
                    parentId = None,
                    x        = float(p.pop("x", xy[0])),
                    y        = float(p.pop("y", xy[1])),
                    width    = float(p.pop("width", 1.0)),
                    height   = float(p.pop("height", 0.12)),
                    zIndex   = len(scene.elements) + 5,
                    properties = p,
                )
        else:
            pos_str = p.pop("position", "center")
            xy = SceneExecutor._resolve_position(pos_str)
            el = SceneElement(
                id       = action.id or f"{el_type}_{uuid.uuid4().hex[:8]}",
                type     = el_type,
                role     = role,
                parentId = parent_id,
                x        = float(p.pop("x", xy[0])),
                y        = float(p.pop("y", xy[1])),
                width    = float(p.pop("width", 0.25)),
                height   = float(p.pop("height", 0.25)),
                zIndex   = len(scene.elements) + 3,
                properties = p,
            )

        # Remove existing banner at same position (only one per slot)
        if el_type == "banner":
            is_bot = "bottom" in (el.properties.get("position") or "top").lower()
            scene.elements = [
                e for e in scene.elements
                if not (
                    e.type == "banner" and
                    ("bottom" in (e.properties.get("position") or "top").lower()) == is_bot
                )
            ]

        # Sync text/content
        if "text" in p and "content" not in p:  p["content"] = p["text"]
        if "content" in p and "text" not in p:  p["text"]    = p["content"]

        scene.elements.append(el)
        scene.references["last_created"]    = el.id
        scene.references["last_referenced"] = el.id
        print(f"[EXECUTOR] Added {el_type} {el.id} parentId={el.parentId} y={el.y:.2f}")

    @staticmethod
    def _resolve_position(pos: str):
        """Return (x, y) normalized for common position strings."""
        MAP = {
            "top-left":     (0.01, 0.01),
            "top-center":   (0.0,  0.02),
            "top-right":    (0.6,  0.01),
            "top":          (0.0,  0.02),
            "center-left":  (0.01, 0.40),
            "center":       (0.0,  0.40),
            "middle":       (0.0,  0.40),
            "center-right": (0.6,  0.40),
            "bottom-left":  (0.01, 0.82),
            "bottom-center":(0.0,  0.82),
            "bottom-right": (0.6,  0.82),
            "bottom":       (0.0,  0.82),
        }
        key = (pos or "center").lower().replace(" ", "-")
        return MAP.get(key, MAP.get(key.replace("-center",""), (0.0, 0.40)))

    # ── update ───────────────────────────────────────────────────
    @staticmethod
    def _update(scene: SceneGraph, action: EditAction):
        if not action.target:
            return
        el = scene.get_element(action.target)
        if not el:
            print(f"[EXECUTOR] update: element '{action.target}' not found")
            return

        p = action.properties

        # Handle geometric updates (move_element / resize_element)
        if action.action == "move_element":
            pos_str = p.get("position")
            if pos_str:
                xy = SceneExecutor._resolve_position(pos_str)
                if "x" not in p: el.x = xy[0]
                if "y" not in p: el.y = xy[1]
                # Banner vertical snap
                if el.type == "banner":
                    lp = pos_str.lower()
                    if "bottom" in lp:
                        el.y = 1.0 - el.height
                        el.properties["position"] = "bottom"
                        el.role = "bottom_banner"
                    elif "top" in lp:
                        el.y = 0.0
                        el.properties["position"] = "top"
                        el.role = "top_banner"
            if "x" in p: el.x = float(p["x"])
            if "y" in p: el.y = float(p["y"])
            if "deltaX" in p: el.x = max(0, el.x + float(p["deltaX"]))
            if "deltaY" in p: el.y = max(0, el.y + float(p["deltaY"]))
            scene.references["last_referenced"] = el.id
            return

        if action.action == "resize_element":
            if "width"  in p: el.width  = float(p["width"])
            if "height" in p:
                el.height = float(p["height"])
                # Update banner y for bottom banners
                if el.type == "banner" and "bottom" in (el.properties.get("position") or "").lower():
                    el.y = 1.0 - el.height
            if "fontSize"  in p: el.properties["fontSize"]  = p["fontSize"]
            if "font_size" in p: el.properties["fontSize"]  = p["font_size"]
            scene.references["last_referenced"] = el.id
            return

        if action.action == "align_element":
            alignment = p.get("alignment") or p.get("position") or "center"
            xy = SceneExecutor._resolve_position(alignment)
            el.x = xy[0]; el.y = xy[1]
            el.properties["textAlign"] = (
                "right" if "right" in alignment else
                "left"  if "left"  in alignment else "center"
            )
            scene.references["last_referenced"] = el.id
            return

        if action.action == "change_text":
            txt = p.get("text") or p.get("content")
            if txt is not None:
                el.properties["text"]    = txt
                el.properties["content"] = txt
            scene.references["last_referenced"] = el.id
            return

        # General property update (style_element / update_element)
        for k, v in p.items():
            if isinstance(v, dict) and "operation" in v:
                # Relative operation
                op  = v["operation"]
                amt = v.get("amount", 0)
                cur = el.properties.get(k)
                if isinstance(cur, (int, float)):
                    unit = v.get("unit", "")
                    if unit == "percent":
                        nv = cur * (1 + amt/100) if op == "increase" else cur * (1 - amt/100)
                    else:
                        nv = cur + amt if op == "increase" else max(1, cur - amt)
                    el.properties[k] = nv
                    continue
            el.properties[k] = v

        # Sync text/content
        if "text" in el.properties and "content" not in el.properties:
            el.properties["content"] = el.properties["text"]
        if "content" in el.properties and "text" not in el.properties:
            el.properties["text"] = el.properties["content"]

        scene.references["last_referenced"] = el.id

    # ── delete ───────────────────────────────────────────────────
    @staticmethod
    def _delete(scene: SceneGraph, action: EditAction):
        if not action.target:
            print(f"[EXECUTOR] _delete: no target — skipping")
            return
        before = len(scene.elements)
        scene.elements = [e for e in scene.elements if e.id != action.target]
        after = len(scene.elements)
        for k in ("last_created", "last_referenced"):
            if scene.references.get(k) == action.target:
                scene.references.pop(k, None)
        print(f"[EXECUTOR] _delete target={action.target}: removed {before-after} element(s) (was {before}, now {after})")

    # ── set_parent ───────────────────────────────────────────────
    @staticmethod
    def _set_parent(scene: SceneGraph, action: EditAction):
        if not action.target:
            return
        el = scene.get_element(action.target)
        if not el:
            return
        pid = action.properties.get("parentId") or action.properties.get("parent_id")
        el.parentId = pid
        if pid:
            el.x = 0.0; el.y = 0.0; el.width = 1.0; el.height = 1.0
        scene.references["last_referenced"] = el.id

    # ── crop_video ───────────────────────────────────────────────
    @staticmethod
    def _crop_video(scene: SceneGraph, action: EditAction):
        ratio = action.properties.get("aspect_ratio") or action.properties.get("ratio")
        if ratio:
            scene.canvas["aspectRatio"] = ratio
            # Compute crop from video dimensions
            vw = scene.video.width or 0
            vh = scene.video.height or 0
            if vw and vh:
                try:
                    parts = str(ratio).split(":")
                    r = float(parts[0]) / float(parts[1])
                    if vw / vh > r:
                        cw = vh * r; ch = vh
                    else:
                        cw = vw; ch = vw / r
                    cx = (vw - cw) / 2 / vw
                    cy = (vh - ch) / 2 / vh
                    scene.canvas["crop"] = {
                        "applied": True,
                        "x": cx, "y": cy,
                        "w": cw/vw, "h": ch/vh,
                    }
                except Exception as e:
                    print(f"[EXECUTOR] crop_video ratio parse error: {e}")
        else:
            nx = action.properties.get("nx")
            if nx is not None:
                scene.canvas["crop"] = {
                    "applied": True,
                    "x": nx, "y": action.properties.get("ny", 0),
                    "w": action.properties.get("nw", 1), "h": action.properties.get("nh", 1),
                }

    # ── z-order ───────────────────────────────────────────────────
    @staticmethod
    def _z_delta(scene: SceneGraph, action: EditAction, delta: int):
        if not action.target:
            return
        el = scene.get_element(action.target)
        if el:
            amt = int(action.properties.get("amount", 1))
            el.zIndex = max(0, el.zIndex + delta * amt)
            scene.references["last_referenced"] = el.id


# ============================================================
# 10. CONVERSATION MEMORY
# ============================================================

class ConversationMemory:

    def __init__(self, max_messages: int = 20):
        self.max_messages = max_messages
        self.messages: List[dict] = []

    def add_user(self, message: str):
        self.messages.append({"role": "user", "content": message})
        self._trim()

    def add_assistant(self, message: str, reasoning_details=None):
        item = {"role": "assistant", "content": message}
        # Preserve OpenRouter chain-of-thought (when present) so it can be
        # forwarded verbatim on the next turn for continuation. Gated on
        # presence so an absent/empty value is never echoed back.
        if reasoning_details:
            item["reasoning_details"] = reasoning_details
        self.messages.append(item)
        self._trim()

    def get(self) -> List[dict]:
        return list(self.messages)

    def clear(self):
        self.messages = []

    def _trim(self):
        if len(self.messages) > self.max_messages:
            self.messages = self.messages[-self.max_messages:]


# ============================================================
# 11. HIGH-LEVEL AutoquenceAI
# ============================================================

class AutoquenceAI:

    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-3.1-flash-lite"):
        self.planner   = AutoquencePlanner(api_key=api_key, model=model)
        self.validator = EditPlanValidator()
        self.executor  = SceneExecutor()
        self.memory    = ConversationMemory()

    def process(
        self,
        user_prompt: str,
        scene: SceneGraph,
        available_assets=None,
        available_fonts=None,
        apply_plan: bool = True,
        conversation_history=None,
        answering_pending_question: bool = False,
        pending_question: str = None,
        vague_prompt: bool = False,
    ) -> dict:

        # Snapshot history BEFORE adding the current prompt, so the
        # planner does not receive the current user message twice
        # (once via history and once as the live user message).
        # An explicit conversation_history (sent by the client) takes
        # precedence over the server-side memory.
        if conversation_history:
            history = list(conversation_history)[-8:]
        else:
            history = self.memory.get()
        if not conversation_history:
            self.memory.add_user(user_prompt)

        # 1. AI understanding
        ai_data = self.planner.plan(
            user_prompt          = user_prompt,
            scene                = scene,
            available_assets     = available_assets,
            available_fonts      = available_fonts,
            conversation_history = history,
            answering_pending_question = answering_pending_question,
            pending_question     = pending_question,
            vague_prompt         = vague_prompt,
        )

        # If the planner completely failed (API down / unparseable),
        # report it honestly instead of pretending no edit was found.
        if not ai_data:
            planner_error = getattr(self.planner, "last_error", None)
            print("[AI] !!! Planner returned EMPTY result -> 'AI service didn't respond'")
            print(f"[AI] Prompt was: '{user_prompt[:120]}'")
            print(f"[AI] Planner last error: {planner_error}")
            print("[AI] (API error / retries exhausted / unparseable JSON).")
            error_hint = (
                f" The error was: {planner_error}"
                if planner_error
                else " Please try again."
            )
            return {
                "response_type":  "conversation",
                "message": (
                    "The AI service didn't respond." + error_hint
                ),
                "intent_summary": "",
                "plan": {"actions": [], "assumptions": []},
                "scene": None,
            }

        print(f"[AI] response_type={ai_data.get('response_type')} actions={len(ai_data.get('actions', []))}")

        # 2. Validate
        validated = self.validator.validate(ai_data, scene)
        response_type  = validated["response_type"]
        message        = validated["message"]
        intent_summary = validated["intent_summary"]
        actions        = validated["actions"]
        assumptions    = validated["assumptions"]

        # 3. Execute
        plan      = EditPlan(actions=actions, assumptions=assumptions)
        new_scene = scene

        if response_type == "edit" and actions and apply_plan:
            new_scene = self.executor.apply(scene, plan)

        if message:
            self.memory.add_assistant(
                message,
                reasoning_details=getattr(self.planner, "last_reasoning_details", None),
            )

        # Chain-of-thought from this turn's reply, surfaced to the client so
        # it can store it in history and forward it for multi-turn reasoning.
        reasoning_details = getattr(self.planner, "last_reasoning_details", None)

        # 4. Build response
        return {
            "response_type":  response_type,
            "message":        message,
            "intent_summary": intent_summary,
            "reasoning_details": reasoning_details,
            "plan": {
                "actions": [
                    {
                        "action":      a.action,
                        "target":      a.target,
                        "target_role": a.target_role,
                        "id":          a.id,
                        "properties":  a.properties,
                        "reason":      a.reason,
                    }
                    for a in actions
                ],
                "assumptions": assumptions,
            },
            "scene": new_scene.to_dict() if new_scene is not None else None,
        }


# ============================================================
# 12. TEST HELPERS
# ============================================================

def make_test_scene():
    return SceneGraph(
        video    = VideoInfo(width=1920, height=1080, duration=12.4, filename="test.mp4"),
        canvas   = {"aspectRatio": "16:9", "background": None, "speed": 1.0},
        elements = [],
        references = {},
    )


def print_result(result):
    print()
    print("-" * 70)
    print(f"TYPE:    {result['response_type']}")
    print(f"INTENT:  {result['intent_summary']}")
    print(f"MESSAGE: {result['message']}")
    for i, a in enumerate(result["plan"]["actions"], 1):
        print(f"  Action {i}: {a['action']}")
        if a["target"]:      print(f"    target:   {a['target']}")
        if a["target_role"]: print(f"    role:     {a['target_role']}")
        if a["id"]:          print(f"    id:       {a['id']}")
        if a["properties"]:
            print(f"    props:    {json.dumps(a['properties'], ensure_ascii=False)}")
    if result.get("scene"):
        els = result["scene"].get("elements", [])
        print(f"\n  Scene elements ({len(els)}):")
        for e in els:
            pid = f" parentId={e['parentId']}" if e.get('parentId') else ""
            print(f"    [{e['id']}] {e['type']} x={e.get('x',0):.2f} y={e.get('y',0):.2f} w={e.get('width',1):.2f} h={e.get('height',0.1):.2f}{pid}")
    print("-" * 70)


def main():
    print("\n" + "=" * 70)
    print("AUTOQUENCE AI V3 — Canonical Scene Model")
    print("=" * 70)
    print("Type /help, /scene, /reset, /exit\n")

    ai    = AutoquenceAI()
    scene = make_test_scene()

    available_fonts = [
        "Impact", "Arial", "Arial Black", "Montserrat", "Inter",
        "Roboto", "Poppins", "Georgia", "Verdana", "Trebuchet MS",
        "Oswald", "Raleway", "Open Sans", "Lato", "Nunito",
        "Bebas Neue", "Anton",
    ]

    while True:
        try:
            prompt = input("You > ").strip()
        except (KeyboardInterrupt, EOFError):
            break

        if not prompt: continue
        if prompt.lower() in ("/exit", "exit", "quit", "q"): break
        if prompt.lower() == "/scene":
            print(json.dumps(scene.to_dict(), indent=2, ensure_ascii=False)); continue
        if prompt.lower() == "/reset":
            scene = make_test_scene(); ai.memory.clear()
            print("Reset.\n"); continue
        if prompt.lower() == "/help":
            print("/scene  /reset  /exit\n"); continue

        try:
            result = ai.process(prompt, scene, available_fonts=available_fonts)
            print_result(result)
            if result["response_type"] == "edit" and result.get("scene"):
                sd = result["scene"]
                scene = SceneGraph(
                    video      = VideoInfo(
                        width    = sd["video"].get("width", 0),
                        height   = sd["video"].get("height", 0),
                        duration = sd["video"].get("duration", 0.0),
                        filename = sd["video"].get("filename"),
                    ),
                    elements   = [
                        SceneElement(
                            id         = e["id"],
                            type       = e["type"],
                            role       = e.get("role"),
                            parentId   = e.get("parentId"),
                            x          = e.get("x", 0.0),
                            y          = e.get("y", 0.0),
                            width      = e.get("width", 1.0),
                            height     = e.get("height", 0.1),
                            zIndex     = e.get("zIndex", 0),
                            properties = e.get("properties", {}),
                        )
                        for e in sd.get("elements", [])
                    ],
                    canvas     = sd.get("canvas", {}),
                    references = sd.get("references", {}),
                    version    = sd.get("version", 0),
                )
        except Exception as exc:
            print(f"\nERROR: {exc}\n")
            import traceback; traceback.print_exc()


if __name__ == "__main__":
    main()
