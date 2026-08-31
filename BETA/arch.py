"""
============================================================
AUTOQUENCE AI V3
============================================================

Conversational Natural Language Video Editing Planner

This version DOES NOT edit/render actual videos.

It simulates the editing state using a SceneGraph.

The goal is to test:

    User prompt
        ↓
    Current Scene
        ↓
    AI understands context
        ↓
    Edit OR clarification
        ↓
    Execute abstract edit
        ↓
    Updated Scene
        ↓
    Next user prompt sees updated Scene

Example:

Autoquence > Add a red banner at the top

AI:
Creates a red top banner.

Scene:
- text_1
- banner_abc123

Autoquence > Make it blue

AI:
Understands "it" = banner_abc123.

Scene:
- text_1
- banner_abc123 (blue)

Autoquence > Make the text red

AI:
Finds existing text_1 and changes its color.

Autoquence > Make the other text red

AI:
There is only one text element, so it asks for clarification.

Commands:

    undo
    redo
    show scene
    show history
    reset
    exit

IMPORTANT:

This version does NOT expose private chain-of-thought.

Instead, the AI returns:
- intent_summary
- concise action reasons
- clarification messages
"""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from openai import OpenAI


# ============================================================
# 1. EDITING LANGUAGE
# ============================================================

ACTION_REGISTRY = {
    "add_text": {
        "description": "Create a text/caption element.",
        "properties": [
            "content",
            "position",
            "font",
            "font_size",
            "font_weight",
            "text_color",
            "background_color",
            "background_opacity",
            "padding",
            "line_spacing",
            "alignment",
            "width",
            "height",
        ],
    },

    "add_shape": {
        "description": "Create a rectangle, banner, box, or simple shape.",
        "properties": [
            "shape",
            "role",
            "position",
            "x",
            "y",
            "width",
            "height",
            "fill",
            "opacity",
            "border_color",
            "border_width",
            "radius",
        ],
    },

    "add_logo": {
        "description": "Place an uploaded logo.",
        "properties": [
            "asset_id",
            "position",
            "x",
            "y",
            "width",
            "height",
            "scale",
            "opacity",
        ],
    },

    "add_image": {
        "description": "Place an uploaded image.",
        "properties": [
            "asset_id",
            "position",
            "x",
            "y",
            "width",
            "height",
            "opacity",
        ],
    },

    "update_element": {
        "description": "Change properties of an existing element.",
        "properties": "any supported element properties",
    },

    "move_element": {
        "description": "Move an existing element.",
        "properties": [
            "position",
            "x",
            "y",
            "relative",
        ],
    },

    "resize_element": {
        "description": "Resize an existing element.",
        "properties": [
            "width",
            "height",
            "scale",
            "relative",
        ],
    },

    "change_text": {
        "description": "Change existing text content.",
        "properties": [
            "content",
        ],
    },

    "style_element": {
        "description": "Change visual styling.",
        "properties": [
            "font",
            "font_size",
            "font_weight",
            "text_color",
            "fill",
            "background_color",
            "background_opacity",
            "opacity",
            "padding",
            "line_spacing",
            "alignment",
            "border_color",
            "border_width",
            "radius",
        ],
    },

    "delete_element": {
        "description": "Delete an existing element.",
        "properties": [],
    },

    "crop_video": {
        "description": "Change video aspect ratio/crop.",
        "properties": [
            "aspect_ratio",
        ],
    },

    "resize_video": {
        "description": "Change output dimensions.",
        "properties": [
            "width",
            "height",
            "preset",
        ],
    },

    "set_speed": {
        "description": "Change playback speed.",
        "properties": [
            "speed",
        ],
    },

    "trim_video": {
        "description": "Trim video.",
        "properties": [
            "start",
            "end",
        ],
    },

    "set_background": {
        "description": "Change video/canvas background.",
        "properties": [
            "color",
            "image_asset_id",
        ],
    },

    "align_element": {
        "description": "Align an element.",
        "properties": [
            "alignment",
            "reference",
        ],
    },

    "bring_forward": {
        "description": "Move element forward in layer order.",
        "properties": [
            "amount",
        ],
    },

    "send_backward": {
        "description": "Move element backward in layer order.",
        "properties": [
            "amount",
        ],
    },
}


# ============================================================
# 2. SCENE GRAPH
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

    properties: Dict[str, Any] = field(
        default_factory=dict
    )

    z_index: int = 0


@dataclass
class SceneGraph:

    video: VideoInfo

    elements: List[SceneElement] = field(
        default_factory=list
    )

    canvas: Dict[str, Any] = field(
        default_factory=lambda: {
            "aspect_ratio": "original",
            "background": "#000000",
        }
    )

    references: Dict[str, str] = field(
        default_factory=dict
    )

    version: int = 0

    def to_dict(self):

        return asdict(self)

    def get_element(
        self,
        element_id: str,
    ):

        return next(
            (
                element
                for element in self.elements
                if element.id == element_id
            ),
            None,
        )

    def find_by_role(
        self,
        role: str,
    ):

        return [
            element
            for element in self.elements
            if element.role == role
        ]

    def find_by_type(
        self,
        element_type: str,
    ):

        return [
            element
            for element in self.elements
            if element.type == element_type
        ]


# ============================================================
# 3. EDIT ACTION
# ============================================================

@dataclass
class EditAction:

    action: str

    target: Optional[str] = None

    target_role: Optional[str] = None

    id: Optional[str] = None

    properties: Dict[str, Any] = field(
        default_factory=dict
    )

    reason: Optional[str] = None


# ============================================================
# 4. EDIT PLAN
# ============================================================

@dataclass
class EditPlan:

    response_type: str

    intent_summary: str

    actions: List[EditAction] = field(
        default_factory=list
    )

    assumptions: List[str] = field(
        default_factory=list
    )

    message: Optional[str] = None


# ============================================================
# 5. SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = r"""
You are Autoquence's conversational video editing intelligence.

You are the intelligence layer of a video editor.

The user talks naturally.

Your job is to understand what they mean, inspect the current
editor state, and either:

A. produce a structured edit plan

OR

B. respond conversationally when the request is ambiguous,
impossible to resolve safely, or refers to something that does
not exist.

You do NOT render video.

You do NOT generate FFmpeg.

You do NOT generate Python.

You do NOT generate JavaScript.

The application executes your edit plan.

============================================================
IMPORTANT: NO PRIVATE CHAIN OF THOUGHT
============================================================

Do not expose private chain-of-thought.

Instead provide:

intent_summary:
A concise explanation of what the user wants.

reason:
A concise explanation of why each action is appropriate.

============================================================
RESPONSE TYPES
============================================================

You MUST return one of:

"edit"

or

"clarification"

or

"conversation"

Use:

"edit"
when the request is sufficiently clear and can be executed.

"clarification"
when the user refers to something that is missing,
ambiguous, or cannot safely be identified.

"conversation"
when the user is simply talking, asking a general question,
or no editing action is required.

============================================================
CURRENT SCENE IS AUTHORITATIVE
============================================================

The current editor context is provided in the user payload.

It contains the current SceneGraph.

The SceneGraph represents the current state AFTER previous
edits.

You MUST use it.

Do not assume that the scene is still in its original state.

If the user previously created:

banner_abc123

and then says:

"make it blue"

you should understand that "it" refers to the relevant
existing element.

============================================================
CONVERSATION CONTEXT
============================================================

The system may provide:

- previous user messages
- previous AI responses
- recent actions
- current scene
- references

Use this context to resolve natural language.

============================================================
REFERENCE RESOLUTION
============================================================

Users naturally say:

"it"

"that"

"this"

"the text"

"the banner"

"the one at the top"

"the other one"

"the second caption"

"make it bigger"

"move that down"

"remove this"

Resolve references using:

1. Explicit target.
2. Exact semantic role.
3. Description/location.
4. Current references.
5. Most recently referenced element.
6. Most recently created element.

============================================================
CRITICAL: DO NOT HALLUCINATE ELEMENTS
============================================================

If the user says:

"make the text red"

but the scene contains no text elements:

DO NOT invent a text element.

Instead return:

{
    "response_type": "clarification",
    "intent_summary": "The user wants text styled red.",
    "actions": [],
    "message": "I don't see any text elements in the current scene. Would you like me to add some text first?",
    "assumptions": []
}

============================================================
AMBIGUOUS REFERENCES
============================================================

If there are multiple possible targets:

User:
"make the text red"

Scene:
- text_1
- text_2
- text_3

Do not randomly choose one.

Ask for clarification.

Example:

"I found three text elements. Which one should I change?"

============================================================
MISSING OBJECTS
============================================================

If the requested object does not exist:

User:
"make the logo bigger"

Scene:
(no logo)

Return clarification.

Example:

"I don't see a logo in the current scene. Add one first?"

============================================================
CONTEXTUAL FOLLOW-UP
============================================================

Example:

User:
"Add a red banner at the top."

AI creates:
banner_123

User:
"Make it blue."

Interpret:
"it" = banner_123.

DO NOT ask:
"Which element do you mean?"

unless the reference genuinely cannot be resolved.

============================================================
CREATE VS MODIFY
============================================================

"add"
"put"
"create"
"insert"

means create a new element.

"make"
"change"
"move"
"resize"
"remove"
"delete"

means modify an existing element when the target exists.

============================================================
NATURAL LANGUAGE
============================================================

Understand:

"slightly bigger"
"much bigger"
"a little smaller"
"move it down"
"put it in the middle"
"make it darker"
"make it more transparent"

Infer sensible values.

============================================================
POSITIONS
============================================================

Understand:

top
top center
top left
top right

center
middle

bottom
bottom center
bottom left
bottom right

left
right

Prefer semantic positions over raw pixels.

============================================================
BANNERS
============================================================

"red banner"

means:

add_shape

"red banner saying HELLO"

means:

1. add shape
2. add text

The text should reference the banner.

============================================================
COMPLEX REQUESTS
============================================================

For:

"Make this a TikTok meme, crop it vertical,
add a black banner at the top, and put huge
white text saying NO WAY inside it."

Return:

1. crop video
2. add banner
3. add text
4. style if necessary

Actions must be in dependency order.

============================================================
PRESERVE EXISTING WORK
============================================================

Do not delete or replace existing elements unless requested.

If the user says:

"change the banner from red to blue"

modify the existing banner.

Do NOT create another banner.

============================================================
ASSETS
============================================================

Never invent asset IDs.

Only use IDs in available_assets.

Never invent unavailable fonts if available_fonts is supplied.

============================================================
OUTPUT
============================================================

Return ONLY valid JSON.

For an edit:

{
    "response_type": "edit",
    "intent_summary": "Change the existing top banner from red to blue.",
    "actions": [
        {
            "action": "style_element",
            "target": "banner_123",
            "properties": {
                "fill": "#0000FF"
            },
            "reason": "The user wants the existing banner changed from red to blue."
        }
    ],
    "assumptions": [],
    "message": null
}

For clarification:

{
    "response_type": "clarification",
    "intent_summary": "The user wants the text changed.",
    "actions": [],
    "assumptions": [],
    "message": "I don't see any text elements in the current scene. Would you like me to add some text first?"
}

For conversation:

{
    "response_type": "conversation",
    "intent_summary": "The user is asking a general question.",
    "actions": [],
    "assumptions": [],
    "message": "..."
}

No Markdown.
No code fences.
No explanation outside JSON.
"""


# ============================================================
# 6. CONTEXT BUILDER
# ============================================================

def build_context(
    scene: SceneGraph,
    available_assets=None,
    available_fonts=None,
    conversation_history=None,
):

    return {

        "current_editor_state": {

            "video": asdict(
                scene.video
            ),

            "canvas": scene.canvas,

            "elements": [

                {
                    "id": element.id,

                    "type": element.type,

                    "role": element.role,

                    "properties": element.properties,

                    "z_index": element.z_index,
                }

                for element in scene.elements
            ],

            "references": scene.references,

            "version": scene.version,
        },

        "available_assets":
            available_assets or [],

        "available_fonts":
            available_fonts or [],

        "action_registry":
            ACTION_REGISTRY,

        "conversation_history":
            conversation_history or [],
    }


# ============================================================
# 7. AI PLANNER
# ============================================================

class AutoquencePlanner:

    def __init__(
        self,
        api_key=None,
        model="openai/gpt-4o-mini",
        base_url="https://openrouter.ai/api/v1",
    ):

        self.api_key = (
            api_key
            or os.getenv(
                "OPENROUTER_API_KEY"
            )
        )

        if not self.api_key:

            raise RuntimeError(
                "OPENROUTER_API_KEY is missing."
            )

        self.model = model

        self.client = OpenAI(
            api_key=self.api_key,
            base_url=base_url,
        )

    def plan(
        self,
        user_prompt: str,
        scene: SceneGraph,
        available_assets=None,
        available_fonts=None,
        conversation_history=None,
    ):

        payload = {

            "user_request":
                user_prompt,

            "editor_context":
                build_context(
                    scene=scene,
                    available_assets=available_assets,
                    available_fonts=available_fonts,
                    conversation_history=conversation_history,
                ),
        }

        response = (
            self.client
            .chat
            .completions
            .create(

                model=self.model,

                temperature=0.1,

                response_format={
                    "type": "json_object"
                },

                messages=[

                    {
                        "role": "system",
                        "content": SYSTEM_PROMPT,
                    },

                    {
                        "role": "user",
                        "content": json.dumps(
                            payload,
                            ensure_ascii=False,
                        ),
                    },
                ],
            )
        )

        raw = (
            response
            .choices[0]
            .message
            .content
            or "{}"
        )

        # ----------------------------------------------------
        # Parse JSON
        # ----------------------------------------------------

        try:

            data = json.loads(
                raw
            )

        except json.JSONDecodeError:

            match = re.search(
                r"\{.*\}",
                raw,
                re.DOTALL,
            )

            if not match:

                raise ValueError(
                    "AI returned invalid JSON:\n"
                    + raw
                )

            data = json.loads(
                match.group(0)
            )

        # ----------------------------------------------------
        # Response type
        # ----------------------------------------------------

        response_type = data.get(
            "response_type",
            "clarification",
        )

        if response_type not in {
            "edit",
            "clarification",
            "conversation",
        }:

            response_type = "clarification"

        # ----------------------------------------------------
        # Intent
        # ----------------------------------------------------

        intent_summary = data.get(
            "intent_summary",
            "",
        )

        if not isinstance(
            intent_summary,
            str,
        ):

            intent_summary = str(
                intent_summary
            )

        # ----------------------------------------------------
        # Message
        # ----------------------------------------------------

        message = data.get(
            "message"
        )

        if message is not None:

            if not isinstance(
                message,
                str,
            ):

                message = str(
                    message
                )

        # ----------------------------------------------------
        # Actions
        # ----------------------------------------------------

        actions = []

        raw_actions = data.get(
            "actions",
            [],
        )

        if not isinstance(
            raw_actions,
            list,
        ):

            raw_actions = []

        for item in raw_actions:

            if not isinstance(
                item,
                dict,
            ):

                continue

            actions.append(
                EditAction(

                    action=item.get(
                        "action",
                        "",
                    ),

                    target=item.get(
                        "target"
                    ),

                    target_role=item.get(
                        "target_role"
                    ),

                    id=item.get(
                        "id"
                    ),

                    properties=item.get(
                        "properties",
                        {},
                    ),

                    reason=item.get(
                        "reason"
                    ),
                )
            )

        # ----------------------------------------------------
        # Assumptions
        # ----------------------------------------------------

        assumptions = data.get(
            "assumptions",
            [],
        )

        if not isinstance(
            assumptions,
            list,
        ):

            assumptions = []

        return EditPlan(

            response_type=response_type,

            intent_summary=intent_summary,

            actions=actions,

            assumptions=assumptions,

            message=message,
        )


# ============================================================
# 8. VALIDATOR
# ============================================================

class EditPlanValidator:

    ASPECT_RATIOS = {
        "16:9",
        "9:16",
        "1:1",
        "4:3",
    }

    TARGET_ACTIONS = {

        "update_element",

        "move_element",

        "resize_element",

        "change_text",

        "style_element",

        "delete_element",

        "align_element",

        "bring_forward",

        "send_backward",
    }

    CREATE_ACTIONS = {

        "add_text",

        "add_shape",

        "add_logo",

        "add_image",
    }

    def validate(
        self,
        plan: EditPlan,
        scene: SceneGraph,
    ):

        valid = []

        existing_ids = {
            element.id
            for element in scene.elements
        }

        created_ids = set()

        for action in plan.actions:

            # ------------------------------------------------
            # Unknown action
            # ------------------------------------------------

            if (
                action.action
                not in ACTION_REGISTRY
            ):

                continue

            # ------------------------------------------------
            # Target validation
            # ------------------------------------------------

            if action.action in self.TARGET_ACTIONS:

                has_target = (

                    action.target
                    in existing_ids

                    or

                    action.target
                    in created_ids

                    or

                    bool(
                        action.target_role
                    )
                )

                if not has_target:

                    continue

            # ------------------------------------------------
            # Generate missing create IDs
            # ------------------------------------------------

            if action.action in self.CREATE_ACTIONS:

                if not action.id:

                    prefix = {

                        "add_text":
                            "text",

                        "add_shape":
                            "shape",

                        "add_logo":
                            "logo",

                        "add_image":
                            "image",

                    }[
                        action.action
                    ]

                    action.id = (
                        f"{prefix}_"
                        f"{uuid.uuid4().hex[:8]}"
                    )

                created_ids.add(
                    action.id
                )

            # ------------------------------------------------
            # Crop validation
            # ------------------------------------------------

            if action.action == "crop_video":

                ratio = action.properties.get(
                    "aspect_ratio"
                )

                if ratio not in self.ASPECT_RATIOS:

                    continue

            # ------------------------------------------------
            # Speed validation
            # ------------------------------------------------

            if action.action == "set_speed":

                speed = action.properties.get(
                    "speed"
                )

                if not isinstance(
                    speed,
                    (int, float),
                ):

                    continue

                if speed <= 0 or speed > 4:

                    continue

            # ------------------------------------------------
            # Text validation
            # ------------------------------------------------

            if action.action in {
                "add_text",
                "change_text",
            }:

                content = action.properties.get(
                    "content"
                )

                if (
                    content is not None
                    and not isinstance(
                        content,
                        str,
                    )
                ):

                    continue

            valid.append(
                action
            )

        return EditPlan(

            response_type=
                plan.response_type,

            intent_summary=
                plan.intent_summary,

            actions=valid,

            assumptions=
                plan.assumptions,

            message=
                plan.message,
        )


# ============================================================
# 9. REFERENCE RESOLVER
# ============================================================

class ReferenceResolver:

    def resolve(
        self,
        action: EditAction,
        scene: SceneGraph,
    ):

        # ----------------------------------------------------
        # Explicit ID
        # ----------------------------------------------------

        if (
            action.target
            and scene.get_element(
                action.target
            )
        ):

            return action

        # ----------------------------------------------------
        # Role
        # ----------------------------------------------------

        if action.target_role:

            matches = (
                scene.find_by_role(
                    action.target_role
                )
            )

            if len(matches) == 1:

                action.target = (
                    matches[0].id
                )

                return action

        # ----------------------------------------------------
        # Natural reference
        # ----------------------------------------------------

        natural_targets = {
            "it",
            "that",
            "this",
            "last_created",
            "last_referenced",
        }

        if (
            action.target
            in natural_targets
        ):

            target = (
                scene.references.get(
                    action.target
                )
                or
                scene.references.get(
                    "last_referenced"
                )
                or
                scene.references.get(
                    "last_created"
                )
            )

            if target:

                action.target = target

        return action


# ============================================================
# 10. SCENE EXECUTOR
# ============================================================

class SceneExecutor:

    def apply(
        self,
        scene: SceneGraph,
        plan: EditPlan,
    ):

        new_scene = copy.deepcopy(
            scene
        )

        resolver = (
            ReferenceResolver()
        )

        for action in plan.actions:

            action = resolver.resolve(
                action,
                new_scene,
            )

            # ----------------------------------------------
            # CREATE
            # ----------------------------------------------

            if action.action in {
                "add_text",
                "add_shape",
                "add_logo",
                "add_image",
            }:

                self._add(
                    new_scene,
                    action,
                )

            # ----------------------------------------------
            # UPDATE
            # ----------------------------------------------

            elif action.action in {
                "update_element",
                "style_element",
                "move_element",
                "resize_element",
                "change_text",
                "align_element",
            }:

                self._update(
                    new_scene,
                    action,
                )

            # ----------------------------------------------
            # DELETE
            # ----------------------------------------------

            elif (
                action.action
                == "delete_element"
            ):

                self._delete(
                    new_scene,
                    action,
                )

            # ----------------------------------------------
            # VIDEO
            # ----------------------------------------------

            elif (
                action.action
                == "crop_video"
            ):

                new_scene.canvas[
                    "aspect_ratio"
                ] = action.properties[
                    "aspect_ratio"
                ]

            elif (
                action.action
                == "resize_video"
            ):

                new_scene.canvas[
                    "width"
                ] = action.properties.get(
                    "width"
                )

                new_scene.canvas[
                    "height"
                ] = action.properties.get(
                    "height"
                )

                if action.properties.get(
                    "preset"
                ):

                    new_scene.canvas[
                        "preset"
                    ] = action.properties[
                        "preset"
                    ]

            elif (
                action.action
                == "set_speed"
            ):

                new_scene.canvas[
                    "speed"
                ] = action.properties[
                    "speed"
                ]

            elif (
                action.action
                == "set_background"
            ):

                new_scene.canvas[
                    "background"
                ] = action.properties

            elif (
                action.action
                == "trim_video"
            ):

                new_scene.canvas[
                    "trim"
                ] = {
                    "start":
                        action.properties.get(
                            "start"
                        ),

                    "end":
                        action.properties.get(
                            "end"
                        ),
                }

            elif (
                action.action
                == "bring_forward"
            ):

                self._bring_forward(
                    new_scene,
                    action,
                )

            elif (
                action.action
                == "send_backward"
            ):

                self._send_backward(
                    new_scene,
                    action,
                )

        new_scene.version += 1

        return new_scene

    # ========================================================
    # ADD
    # ========================================================

    @staticmethod
    def _add(
        scene: SceneGraph,
        action: EditAction,
    ):

        element_type = {

            "add_text":
                "text",

            "add_shape":
                "shape",

            "add_logo":
                "logo",

            "add_image":
                "image",

        }[
            action.action
        ]

        element = SceneElement(

            id=(
                action.id
                or
                f"{element_type}_"
                f"{uuid.uuid4().hex[:8]}"
            ),

            type=element_type,

            role=action.properties.get(
                "role"
            ),

            properties=copy.deepcopy(
                action.properties
            ),

            z_index=len(
                scene.elements
            ),
        )

        scene.elements.append(
            element
        )

        scene.references[
            "last_created"
        ] = element.id

        scene.references[
            "last_referenced"
        ] = element.id

    # ========================================================
    # UPDATE
    # ========================================================

    @staticmethod
    def _update(
        scene: SceneGraph,
        action: EditAction,
    ):

        if not action.target:

            return

        element = (
            scene.get_element(
                action.target
            )
        )

        if not element:

            return

        for key, value in (
            action.properties.items()
        ):

            # --------------------------------------------
            # Relative operation
            # --------------------------------------------

            if isinstance(
                value,
                dict,
            ):

                operation = value.get(
                    "operation"
                )

                current = (
                    element.properties.get(
                        key
                    )
                )

                if (
                    operation
                    in {
                        "increase",
                        "decrease",
                    }

                    and

                    isinstance(
                        current,
                        (int, float),
                    )
                ):

                    amount = value.get(
                        "amount",
                        0,
                    )

                    unit = value.get(
                        "unit"
                    )

                    if unit == "percent":

                        factor = (
                            amount / 100
                        )

                        if (
                            operation
                            == "increase"
                        ):

                            element.properties[
                                key
                            ] = (
                                current
                                * (
                                    1
                                    + factor
                                )
                            )

                        else:

                            element.properties[
                                key
                            ] = (
                                current
                                * (
                                    1
                                    - factor
                                )
                            )

                    else:

                        if (
                            operation
                            == "increase"
                        ):

                            element.properties[
                                key
                            ] = (
                                current
                                + amount
                            )

                        else:

                            element.properties[
                                key
                            ] = (
                                current
                                - amount
                            )

                    continue

                if (
                    operation
                    == "set"
                ):

                    element.properties[
                        key
                    ] = value.get(
                        "value"
                    )

                    continue

            # --------------------------------------------
            # Normal update
            # --------------------------------------------

            element.properties[
                key
            ] = value

        scene.references[
            "last_referenced"
        ] = element.id

    # ========================================================
    # DELETE
    # ========================================================

    @staticmethod
    def _delete(
        scene: SceneGraph,
        action: EditAction,
    ):

        if not action.target:

            return

        scene.elements = [

            element

            for element in scene.elements

            if element.id
            != action.target
        ]

        # If deleted element was referenced,
        # clear stale references.

        for key in [
            "last_created",
            "last_referenced",
        ]:

            if (
                scene.references.get(
                    key
                )
                == action.target
            ):

                scene.references.pop(
                    key,
                    None,
                )

    # ========================================================
    # BRING FORWARD
    # ========================================================

    @staticmethod
    def _bring_forward(
        scene: SceneGraph,
        action: EditAction,
    ):

        if not action.target:

            return

        element = (
            scene.get_element(
                action.target
            )
        )

        if not element:

            return

        amount = action.properties.get(
            "amount",
            1,
        )

        element.z_index += amount

        scene.elements.sort(
            key=lambda item:
                item.z_index
        )

    # ========================================================
    # SEND BACKWARD
    # ========================================================

    @staticmethod
    def _send_backward(
        scene: SceneGraph,
        action: EditAction,
    ):

        if not action.target:

            return

        element = (
            scene.get_element(
                action.target
            )
        )

        if not element:

            return

        amount = action.properties.get(
            "amount",
            1,
        )

        element.z_index -= amount

        scene.elements.sort(
            key=lambda item:
                item.z_index
        )


# ============================================================
# 11. CONVERSATION ENGINE
# ============================================================

class AutoquenceAI:

    def __init__(
        self,
        api_key=None,
        model="openai/gpt-4o-mini",
    ):

        self.planner = (
            AutoquencePlanner(
                api_key=api_key,
                model=model,
            )
        )

        self.validator = (
            EditPlanValidator()
        )

        self.resolver = (
            ReferenceResolver()
        )

        self.executor = (
            SceneExecutor()
        )

        # ----------------------------------------------------
        # Persistent conversation
        # ----------------------------------------------------

        self.conversation_history = []

        # ----------------------------------------------------
        # Undo / redo
        # ----------------------------------------------------

        self.undo_stack = []

        self.redo_stack = []

    # ========================================================
    # PROCESS MESSAGE
    # ========================================================

    def process(
        self,
        user_prompt: str,
        scene: SceneGraph,
        available_assets=None,
        available_fonts=None,
    ):

        # ----------------------------------------------------
        # Ask AI
        # ----------------------------------------------------

        plan = self.planner.plan(

            user_prompt=user_prompt,

            scene=scene,

            available_assets=
                available_assets,

            available_fonts=
                available_fonts,

            conversation_history=
                self.conversation_history,
        )

        # ----------------------------------------------------
        # Validate
        # ----------------------------------------------------

        plan = (
            self.validator.validate(
                plan,
                scene,
            )
        )

        # ----------------------------------------------------
        # Resolve references
        # ----------------------------------------------------

        for action in plan.actions:

            self.resolver.resolve(
                action,
                scene,
            )

        # ----------------------------------------------------
        # Store user message
        # ----------------------------------------------------

        self.conversation_history.append({

            "role":
                "user",

            "content":
                user_prompt,
        })

        # ----------------------------------------------------
        # If clarification/conversation
        # ----------------------------------------------------

        if (
            plan.response_type
            != "edit"
            or not plan.actions
        ):

            message = (
                plan.message
                or
                plan.intent_summary
                or
                "I need a little more information."
            )

            self.conversation_history.append({

                "role":
                    "assistant",

                "content":
                    message,
            })

            return {

                "response_type":
                    plan.response_type,

                "message":
                    message,

                "intent_summary":
                    plan.intent_summary,

                "actions":
                    [],

                "assumptions":
                    plan.assumptions,

                "scene":
                    scene.to_dict(),
            }

        # ----------------------------------------------------
        # EDIT
        # ----------------------------------------------------

        # Save current state BEFORE edit
        self.undo_stack.append(
            copy.deepcopy(scene)
        )

        # A new edit invalidates redo history
        self.redo_stack.clear()

        # Apply abstract scene changes
        updated_scene = (
            self.executor.apply(
                scene,
                plan,
            )
        )

        # ----------------------------------------------------
        # Build action result
        # ----------------------------------------------------

        actions_output = []

        for action in plan.actions:

            actions_output.append({

                "action":
                    action.action,

                "target":
                    action.target,

                "target_role":
                    action.target_role,

                "id":
                    action.id,

                "properties":
                    action.properties,

                "reason":
                    action.reason,
            })

        # ----------------------------------------------------
        # Store AI response
        # ----------------------------------------------------

        self.conversation_history.append({

            "role":
                "assistant",

            "content":
                plan.intent_summary,
        })

        return {

            "response_type":
                "edit",

            "intent_summary":
                plan.intent_summary,

            "actions":
                actions_output,

            "assumptions":
                plan.assumptions,

            "scene":
                updated_scene.to_dict(),
        }

    # ========================================================
    # UNDO
    # ========================================================

    def undo(
        self,
        current_scene: SceneGraph,
    ):

        if not self.undo_stack:

            return (
                current_scene,
                False,
                "Nothing to undo."
            )

        previous_scene = (
            self.undo_stack.pop()
        )

        self.redo_stack.append(
            copy.deepcopy(
                current_scene
            )
        )

        return (
            previous_scene,
            True,
            "Undid the last edit."
        )

    # ========================================================
    # REDO
    # ========================================================

    def redo(
        self,
        current_scene: SceneGraph,
    ):

        if not self.redo_stack:

            return (
                current_scene,
                False,
                "Nothing to redo."
            )

        next_scene = (
            self.redo_stack.pop()
        )

        self.undo_stack.append(
            copy.deepcopy(
                current_scene
            )
        )

        return (
            next_scene,
            True,
            "Redid the edit."
        )

    # ========================================================
    # RESET
    # ========================================================

    def reset_history(self):

        self.conversation_history.clear()

        self.undo_stack.clear()

        self.redo_stack.clear()


# ============================================================
# 12. TEST SCENE
# ============================================================

def make_test_scene():

    return SceneGraph(

        video=VideoInfo(

            width=1920,

            height=1080,

            duration=12.4,

            filename="test.mp4",
        ),

        canvas={

            "aspect_ratio":
                "16:9",

            "background":
                "#000000",
        },

        elements=[

            SceneElement(

                id="text_1",

                type="text",

                role="top_caption",

                properties={

                    "content":
                        "BRO REALLY THOUGHT HE WAS HIM",

                    "font":
                        "Impact",

                    "font_size":
                        64,

                    "font_weight":
                        "bold",

                    "text_color":
                        "#FFFFFF",

                    "position":
                        "top center",
                },

                z_index=2,
            )
        ],

        references={

            "last_created":
                "text_1",

            "last_referenced":
                "text_1",
        },
    )


# ============================================================
# 13. DISPLAY SCENE
# ============================================================

def print_scene(
    scene: SceneGraph,
):

    print()

    print("=" * 80)

    print("CURRENT SCENE")

    print("=" * 80)

    print()

    print(
        f"Scene version: {scene.version}"
    )

    print(
        f"Video: "
        f"{scene.video.width}x"
        f"{scene.video.height}"
    )

    print(
        f"Duration: "
        f"{scene.video.duration}s"
    )

    print(
        f"Aspect ratio: "
        f"{scene.canvas.get('aspect_ratio')}"
    )

    print()

    if not scene.elements:

        print(
            "No elements currently exist."
        )

    else:

        print(
            f"Elements ({len(scene.elements)}):"
        )

        print()

        for index, element in enumerate(
            scene.elements,
            start=1,
        ):

            print(
                f"{index}. "
                f"{element.id}"
            )

            print(
                f"   type: "
                f"{element.type}"
            )

            if element.role:

                print(
                    f"   role: "
                    f"{element.role}"
                )

            print(
                f"   z-index: "
                f"{element.z_index}"
            )

            print(
                "   properties:"
            )

            print(
                json.dumps(
                    element.properties,
                    indent=6,
                    ensure_ascii=False,
                )
            )

            print()

    print(
        "References:"
    )

    print(
        json.dumps(
            scene.references,
            indent=2,
        )
    )


# ============================================================
# 14. DISPLAY RESULT
# ============================================================

def print_result(
    result: Dict[str, Any],
):

    response_type = (
        result.get(
            "response_type"
        )
    )

    # ========================================================
    # CONVERSATION / CLARIFICATION
    # ========================================================

    if response_type in {
        "clarification",
        "conversation",
    }:

        print()

        print(
            "AI:"
        )

        print(
            result.get(
                "message",
                "No response.",
            )
        )

        print()

        return

    # ========================================================
    # EDIT
    # ========================================================

    print()

    print("=" * 80)

    print("AI INTENT")

    print("=" * 80)

    print(
        result.get(
            "intent_summary",
            "",
        )
    )

    # ========================================================
    # ACTIONS
    # ========================================================

    print()

    print("=" * 80)

    print("EDIT ACTIONS")

    print("=" * 80)

    actions = result.get(
        "actions",
        [],
    )

    for index, action in enumerate(
        actions,
        start=1,
    ):

        print()

        print(
            f"ACTION {index}"
        )

        print(
            "-" * 50
        )

        print(
            "Action:",
            action.get(
                "action"
            ),
        )

        if action.get("id"):

            print(
                "ID:",
                action.get(
                    "id"
                ),
            )

        if action.get("target"):

            print(
                "Target:",
                action.get(
                    "target"
                ),
            )

        if action.get(
            "target_role"
        ):

            print(
                "Target role:",
                action.get(
                    "target_role"
                ),
            )

        print()

        print(
            "Properties:"
        )

        print(
            json.dumps(
                action.get(
                    "properties",
                    {},
                ),
                indent=2,
                ensure_ascii=False,
            )
        )

        if action.get("reason"):

            print()

            print(
                "Reason:",
                action.get(
                    "reason"
                ),
            )

    # ========================================================
    # ASSUMPTIONS
    # ========================================================

    assumptions = result.get(
        "assumptions",
        [],
    )

    if assumptions:

        print()

        print("=" * 80)

        print("ASSUMPTIONS")

        print("=" * 80)

        for assumption in assumptions:

            print(
                "-",
                assumption,
            )

    # ========================================================
    # UPDATED SCENE
    # ========================================================

    print()

    print("=" * 80)

    print("SCENE UPDATED")

    print("=" * 80)

    scene_data = result.get(
        "scene"
    )

    if scene_data:

        print(
            f"Version: "
            f"{scene_data.get('version')}"
        )

        elements = scene_data.get(
            "elements",
            [],
        )

        print(
            f"Elements: "
            f"{len(elements)}"
        )

        for element in elements:

            print(
                f"  - "
                f"{element.get('id')} "
                f"({element.get('type')})"
            )

    print()


# ============================================================
# 15. TERMINAL HELP
# ============================================================

def print_help():

    print()

    print("=" * 80)

    print("AUTOQUENCE COMMANDS")

    print("=" * 80)

    print()

    print(
        "Type any natural-language editing request."
    )

    print()

    print(
        "Commands:"
    )

    print(
        "  undo"
    )

    print(
        "  redo"
    )

    print(
        "  show scene"
    )

    print(
        "  show history"
    )

    print(
        "  reset"
    )

    print(
        "  help"
    )

    print(
        "  exit"
    )

    print()


# ============================================================
# 16. MAIN INTERACTIVE TESTER
# ============================================================

def main():

    print()

    print("=" * 80)

    print("AUTOQUENCE AI V3")

    print("CONVERSATIONAL EDITOR TEST")

    print("=" * 80)

    print()

    print(
        "No actual video will be edited."
    )

    print(
        "You are testing the AI's understanding + scene context."
    )

    print()

    print(
        "Type 'help' for commands."
    )

    print()

    # --------------------------------------------------------
    # Initialize
    # --------------------------------------------------------

    ai = AutoquenceAI()

    scene = make_test_scene()

    # --------------------------------------------------------
    # Assets
    # --------------------------------------------------------

    available_assets = [

        {
            "id":
                "logo_1",

            "name":
                "my_logo.png",

            "type":
                "logo",
        }
    ]

    # --------------------------------------------------------
    # Fonts
    # --------------------------------------------------------

    available_fonts = [

        "Impact",

        "Arial",

        "Montserrat",

        "Inter",

        "Roboto",
    ]

    # ========================================================
    # LOOP
    # ========================================================

    while True:

        try:

            prompt = input(
                "Autoquence > "
            ).strip()

        except (
            KeyboardInterrupt,
            EOFError,
        ):

            print()

            print(
                "Exiting..."
            )

            break

        # ----------------------------------------------------
        # Empty
        # ----------------------------------------------------

        if not prompt:

            continue

        command = (
            prompt
            .lower()
            .strip()
        )

        # ====================================================
        # EXIT
        # ====================================================

        if command in {
            "exit",
            "quit",
            "q",
        }:

            print()

            print(
                "Goodbye."
            )

            break

        # ====================================================
        # HELP
        # ====================================================

        if command == "help":

            print_help()

            continue

        # ====================================================
        # SHOW SCENE
        # ====================================================

        if command in {
            "show scene",
            "scene",
            "state",
        }:

            print_scene(
                scene
            )

            continue

        # ====================================================
        # SHOW HISTORY
        # ====================================================

        if command in {
            "show history",
            "history",
        }:

            print()

            print(
                "=" * 80
            )

            print(
                "CONVERSATION HISTORY"
            )

            print(
                "=" * 80
            )

            if not ai.conversation_history:

                print(
                    "No conversation yet."
                )

            else:

                for item in (
                    ai.conversation_history
                ):

                    print()

                    print(
                        item["role"]
                        .upper()
                        + ":"
                    )

                    print(
                        item["content"]
                    )

            print()

            continue

        # ====================================================
        # RESET
        # ====================================================

        if command == "reset":

            scene = make_test_scene()

            ai.reset_history()

            print()

            print(
                "Scene and conversation reset."
            )

            continue

        # ====================================================
        # UNDO
        # ====================================================

        if command == "undo":

            (
                scene,
                success,
                message,
            ) = ai.undo(
                scene
            )

            print()

            print(
                message
            )

            if success:

                print_scene(
                    scene
                )

            continue

        # ====================================================
        # REDO
        # ====================================================

        if command == "redo":

            (
                scene,
                success,
                message,
            ) = ai.redo(
                scene
            )

            print()

            print(
                message
            )

            if success:

                print_scene(
                    scene
                )

            continue

        # ====================================================
        # NORMAL AI REQUEST
        # ====================================================

        print()

        print(
            "Thinking..."
        )

        try:

            result = ai.process(

                user_prompt=prompt,

                scene=scene,

                available_assets=
                    available_assets,

                available_fonts=
                    available_fonts,
            )

            # ------------------------------------------------
            # If edit, update actual tester scene
            # ------------------------------------------------

            if (
                result.get(
                    "response_type"
                )
                == "edit"
            ):

                scene = SceneGraph(
                    video=VideoInfo(
                        **result[
                            "scene"
                        ]["video"]
                    ),

                    elements=[
                        SceneElement(
                            **element
                        )

                        for element
                        in result[
                            "scene"
                        ]["elements"]
                    ],

                    canvas=result[
                        "scene"
                    ]["canvas"],

                    references=result[
                        "scene"
                    ]["references"],

                    version=result[
                        "scene"
                    ]["version"],
                )

            # ------------------------------------------------
            # Print result
            # ------------------------------------------------

            print_result(
                result
            )

        except Exception as error:

            print()

            print(
                "=" * 80
            )

            print(
                "ERROR"
            )

            print(
                "=" * 80
            )

            print(
                type(error).__name__
            )

            print(
                str(error)
            )

            print()


# ============================================================
# 17. START
# ============================================================

if __name__ == "__main__":

    main()