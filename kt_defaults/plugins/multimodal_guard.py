"""Multimodal guard plugin.

When enabled, this plugin rewrites any multimodal outgoing LLM message into
text-only content at the shared ``pre_llm_call`` hook. It does not try to
infer model capabilities; it is an explicit user-controlled policy toggle.
"""

from __future__ import annotations

from typing import Any

from kohakuterrarium.modules.plugin.base import BasePlugin

_PLACEHOLDER = (
    "[Image omitted by multimodal guard plugin. The current request is being "
    "forced to text-only. If this image matters, tell the user you could not "
    "inspect it and ask for a text description of the relevant visual details.]"
)


class MultimodalGuardPlugin(BasePlugin):
    name = "multimodal_guard"
    priority = 5
    description = "Rewrite multimodal LLM input into text-only placeholders"

    def __init__(self, options: dict[str, Any] | None = None):
        opts = options or {}
        self._placeholder = (
            str(opts.get("placeholder", _PLACEHOLDER)).strip() or _PLACEHOLDER
        )

    async def pre_llm_call(self, messages: list[dict], **kwargs) -> list[dict] | None:
        changed = False
        rewritten = []

        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                rewritten.append(message)
                continue

            text_parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    text_parts.append(str(part))
                    changed = True
                    continue

                part_type = part.get("type")
                if part_type == "text":
                    text = part.get("text", "")
                    if text:
                        text_parts.append(str(text))
                    continue

                if part_type == "image_url":
                    text_parts.append(self._describe_image_part(part))
                    changed = True
                    continue

                text_parts.append(str(part))
                changed = True

            flattened = "\n".join(chunk for chunk in text_parts if chunk)
            new_message = dict(message)
            new_message["content"] = flattened
            rewritten.append(new_message)

        return rewritten if changed else None

    def _describe_image_part(self, part: dict[str, Any]) -> str:
        source_name = None
        image_data = part.get("image_url")
        if isinstance(image_data, dict):
            source_name = image_data.get("source_name") or image_data.get("url")
        else:
            source_name = part.get("source_name") or part.get("url")

        if isinstance(source_name, str) and source_name.startswith("data:"):
            source_name = None

        if source_name:
            return f"{self._placeholder} Source: {source_name}"
        return self._placeholder
