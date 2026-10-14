"""Mock LLM for smoke-testing without API keys.

Provides a simple heuristic classifier that matches the TS project's intent
classification, so the beat-based state machine can be tested offline.
"""
from __future__ import annotations

import json
import re

from .config import Config


class MockLLM:
    def __init__(self, config: Config):
        self.config = config

    def chat(self, messages, temperature=0.7, max_tokens=1500) -> str:
        system = ""
        user_parts = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            elif m["role"] == "user":
                user_parts.append(m["content"])

        combined = "\n".join(user_parts)
        last_user = user_parts[-1] if user_parts else ""

        if "classify" in system.lower() or "intent classifier" in system.lower():
            return self._classify(combined)
        return self._respond(last_user)

    def chat_json(self, messages, temperature=0.3) -> dict:
        raw = self.chat(messages, temperature=temperature, max_tokens=500)
        try:
            return json.loads(raw)
        except Exception:
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except Exception:
                    pass
        return {"intent": "UNCLEAR", "confidence": 0.5}

    def models_available(self) -> bool:
        return False

    def _classify(self, text: str) -> str:
        low = text.lower()
        result = {
            "intent": "UNCLEAR",
            "decision_maker": False,
            "confidence": 0.5,
            "dnc": False,
            "busy": False,
            "transfer": False,
            "reason": "mock classifier",
        }

        if any(w in low for w in ["take me off", "stop calling", "don't call", "remove"]):
            result["intent"] = "DNC"
            result["dnc"] = True
            result["confidence"] = 0.95
            return json.dumps(result)

        if any(w in low for w in ["busy", "can't talk", "in a meeting", "call back"]):
            result["intent"] = "BUSY"
            result["busy"] = True
            result["confidence"] = 0.9
            return json.dumps(result)

        if any(w in low for w in ["who is", "who's", "what is this", "what's this about"]):
            result["intent"] = "QUESTION"
            result["confidence"] = 0.9
            return json.dumps(result)

        if any(w in low for w in [
            "yes", "yeah", "yep", "sure", "i do", "that's me", "i handle",
            "i make", "correct", "right", "absolutely", "definitely",
        ]):
            result["intent"] = "POSITIVE"
            result["decision_maker"] = True
            result["transfer"] = True
            result["confidence"] = 0.9
            result["reason"] = "caller confirmed decision-maker status"
            return json.dumps(result)

        if any(w in low for w in [
            "my wife", "my husband", "my son", "my daughter",
            "someone else", "not interested", "no thanks",
        ]):
            result["intent"] = "NEGATIVE"
            result["decision_maker"] = False
            result["confidence"] = 0.85
            result["reason"] = "caller indicated not decision-maker"
            return json.dumps(result)

        return json.dumps(result)

    def _respond(self, text: str) -> str:
        # Only consider the instruction portion of the prompt ("Your task for this line: ..."),
        # so conversation history doesn't confuse keyword matching.
        task = text
        idx = text.lower().find("your task for this line")
        if idx != -1:
            task = text[idx + len("your task for this line"):]
        low = task.lower()
        
        # Medicare-related beats
        if "ask the medicare" in low or "do you currently have medicare" in low:
            return "Do you currently have Medicare?"
        if "customer does not have medicare" in low or "politely thank them" in low and "medicare" in low:
            return "I understand. Thank you for your time. Have a great day."
        if "unclear answer about medicare" in low or "just to confirm" in low:
            return "Just to confirm, do you currently have Medicare — yes or no?"
        if "asked what medicare means" in low or "confirming whether you currently have medicare" in low:
            return "I'm just confirming whether you currently have Medicare coverage. Do you have it — yes or no?"
        if "mentioned private insurance only" in low or "do you also currently have medicare" in low:
            return "Do you also currently have Medicare?"
        if "repeat the medicare question" in low or "just need to confirm" in low:
            return "I'm sorry, I just need to confirm — do you currently have Medicare, yes or no?"
        if "refuses to answer" in low and "medicare" in low:
            return "I understand. Thank you for your time. Have a great day."
        
        # Age-related beats
        if "ask their age" in low or "how old are you" in low:
            return "How old are you, if you don't mind me asking?"
        if "eligible" in low and "transfer" in low:
            return "Thank you. One moment, let me get you connected with our licensed agent."
        if "not eligible" in low or "45 to 90" in low or "outside" in low:
            return "I appreciate your time, but this option is for ages 45 to 90. Thank you. Have a great day."
        if "didn't catch" in low:
            return "I'm sorry, I didn't catch that. How old are you?"
        
        # General beats
        if "greet them professionally" in low:
            return "Hi, this is Lina from Senior Resource Center. How are you today?"
        if "licensed agent" in low or "one moment" in low:
            return "Perfect, one moment."
        if "offer to follow up later" in low or "acknowledge they're busy" in low:
            return "I understand. I'll let you go for now. Thank you for your time."
        if "acknowledge their decision" in low or "thank them for their time" in low:
            return "I understand completely. Thank you for your time today."
        if "confirm they're still on the line" in low:
            return "Hello? Are you still there?"
        return "I understand. Let me see how I can help."
