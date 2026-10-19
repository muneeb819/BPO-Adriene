"""Adriene's dialogue brain as a LiveKit ``llm.LLM``.

Why a custom LLM instead of a general model?

The Medicare script is *policy*: the greeting, the two qualifying questions, the
compliance-owned do-not-call wording and the warm transfer. Adriene's brain
(``backend/app/core/state_machine.py`` + ``brain/``) already implements that as a
deterministic state machine with NLU, a knowledge base, objection handling, a
repair ladder and a self-learning memory. Wrapping it as an ``LLM`` means the
LiveKit pipeline keeps its normal shape (STT -> LLM -> TTS, interruptions,
transcripts, metrics) while every word still comes from the policy layer.

The stream emits the full reply as one chunk: replies are one or two sentences
by design, so the extra latency of a "streaming" implementation would be noise,
and TTS gets a clean sentence to speak.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from livekit.agents import llm, utils
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

from ...state_machine import CallSession

logger = logging.getLogger("adriene.livekit.llm")


class BrainLLM(llm.LLM):
    """Drive one ``CallSession`` per LiveKit job.

    Args:
        session_id: stable id for persona seeding / memory attribution.
        on_turn: optional ``(role, text)`` callback fired for every turn, used
            by the worker for transcript logging and the debug console.
    """

    def __init__(
        self,
        *,
        session_id: Optional[str] = None,
        on_turn: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        super().__init__()
        self.call = CallSession(session_id or utils.shortuuid("lk-call-"))
        self.session_id = self.call.session_id
        self._on_turn = on_turn
        self._greeted = False
        self._closed = False

        #: set by the policy layer when the call has to do something after the
        #: current sentence finishes playing ("transfer" | "end" | None)
        self.pending_action: Optional[str] = None
        self.disposition: str = ""
        self.turns: list[dict] = []

    # ------------------------------------------------------------------ meta
    @property
    def model(self) -> str:
        return "adriene-brain-v1"

    @property
    def provider(self) -> str:
        return "bpo-adriene"

    # ------------------------------------------------------------------- api
    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: Optional[list[llm.Tool]] = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        **kwargs,
    ) -> "BrainLLMStream":
        return BrainLLMStream(
            self, chat_ctx=chat_ctx, tools=list(tools or []), conn_options=conn_options
        )

    # ------------------------------------------------------------- behaviour
    def opening_line(self) -> str:
        """The first thing the caller hears (greeting + first question)."""
        self._greeted = True
        return self.call.get_next_bot_prompt()

    def respond(self, text: str) -> str:
        """Run one caller turn through the brain and return Adriene's reply."""
        text = (text or "").strip()
        if not text:
            return "I'm here — take your time."

        if self._closed:
            return self.call.persona.farewell()

        reply, transfer = self.call.process_user_utterance(text)

        if transfer:
            self.pending_action = "transfer"
            self.disposition = "AI-TRANSFER"
        elif self.call.state == "ended":
            self.pending_action = "end"
            self.disposition = (
                "DNC" if self.call.data.get("dnc") else self._ended_disposition()
            )
        elif self.call.state == "not_eligible":
            self.pending_action = "end"
            self.disposition = "NOT-ELIGIBLE"

        logger.debug(
            "brain turn | state=%s pending=%s intent_reply=%r",
            self.call.state,
            self.call.pending,
            reply[:120],
        )
        return reply

    def _ended_disposition(self) -> str:
        if self.call.data.get("has_medicare") is True:
            return "QUALIFIED"
        if self.call.data.get("has_medicare") is False:
            return "NO-MEDICARE"
        return "COMPLETED"

    # ------------------------------------------------------------- recording
    def record(self, role: str, text: str) -> None:
        """Append a turn to the local transcript and notify the worker."""
        self.turns.append({"role": role, "text": text})
        if self._on_turn is not None:
            try:
                self._on_turn(role, text)
            except Exception:  # a logging callback must never kill a call
                logger.exception("on_turn callback failed")

    def result(self) -> dict:
        return {
            "session_id": self.session_id,
            "disposition": self.disposition or "INCOMPLETE",
            "state": self.call.state,
            "data": dict(self.call.data),
            "transcript": list(self.turns),
        }


class BrainLLMStream(llm.LLMStream):
    """One-shot stream: policy decision in, one ``ChatChunk`` out."""

    def __init__(
        self,
        llm_impl: BrainLLM,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(
            llm_impl, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options
        )
        self._brain = llm_impl

    async def _run(self) -> None:
        user_text = self._last_user_text()
        if user_text is None:
            reply = self._brain.opening_line()
            self._brain.record("assistant", reply)
        else:
            reply = self._brain.respond(user_text)
            self._brain.record("user", user_text)
            self._brain.record("assistant", reply)

        self._event_ch.send_nowait(
            llm.ChatChunk(
                id=utils.shortuuid("adriene-"),
                delta=llm.ChoiceDelta(role="assistant", content=reply),
            )
        )

    def _last_user_text(self) -> Optional[str]:
        """The newest caller utterance in the chat context, if any."""
        for message in reversed(self._chat_ctx.messages()):
            if message.role != "user":
                continue
            text = (message.text_content or "").strip()
            if text:
                return text
        return None
