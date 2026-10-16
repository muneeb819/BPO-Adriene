"""Adriene's brain: language understanding, Medicare knowledge, compliance,
persona, and self-learning memory.

Design goals (local-first, no API keys required):
  * understand ANY reply — paraphrases, typos, hedged/mixed answers
  * answer Medicare / policy / compliance questions from a grounded KB
  * sound warm, human and expressive (persona composes; the UI adds prosody)
  * learn: heuristic interpretations are stored, unknowns are queued, and
    feedback corrections are applied on later calls
  * upgrade path: ``get_brain()`` exposes ``llm_available`` — when an LLM key
    is added to the environment the same decisions can be re-worded by a real
    model (see ``brain.Brain.generate``)
"""
from .nlu import Utterance, analyze, normalize
from .knowledge import KB, KBEntry, retrieve, topics as kb_topics
from . import compliance
from .persona import Persona, PROFILE
from .memory import Memory, get_memory
from .brain import Brain, get_brain

__all__ = [
    "Utterance", "analyze", "normalize",
    "KB", "KBEntry", "retrieve", "kb_topics", "compliance",
    "Persona", "PROFILE", "Memory", "get_memory",
    "Brain", "get_brain",
]
