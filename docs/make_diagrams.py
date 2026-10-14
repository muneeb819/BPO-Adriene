"""Generate scientific diagrams for the Jessica FE Fronter bot (white-paper style).

Outputs (300 DPI PNG):
  docs/architecture.png     - system architecture block diagram
  docs/call_flow.png        - beat-based state machine / call-flow diagram
  docs/cost_breakdown.png   - budget charts (monthly stacked bars + per-call pie)

Run:  .venv/bin/python docs/make_diagrams.py
"""
from __future__ import annotations

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Polygon

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)))

# ---- palette (print-friendly, colorblind-safe-ish) ----
INK = "#1f2937"
MUTED = "#6b7280"
BLUE = "#2563eb"
LIGHT_BLUE = "#dbeafe"
TEAL = "#0d9488"
LIGHT_TEAL = "#ccfbf1"
AMBER = "#d97706"
LIGHT_AMBER = "#fef3c7"
GREEN = "#15803d"
LIGHT_GREEN = "#dcfce7"
RED = "#dc2626"
LIGHT_RED = "#fee2e2"
PURPLE = "#7c3aed"
LIGHT_PURPLE = "#ede9fe"
GRAY_BG = "#f3f4f6"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "axes.unicode_minus": False,
})


def new_fig(w=16, h=10):
    fig, ax = plt.subplots(figsize=(w, h))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")
    return fig, ax


def box(ax, x, y, w, h, title, sub="", face=LIGHT_BLUE, edge=BLUE, tsize=11, ssize=9):
    p = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.6,rounding_size=1.5",
                       facecolor=face, edgecolor=edge, linewidth=2)
    ax.add_patch(p)
    cx = x + w / 2
    if sub:
        ax.text(cx, y + h / 2 + 2.2, title, ha="center", va="center", fontsize=tsize,
                fontweight="bold", color=INK)
        ax.text(cx, y + h / 2 - 3.2, sub, ha="center", va="center", fontsize=ssize, color=MUTED)
    else:
        ax.text(cx, y + h / 2, title, ha="center", va="center", fontsize=tsize,
                fontweight="bold", color=INK)
    return (x, y, w, h)


def arrow(ax, x1, y1, x2, y2, label="", lpos=0.5, color=INK, style="-", width=2.2, size=14):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=f"-|>,head_length={size/4},head_width={size/5}",
                                 color=color, linewidth=width, linestyle=style,
                                 connectionstyle="arc3,rad=0", shrinkA=1, shrinkB=3))
    if label:
        mx, my = x1 + (x2 - x1) * lpos, y1 + (y2 - y1) * lpos
        ax.text(mx, my + 1.6, label, ha="center", va="bottom", fontsize=8.5,
                color=color, style="italic",
                bbox=dict(facecolor="white", edgecolor="none", pad=1.2))


def title_block(ax, title, subtitle):
    ax.text(50, 96.5, title, ha="center", va="center", fontsize=17, fontweight="bold", color=INK)
    ax.text(50, 93.2, subtitle, ha="center", va="center", fontsize=10.5, color=MUTED)


# =====================================================================
# 1. SYSTEM ARCHITECTURE
# =====================================================================
def architecture():
    fig, ax = new_fig(16, 10)
    title_block(ax, "Jessica FE Fronter — System Architecture",
                "AI voice fronter for final-expense leads  •  caller → Asterisk/ARI → beat-based dialogue engine → closer handoff")

    # --- main left-to-right pipeline (y band 52..68) ---
    y, h = 54, 15
    b1 = box(ax, 1.5, y, 12, h, "Caller", "PSTN / mobile", LIGHT_GRAY := GRAY_BG, MUTED)
    b2 = box(ax, 15.5, y, 14, h, "SIP Trunk", "Telnyx / Twilio\nRTP audio", LIGHT_BLUE, BLUE)
    b3 = box(ax, 31.5, y, 17, h, "Asterisk + VICIdial", "Stasis(final-expense-fronter)\nDialplan → Stasis app", LIGHT_AMBER, AMBER)
    b4 = box(ax, 50.5, y, 15, h, "ARI Client", "agent/ari.py\nanswer • play • record\nredirect", LIGHT_TEAL, TEAL)
    b5 = box(ax, 67.5, y, 17, h, "Conversation Engine", "agent/conversation.py\nbeat state machine", LIGHT_PURPLE, PURPLE)

    ymid = y + h / 2
    arrow(ax, 13.5, ymid, 15.5, ymid, "call")
    arrow(ax, 29.5, ymid, 31.5, ymid, "SIP + RTP")
    arrow(ax, 48.5, ymid, 50.5, ymid, "ARI events")
    arrow(ax, 65.5, ymid, 67.5, ymid, "turns")

    # --- AI services above engine ---
    llm = box(ax, 52, 76, 16, 13, "LLM", "OpenAI / Anthropic / Ollama\nagent/llm.py • lines + intents", LIGHT_GREEN, GREEN, tsize=10.5)
    arrow(ax, 63, 76, 69, 69, "prompt → line / JSON", color=GREEN)
    arrow(ax, 71, 69, 65, 76, "", color=GREEN)

    stt = box(ax, 70, 76, 13, 13, "STT", "Deepgram nova-2\nagent/stt.py", LIGHT_BLUE, BLUE, tsize=10.5)
    arrow(ax, 76.5, 76, 74, 69, "transcript", color=BLUE)

    tts = box(ax, 85, 76, 13, 13, "TTS", "ElevenLabs v3 • Jessica\nagent/tts.py", LIGHT_AMBER, AMBER, tsize=10.5)
    arrow(ax, 88, 76, 82, 69, "WAV audio", color=AMBER)

    # --- outputs below engine ---
    o1 = box(ax, 52, 32, 16, 13, "Transfer → Closers", "Local/closers@from-internal\nqualified: age 45–80", LIGHT_GREEN, GREEN, tsize=10.5)
    arrow(ax, 70, 54, 62, 45, "eligible", color=GREEN)
    o2 = box(ax, 70, 32, 13, 13, "Hangup", "polite close +\ndisposition", LIGHT_RED, RED, tsize=10.5)
    arrow(ax, 74, 54, 74, 45, "not eligible", color=RED)

    # --- VICIdial logging + dashboard (bottom band) ---
    vici = box(ax, 31.5, 10, 25, 13, "VICIdial non-agent API", "agent/vicidial.py  •  update_lead: Q / NI / INC / QC", LIGHT_TEAL, TEAL, tsize=10.5)
    dash = box(ax, 58.5, 10, 26, 13, "FastAPI Dashboard :8000", "server/app.py  •  /health  /debug  /api/calls", GRAY_BG, MUTED, tsize=10.5)
    arrow(ax, 60, 32, 52, 23, "log", color=TEAL)
    arrow(ax, 70, 32, 70, 23, "record", color=MUTED, style="--")

    # note
    ax.text(50, 3.5, "Turn-based media path: bot speaks → records caller (DTMF/timeout) → transcribes → reasons → repeats.  •  Full-duplex option: agent/ari_streaming.py (externalMedia/RTP).",
            ha="center", va="center", fontsize=9, color=MUTED, style="italic")

    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "architecture.png"), dpi=200, bbox_inches="tight")
    plt.close(fig)
    print("wrote architecture.png")


# =====================================================================
# 2. CALL FLOW / BEAT STATE MACHINE
# =====================================================================
def diamond(ax, cx, cy, w, h, text, face=LIGHT_AMBER, edge=AMBER, tsize=9.5):
    pts = [(cx, cy + h / 2), (cx + w / 2, cy), (cx, cy - h / 2), (cx - w / 2, cy)]
    ax.add_patch(Polygon(pts, closed=True, facecolor=face, edgecolor=edge, linewidth=2))
    ax.text(cx, cy, text, ha="center", va="center", fontsize=tsize, fontweight="bold", color=INK)


def call_flow():
    fig, ax = new_fig(16, 12)
    title_block(ax, "Jessica FE Fronter — Call-Flow / Beat State Machine",
                "GREETING → HOOK → AGE_DETECTION → TRANSFER / CLOSE  •  end states show disposition codes")

    cx = 50
    # vertical spine of states
    box(ax, cx - 11, 80, 22, 9, "START: StasisStart", "answer channel", GRAY_BG, MUTED, tsize=10)
    arrow(ax, cx, 80, cx, 77.5)
    box(ax, cx - 11, 68, 22, 9, "GREETING", "“Hi, this is Lina … how are you?”", LIGHT_BLUE, BLUE, tsize=10)
    arrow(ax, cx, 68, cx, 65.5)
    box(ax, cx - 11, 56, 22, 9, "WAIT_FOR_RESPONSE → HOOK", "“Fixed-income coverage … How young are you?”", LIGHT_BLUE, BLUE, tsize=9.5)
    arrow(ax, cx, 56, cx, 53.5)
    box(ax, cx - 11, 44, 22, 9, "AGE_DETECTION", "extract_age() + classify_utterance()", LIGHT_PURPLE, PURPLE, tsize=10)

    # decision diamond row
    diamond(ax, cx, 35, 30, 12, "age 45–80\nor “yes”?")
    arrow(ax, cx, 44, cx, 41)

    # eligible → right branch
    box(ax, 68, 30, 26, 9, "TRANSFER to closers", "“Lower monthly premium … one moment”\nAI-TRANSFER", LIGHT_GREEN, GREEN, tsize=10)
    arrow(ax, 65, 35, 68, 34.5, "eligible", color=GREEN)

    # not eligible → left branch
    box(ax, 6, 30, 26, 9, "CLOSE: not eligible", "“Ages 45–80 … thank you”\nAI-NOT-ELIGIBLE", LIGHT_RED, RED, tsize=10)
    arrow(ax, 35, 35, 32, 34.5, "age <45 / >80", color=RED)

    # unclear → clarify loop (below diamond)
    box(ax, cx - 11, 17, 22, 9, "CLARIFY_AGE (×1)", "“Sorry, I didn’t catch that …”", LIGHT_AMBER, AMBER, tsize=9.5)
    arrow(ax, cx, 29, cx, 26, "no age", color=AMBER)
    # loop-back arrow: right-side elbow loop (avoids diamond + TRANSFER box)
    ax.plot([61, 97, 97, 61.5], [21.5, 21.5, 48.5, 48.5], color=AMBER, linewidth=2, linestyle="--")
    ax.add_patch(FancyArrowPatch((63.5, 48.5), (61, 48.5), arrowstyle="-|>,head_length=4,head_width=2.5",
                                 color=AMBER, linewidth=2, linestyle="--", shrinkB=0))
    ax.text(79, 19.2, "retry → AGE_DETECTION", ha="center", va="top", fontsize=8.5, color=AMBER, style="italic")
    box(ax, cx - 11, 5, 22, 9, "HANGUP", "still unclear\nAI-UNCLEAR", LIGHT_RED, RED, tsize=10)
    arrow(ax, cx, 17, cx, 14, "", color=RED)
    ax.text(cx + 3.5, 15.5, "2nd miss", ha="left", va="center", fontsize=8.5, color=RED, style="italic")

    # side guards (left column): DNC / BUSY / QUESTION
    ax.text(8, 80, "Guards (any turn):", ha="left", va="center", fontsize=10, fontweight="bold", color=INK)
    box(ax, 2, 69, 24, 8, "DNC ≥ 0.75 → HANGUP", "AI-DNC (stop at once)", LIGHT_RED, RED, tsize=9, ssize=8)
    box(ax, 2, 59, 24, 8, "BUSY → polite close", "AI-BUSY", LIGHT_AMBER, AMBER, tsize=9, ssize=8)
    box(ax, 2, 49, 24, 8, "QUESTION → answer + re-ask", "who_is_this / what_is_this_about", LIGHT_BLUE, BLUE, tsize=8.5, ssize=7.5)

    # side note: decision-maker path
    box(ax, 72, 62, 26, 15, "Alt path: DECISION_MAKER", "POSITIVE ≥ 0.85 → TRANSFER\nNEGATIVE ≥ 0.85 → AI-NOT-DECISION-MAKER\nUNCLEAR → clarify ×1 → AI-UNCLEAR", LIGHT_TEAL, TEAL, tsize=9, ssize=8)

    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "call_flow.png"), dpi=200, bbox_inches="tight")
    plt.close(fig)
    print("wrote call_flow.png")


# =====================================================================
# 3. COST BREAKDOWN CHARTS
# =====================================================================
def cost_breakdown():
    # unit economics (USD) — keep in sync with BUDGET_COST_STRUCTURE.md
    TTS = 0.0280    # 350 chars @ $0.08/1k (ElevenLabs v3)
    STT = 0.0022    # 0.5 min @ $0.0043/min (Deepgram nova-2)
    LLM_MINI = 0.0005   # ~2.5k in / 0.25k out @ $0.15/$0.60 per M
    LLM_CLAUDE = 0.0113 # same tokens @ $3/$15 per M
    TEL = 0.0053    # 1.5 min inbound @ ~$0.0035/min
    scenarios = ["Pilot\n1k calls/mo", "Growth\n10k calls/mo", "Scale\n50k calls/mo"]
    volumes = [1000, 10000, 50000]
    hosting = [40, 120, 400]
    dids = [2, 5, 15]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 7.5))
    fig.suptitle("Jessica FE Fronter — Budget Cost Structure (USD)", fontsize=16, fontweight="bold", color=INK)
    fig.text(0.5, 0.93, "Budget stack: GPT-4o-mini + Deepgram nova-2 + ElevenLabs v3 + SIP inbound  •  Oct 2026 list prices",
             ha="center", fontsize=10.5, color=MUTED)
    colors = {"TTS": "#7c3aed", "STT": "#2563eb", "LLM": "#0d9488", "Telephony": "#d97706",
              "Hosting": "#475569", "DIDs": "#94a3b8"}

    # ---- left: stacked monthly bars ----
    bottoms = [0, 0, 0]
    segs = [
        ("TTS", [TTS * v for v in volumes]),
        ("STT", [STT * v for v in volumes]),
        ("LLM", [LLM_MINI * v for v in volumes]),
        ("Telephony", [TEL * v for v in volumes]),
        ("Hosting", hosting),
        ("DIDs", dids),
    ]
    for name, vals in segs:
        ax1.bar(scenarios, vals, bottom=bottoms, label=name, color=colors[name], edgecolor="white")
        bottoms = [b + v for b, v in zip(bottoms, vals)]
    for i, tot in enumerate(bottoms):
        ax1.text(i, tot * 1.015, f"${tot:,.0f}/mo", ha="center", va="bottom", fontsize=12, fontweight="bold")
    ax1.set_ylabel("Monthly cost (USD)", fontsize=11)
    ax1.set_title("Monthly cost by scale", fontsize=12, fontweight="bold")
    ax1.legend(frameon=True, fontsize=9)
    ax1.set_ylim(0, max(bottoms) * 1.15)
    ax1.grid(axis="y", alpha=0.3)

    # ---- right: per-call pie (Growth scale, budget stack, hosting amortized) ----
    v = 10000
    per_call = {
        "TTS": TTS,
        "STT": STT,
        "LLM": LLM_MINI,
        "Telephony": TEL,
        "Hosting+DIDs": (120 + 5) / v,
    }
    total_pc = sum(per_call.values())
    ax2.pie(per_call.values(), labels=[f"{k}\n${x:.4f}" for k, x in per_call.items()],
            colors=[colors.get(k, "#94a3b8") for k in per_call.keys()],
            autopct="%1.0f%%", startangle=90, textprops={"fontsize": 10})
    ax2.set_title(f"Cost per call ≈ ${total_pc:.3f} (Growth scale)", fontsize=12, fontweight="bold")
    fig.text(0.5, 0.02,
             f"Per-call AI variable ≈ ${TTS+STT+LLM_MINI+TEL:.3f} (mini) / ${TTS+STT+LLM_CLAUDE+TEL:.3f} (Claude)  •  "
             "Cost per qualified transfer @15% ≈ $0.24 / $0.31  •  Human fronter ≈ $0.75/call → bot saves ~94%",
             ha="center", fontsize=10, color=MUTED, style="italic")

    fig.tight_layout(rect=[0, 0.05, 1, 0.9])
    fig.savefig(os.path.join(OUT, "cost_breakdown.png"), dpi=200, bbox_inches="tight")
    plt.close(fig)
    print("wrote cost_breakdown.png")


if __name__ == "__main__":
    architecture()
    call_flow()
    cost_breakdown()
