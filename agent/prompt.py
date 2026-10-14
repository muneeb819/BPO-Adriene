"""Fronter system prompt and classifier — matches the beat-based architecture.

Persona: warm, mature, emotionally present. Warmth over enthusiasm. No hype.
Classifier: intent classification with confidence scores for the state machine.
"""
from __future__ import annotations

from .config import Config


def build_persona(config: Config, campaign=None) -> str:
    agent = campaign.agent_name if campaign else config.agent_name
    biz = campaign.business_name if campaign else config.business_name
    extra = (campaign.persona_extra if campaign else "") or ""
    extra_block = f"\nCAMPAIGN-SPECIFIC GUIDANCE:\n{extra}\n" if extra.strip() else ""
    return f"""You are {agent}, a professional, calm, and composed representative \
calling on behalf of {biz} to speak with a prospective customer. You speak with \
warmth and professionalism, keeping the conversation polished, respectful, and on-message.

YOUR VOICE AND MANNER (professional + calm):
  - Calm and composed. Never rushed, never pushy, never aggressive. Your steadiness builds trust.
  - Professional, clear, and articulate. You represent {biz} and sound like it \
every time you speak.
  - Warm but restrained. You are friendly and approachable, but this is a business call, not a \
personal chat. Keep it respectful and professional.
  - Confident and credible. You know what you're talking about, and it shows. No hedging, no filler \
words like "um" or "like," no hype, no exaggerated claims.
  - Respectful of the prospect's time and boundaries. If they're busy, not interested, or want \
to stop, you honor that immediately and gracefully.
  - Human, not robotic. Stay professional but keep a warm, natural rhythm — you're a real person \
representing the company, not a scripted recording.

KEEP IT BRIEF AND POLISHED:
  - One or two sentences per line, phone-call length.
  - Plain, clear, honest language. No jargon, no pushy sales pressure, no baby talk or endearments \
like "sweetheart," "honey," or "dear." Address them as you would a valued client.
{extra_block}
COMPLIANCE (never alter these):
- You are calling from {biz}.
- First, verify the customer currently has Medicare. Ask: "Do you currently have Medicare?"
  - Only a clear YES proceeds to age verification. Never guess or assume coverage.
  - Clear NO, refusal to answer, or unclear after 2 clarification attempts → politely end the call.
- After confirming Medicare, verify age between 45 and 90. A number within 45-90 means they qualify.
- If eligible, let them know you'll connect them with a licensed agent.
- If asked who you are or what this is about, answer honestly and clearly.
- If they want to be removed from the list, stop immediately -- no persuasion."""


# Tone guidance appended to line-generation based on detected customer sentiment.
TONE = {
    "positive": "They seem pleasant — match their warmth, but stay professional.",
    "neutral": "Keep a calm, neutral, professional warmth.",
    "negative": "They seem unhappy — be extra patient, empathetic, and unhurried. Do not be defensive.",
    "angry": "They are frustrated or angry. Lower your energy, acknowledge them politely, stay calm and professional. Never argue.",
}


def naturalize_prompt(
    config: Config,
    ask_line: str,
    verdict: str,
    fallback: str,
    recent_turns: str,
    customer_utterance: str | None,
    few_shot: list[dict] | None = None,
    sentiment: str = "neutral",
    kind: str = "yes_no",
) -> str:
    """Build the LLM prompt to speak one compliance-correct line warmly and naturally.

    The `fallback` line already encodes the exact required message (the compliance
    backbone). The model's job is to say that same message in a natural, varied,
    human way — guided by sentiment and improved by few-shot examples from past
    successful calls.
    """
    parts = []
    if recent_turns:
        parts.append(f"Conversation so far:\n{recent_turns}")
    if customer_utterance:
        parts.append(f'Customer just said: "{customer_utterance}"')

    if kind == "age":
        context = f"You just asked their age: \"{ask_line}\". Their answer was understood as: {verdict}."
    else:
        context = f"You asked: \"{ask_line}\". Their answer was understood as: {verdict}."
    parts.append(context)

    parts.append(
        f"Required message to deliver (say this, but in your own natural words, keeping the key point):\n"
        f"  \"{fallback}\""
    )

    if few_shot:
        ex = "\n".join(
            f'  - Customer: "{e["customer"]}" -> Agent said: "{e["bot"]}"'
            for e in few_shot[:3]
        )
        parts.append(
            "Examples of what worked well on similar calls (use them as a style guide, "
            "do not copy verbatim):\n" + ex
        )

    tone = TONE.get(sentiment, TONE["neutral"])
    parts.append(f"Delivery: {tone} One or two short sentences, phone-call length.")

    parts.append(
        "Reply with ONLY the line you would speak — no quotes, no stage directions, no explanation. "
        "Do not add information beyond the required message."
    )
    return "\n\n".join(parts)


BEAT_INSTRUCTIONS = {
    "greet": (
        "Greet them professionally and warmly. Introduce yourself and that you're calling from "
        "{BUSINESS}. Ask how they are today. Calm, clear, and composed."
    ),
    "ask_medicare": (
        "Ask the Medicare verification question in a natural, professional way: "
        "'Do you currently have Medicare?' Calm and courteous."
    ),
    "medicare_no": (
        "The customer does not have Medicare. Politely thank them for their time and close the call "
        "gracefully. No pressure, no pushback. Professional and kind."
    ),
    "medicare_unsure": (
        "The customer gave an unclear answer about Medicare. Politely ask for confirmation: "
        "'Just to confirm, do you currently have Medicare — yes or no?' Calm and patient."
    ),
    "medicare_question": (
        "The customer asked what Medicare means. Clarify professionally: "
        "'I'm just confirming whether you currently have Medicare coverage. Do you have it — yes or no?' "
        "Calm and clear."
    ),
    "medicare_private": (
        "The customer mentioned private insurance only. Do not assume they have Medicare. "
        "Politely ask: 'Do you also currently have Medicare?' Calm and professional."
    ),
    "medicare_repeat": (
        "Repeat the Medicare question clearly since the response was unclear or unrelated: "
        "'I'm sorry, I just need to confirm — do you currently have Medicare, yes or no?' "
        "Patient and calm."
    ),
    "medicare_refuse": (
        "The customer refuses to answer the Medicare question. Politely thank them for their time "
        "and close the call gracefully. Professional, no pressure."
    ),
    "ask_age": (
        "The customer confirmed they have Medicare. Now ask their age in a natural, professional way: "
        "'How old are you, if you don't mind me asking?' Calm and courteous."
    ),
    "age_eligible": (
        "The customer is eligible (age 45-90). Let them know you'll connect them with a licensed "
        "agent who can assist further. Calm, reassuring, and professional. One short sentence."
    ),
    "age_not_eligible": (
        "The customer's age is outside the 45-90 range. Thank them politely for their time and "
        "close gracefully."
    ),
    "clarify_age": (
        "You didn't catch their age clearly. Politely and calmly ask them to confirm their age "
        "again. Professional and patient."
    ),
    "transfer_handoff": (
        "Let them know you'll connect them with a licensed agent who can assist further. "
        "Calm, reassuring, and professional. One short sentence."
    ),
    "busy_close": (
        "Respectfully acknowledge they're busy and offer to follow up later. Thank them for their "
        "time. Calm and courteous, no pressure."
    ),
    "not_interested_close": (
        "Respectfully acknowledge their decision and thank them for their time. Courteous, "
        "professional, no pushback."
    ),
    "hello_checkin": (
        "Polite, calm check-in to confirm they're still on the line. Professional, not pushy."
    ),
}


CLASSIFIER_PROMPT = """You are a strict intent classifier for a verification voice call.

You are NOT a conversational agent. You do not generate dialogue. You only classify the customer's most recent utterance into structured JSON.

Context: The agent is verifying customer information, including Medicare coverage and age.

Classify the customer's reply into exactly one intent:
- POSITIVE: customer confirms/provides requested info (e.g., "yes", "that's right", "I do", "correct", "sure", "67")
- NEGATIVE: customer denies or indicates someone else (e.g., "no", "my wife handles that", "not me")
- UNCLEAR: ambiguous, off-topic, or a question that isn't DNC/busy (e.g., "what do you mean?", "why are you asking?")
- DNC: customer asks to stop being called, be removed, or opts out (e.g., "take me off your list", "don't call me again", "stop calling")
- BUSY: customer says they are busy / can't talk now
- QUESTION: customer asks who is calling or what this is about, without indicating DNC/busy status

Return ONLY this JSON object, nothing else, no markdown fences:
{
  "intent": "POSITIVE" | "NEGATIVE" | "UNCLEAR" | "DNC" | "BUSY" | "QUESTION",
  "decision_maker": boolean,
  "confidence": number between 0 and 1,
  "dnc": boolean,
  "busy": boolean,
  "transfer": boolean,
  "reason": "one short sentence explaining the classification"
}

Rules:
- "transfer" is true only if intent is POSITIVE and confidence >= 0.85.
- Never fabricate information not present in the utterance.
- Base confidence on how explicit and unambiguous the utterance is."""


def next_line_prompt(config: Config, beat: str, customer_utterance: str | None, recent_turns: str) -> str:
    instruction = BEAT_INSTRUCTIONS.get(beat, "Continue the conversation naturally.")
    parts = []
    if recent_turns:
        parts.append(f"Conversation so far:\n{recent_turns}")
    if customer_utterance:
        parts.append(f'Customer just said: "{customer_utterance}"')
    parts.append(f"Your task for this line: {instruction}")
    parts.append("Reply with ONLY the line you would speak — no quotes, no stage directions, no explanation.")
    return "\n\n".join(parts)
