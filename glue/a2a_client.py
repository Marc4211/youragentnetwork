"""A2A client for the glue.

Lets an external, A2A-compliant agent participate in our platform. When such an
agent is @mentioned in a channel, the webhook handler calls ``ask_a2a_agent``
and posts the returned text back as the agent's Rocket Chat bot persona.

This is the thin "speak A2A" layer; it knows nothing about Rocket Chat. It
discovers the agent by its Agent Card, sends one message, and returns the reply
text. Multi-turn memory rides on ``context_id`` (the A2A field that the remote
agent maps to its own conversation thread), so we pass a stable id per
conversation and store no transcript ourselves.

See docs/a2a-integration-plan.md.
"""
from __future__ import annotations

import logging

import httpx

from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types.a2a_pb2 import Role, SendMessageRequest

log = logging.getLogger("glue")

# Resolved Agent Cards are stable; cache them so we don't refetch on every
# message. Keyed by the card's base URL.
_card_cache: dict[str, object] = {}

# A2A agents can be slow (they may call tools / LLMs). Bound the wait so a hung
# remote agent doesn't wedge a webhook; the caller posts a friendly timeout.
DEFAULT_TIMEOUT = 90.0


def _extract_text(chunk) -> str:
    """Pull the human-readable reply text out of one A2A response chunk.

    A completed task carries the agent's answer in its ``artifacts``; while
    streaming, earlier chunks carry interim ``status`` messages. We prefer
    artifact text and fall back to the status message, so this works for both
    streaming and non-streaming agents.
    """
    out: list[str] = []
    task = getattr(chunk, "task", None)
    if task is not None:
        for art in getattr(task, "artifacts", None) or []:
            for p in getattr(art, "parts", None) or []:
                t = getattr(p, "text", "")
                if t:
                    out.append(t)
        if not out:
            status = getattr(task, "status", None)
            msg = getattr(status, "message", None) if status else None
            for p in (getattr(msg, "parts", None) or []) if msg else []:
                t = getattr(p, "text", "")
                if t:
                    out.append(t)
    else:
        # The chunk may itself be a Message (some agents reply with a bare
        # message rather than a task).
        for p in getattr(chunk, "parts", None) or []:
            t = getattr(p, "text", "")
            if t:
                out.append(t)
    return "\n".join(out).strip()


async def ask_a2a_agent(
    card_url: str,
    bearer_token: str | None,
    message: str,
    context_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Send one message to an external A2A agent and return its reply text.

    card_url     base URL where the agent's card is served (e.g. https://host/).
    bearer_token optional bearer for the agent's auth (sent on every request).
    message      the user's text.
    context_id   stable per-conversation id for multi-turn memory; None starts
                 a fresh thread.
    Returns the reply text, or "" if the agent produced no text.
    Raises on transport/protocol failure (caller decides how to surface it).
    """
    headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as httpx_client:
        card = _card_cache.get(card_url)
        if card is None:
            resolver = A2ACardResolver(httpx_client=httpx_client, base_url=card_url)
            card = await resolver.get_agent_card()
            _card_cache[card_url] = card
            log.info("a2a: resolved card for %s (%s)", card_url, getattr(card, "name", "?"))

        client = await create_client(
            agent=card, client_config=ClientConfig(streaming=False)
        )
        try:
            msg = new_text_message(message, role=Role.ROLE_USER)
            if context_id:
                # Continue the remote agent's conversation thread.
                msg.context_id = context_id
            request = SendMessageRequest(message=msg)

            reply = ""
            async for chunk in client.send_message(request):
                text = _extract_text(chunk)
                if text:
                    reply = text  # keep the latest (final) non-empty text
            return reply
        finally:
            await client.close()
