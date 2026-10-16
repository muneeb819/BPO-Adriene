"""
Simple call state machine for the local prototype.
States:
- init -> greeting -> ask_medicare -> ask_age -> eligibility -> transferred/ended

This implements only the minimal logic for the two verification questions described.
"""
from typing import Optional

class CallSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.state = "greeting"
        self.data = {
            "has_medicare": None,
            "age_verified": None,
        }

    def get_next_bot_prompt(self) -> str:
        if self.state == "greeting":
            self.state = "ask_medicare"
            return (
                "Hello, my name is Adriene. I'm calling about Medicare benefits. "
                "May I ask: do you currently have Medicare? (please answer yes or no)"
            )
        if self.state == "ask_medicare":
            return "Do you currently have Medicare?"
        if self.state == "ask_age":
            return "Are you between 45 and 80 years old?"
        return "Thank you."

    def _normalize_yes_no(self, text: str) -> Optional[bool]:
        t = text.strip().lower()
        if t in ["yes", "y", "yeah", "yep", "sure", "i do", "i have medicare"]:
            return True
        if t in ["no", "n", "nope", "i don't", "i do not", "i do not have medicare"]:
            return False
        return None

    def process_user_utterance(self, text: str):
        # returns (bot_reply, should_transfer_flag)
        if self.state == "ask_medicare":
            yn = self._normalize_yes_no(text)
            if yn is None:
                return ("Sorry, I didn't catch that. Do you currently have Medicare? Please say yes or no.", False)
            self.data["has_medicare"] = yn
            self.state = "ask_age"
            return ("Thanks. Are you between 45 and 80 years old?", False)
        elif self.state == "ask_age":
            # Accept direct age or yes/no
            t = text.strip().lower()
            if t.isdigit():
                age = int(t)
                ok = 45 <= age <= 80
                self.data["age_verified"] = ok
            else:
                yn = self._normalize_yes_no(text)
                if yn is None:
                    return ("I didn't understand. Are you between 45 and 80 years old? Please say yes or no or state your age.", False)
                self.data["age_verified"] = yn
            # Decide eligibility
            eligible = bool(self.data.get("has_medicare") and self.data.get("age_verified"))
            if eligible:
                self.state = "eligible"
                bot = (
                    "That's wonderful, I have a Benefits Specialist who is waiting. "
                    "Let me transfer you without any delay — here we go."
                )
                return (bot, True)
            else:
                self.state = "not_eligible"
                bot = (
                    "Thank you for your time. Based on your answers you are not eligible for this offer. "
                    "If you'd like, I can pass your details to someone for further assistance."
                )
                return (bot, False)
        else:
            return ("Thank you. Goodbye.", False)
