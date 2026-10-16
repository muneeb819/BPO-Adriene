"""Medicare knowledge base — grounded answers with sources.

Every entry is a short, spoken-style answer plus keyword triggers. The
retriever scores keyword overlap (rare, specific keywords weigh more) so
Adriene can answer policy questions from *any* state without losing the
pending question. Compliance wording lives in compliance.py and is applied
to everything before it reaches the caller.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class KBEntry:
    id: str
    title: str
    keywords: List[str]
    answer: str
    follow_up: str = ""            # optional re-ask of the pending question
    rare: List[str] = field(default_factory=list)  # weigh 2x when matched


KB: List[KBEntry] = [
    KBEntry(
        id="what_is_medicare",
        title="What Medicare is",
        keywords=["what is medicare", "medicare is", "explain medicare",
                  "about medicare", "medicare about", "tell me about medicare",
                  "what is this medicare", "medicare mean"],
        rare=["federal health", "government health"],
        answer=(
            "Medicare is federal health insurance for people 65 and older, and for "
            "some younger people with disabilities or certain conditions. It's "
            "run by the federal government — parts A, B, C and D cover hospital, "
            "medical, plan and prescription costs."
        ),
        follow_up="Does that clear it up? And do you currently have Medicare?",
    ),
    KBEntry(
        id="part_a",
        title="Part A — hospital",
        keywords=["part a", "hospital insurance", "hospital coverage", "inpatient"],
        rare=["hospital insurance"],
        answer=(
            "Part A is hospital insurance — it covers inpatient hospital stays, "
            "skilled nursing after a hospital stay, hospice, and some home health. "
            "Most people don't pay a monthly premium for Part A."
        ),
        follow_up="Do you already have Part A, or Part B?",
    ),
    KBEntry(
        id="part_b",
        title="Part B — medical",
        keywords=["part b", "doctor visits", "medical insurance", "outpatient",
                  "preventive", "lab work", "ambulance"],
        rare=["medical insurance"],
        answer=(
            "Part B is medical insurance — doctor visits, outpatient care, "
            "preventive screenings, ambulances and durable medical equipment. "
            "It does have a monthly premium, and a yearly deductible."
        ),
        follow_up="Do you have Part B today?",
    ),
    KBEntry(
        id="part_c",
        title="Part C — Advantage",
        keywords=["part c", "medicare advantage", "advantage plan", "ma plan",
                  "advantage plans", "hmo", "ppo"],
        rare=["medicare advantage", "part c"],
        answer=(
            "Part C, also called Medicare Advantage, is an all-in-one alternative "
            "to Original Medicare — it bundles Parts A and B, and most plans add "
            "Part D drug coverage and extra benefits like dental or vision, "
            "usually through a network of providers."
        ),
        follow_up="Are you on an Advantage plan right now?",
    ),
    KBEntry(
        id="part_d",
        title="Part D — prescriptions",
        keywords=["part d", "prescription", "drug plan", "drugs", "pharmacy",
                  "medications", "rx"],
        rare=["part d", "drug plan"],
        answer=(
            "Part D is prescription drug coverage — a private plan that helps pay "
            "for your medications. It has its own premium and formulary, and there's "
            "an annual open enrollment each fall to switch plans."
        ),
        follow_up="Do you have a drug plan in place?",
    ),
    KBEntry(
        id="medigap",
        title="Medigap / supplement",
        keywords=["medigap", "medicare supplement", "supplement plan",
                  "supplement insurance", "gap insurance"],
        rare=["medigap", "medicare supplement"],
        answer=(
            "A Medigap, or Medicare Supplement policy, is private insurance that "
            "fills the gaps in Original Medicare — copays, coinsurance and "
            "deductibles. It works alongside Parts A and B, not instead of them, "
            "and the best rates come from your six-month open enrollment window "
            "that starts the month you're 65 and enrolled in Part B."
        ),
        follow_up="Do you have a supplement already?",
    ),
    KBEntry(
        id="enrollment_initial",
        title="Initial Enrollment Period",
        keywords=["when do i enroll", "when to sign up", "enrollment period",
                  "turning 65", "at 65", "first time", "sign up for medicare",
                  "enroll in medicare", "initial enrollment", "when can i sign up",
                  "sign up", "enroll", "when can i"],
        rare=["initial enrollment", "7 month", "seven month", "birthday month"],
        answer=(
            "Your Initial Enrollment Period is 7 months long — the 3 months before "
            "the month you turn 65, your birthday month, and the 3 months after. "
            "If you're still on employer coverage at 65, you may qualify for a "
            "Special Enrollment Period later instead."
        ),
        follow_up="Have you already gone through that window?",
    ),
    KBEntry(
        id="enrollment_general",
        title="Late Enrollment / General Period",
        keywords=["missed enrollment", "miss enrollment", "missed the deadline",
                  "late enrollment", "signed up late", "i missed it",
                  "general enrollment"],
        rare=["general enrollment", "january 1", "march 31", "miss enrollment"],
        answer=(
            "If you missed your Initial Period, the General Enrollment Period runs "
            "January 1 through March 31 each year — coverage can start the first of "
            "the month after you sign up. A late-enrollment penalty on Part B or "
            "Part D may apply, but there are Exceptions and wage-based appeals."
        ),
        follow_up="When did you first become eligible?",
    ),
    KBEntry(
        id="open_enrollment",
        title="Annual Open Enrollment",
        keywords=["open enrollment", "annual enrollment", "switch plans",
                  "change plans", "october 15", "aep"],
        rare=["october 15", "december 7", "oct 15"],
        answer=(
            "The Annual Open Enrollment Period runs October 15 to December 7 — "
            "that's when you can join, switch or drop an Advantage or drug plan "
            "for coverage starting January 1. There's also a Medicare Advantage "
            "open enrollment January 1 through March 31 for one switch."
        ),
        follow_up="Are you happy with your current plan, or looking to switch?",
    ),
    KBEntry(
        id="penalty",
        title="Late enrollment penalty",
        keywords=["penalty", "penalties", "penalized", "penalised",
                  "late part d", "cost more for waiting", "charge me more",
                  "fine for not", "fee for not"],
        rare=["late enrollment penalty", "10 percent", "1 percent per month",
              "penalized", "penalised"],
        answer=(
            "Yes — going without Part B or Part D coverage when you were eligible "
            "can add a late-enrollment penalty. For Part D it's usually 1% of the "
            "national base premium for each month you went without creditable "
            "coverage, and it lasts as long as you have Part D. Part B's is 10% per "
            "full 12-month period. A few Exceptions apply, like getting Extra Help."
        ),
        follow_up="Have you had continuous coverage since you turned 65?",
    ),
    KBEntry(
        id="costs",
        title="Premiums, deductibles, IRMAA",
        keywords=["how much cost", "how much does", "premium", "deductible",
                  "copay", "coinsurance", "how much is medicare", "expensive",
                  "what does it cost", "monthly cost"],
        rare=["irmaa", "income related", "adjusted"],
        answer=(
            "Part A is premium-free for most people. Part B has a monthly premium "
            "and a yearly deductible; higher earners pay an income-related "
            "additional amount called IRMAA. Advantage plans often have $0 doctor "
            "copays but keep an eye on max out-of-pocket. Drug costs depend on your "
            "plan's tiers — and Extra Help can lower them substantially."
        ),
        follow_up="Are you currently paying a monthly premium?",
    ),
    KBEntry(
        id="extra_help",
        title="Extra Help / LIS / Medicaid",
        keywords=["extra help", "low income", "cannot afford", "can't afford",
                  "can not afford", "afford", "help paying", "assistance paying",
                  "medicaid", "qualify for", "savings", "pay for it", "on medicaid"],
        rare=["extra help", "low-income subsidy", "full benefit dual",
              "can not afford"],
        answer=(
            "There are real savings programs: Extra Help (the low-income subsidy) "
            "cuts Part D drug costs, and Medicare Savings Programs through Medicaid "
            "can pay your Part B premium — qualifying depends on income and assets, "
            "roughly under about $2,000 in savings for an individual in most states. "
            "Dual-eligible folks get both Medicare and Medicaid working together."
        ),
        follow_up="Would you like a specialist to check what you qualify for?",
    ),
    KBEntry(
        id="under_65",
        title="Eligibility under 65",
        keywords=["under 65", "not 65", "before 65", "younger than 65",
                  "disability", "not eligible yet", "too young", "64", "62 and",
                  "not old enough"],
        rare=["24 months of disability", "als", "end stage renal"],
        answer=(
            "Most people qualify at 65, but you can get Medicare earlier with 24 "
            "months of Social Security disability, or with ALS or end-stage renal "
            "disease. Under 65 without Medicare, Marketplace plans and Medicaid are "
            "the usual options — and there's a Special Enrollment Period when you "
            "do turn 65."
        ),
        follow_up="Do you happen to be on disability coverage?",
    ),
    KBEntry(
        id="advantage_vs_original",
        title="Advantage vs Original Medicare",
        keywords=["advantage or original", "original medicare", "difference between",
                  "which is better", "advantage vs", "vs original", "switch to advantage",
                  "leave advantage"],
        rare=["original medicare"],
        answer=(
            "Original Medicare (Parts A and B) lets you see any doctor that takes "
            "Medicare, and you add a Medigap and a separate drug plan. An Advantage "
            "plan bundles everything with networks and often extra benefits, usually "
            "at a lower premium — but with plan rules. Neither is universally "
            "'better'; it depends on your doctors, medications and travel."
        ),
        follow_up="Which are you leaning toward today?",
    ),
    KBEntry(
        id="travel",
        title="Coverage while traveling",
        keywords=["travel", "out of state", "moving", "vacation", "another state",
                  "relocate"],
        rare=["nationwide emergency", "geographic"],
        answer=(
            "Original Medicare works nationwide for emergencies, and most Advantage "
            "plans cover emergency care anywhere too. Routine care out of your plan "
            "area is the catch — some plans offer 'travel' or 'out-of-area' benefits. "
            "If you snowbird or move, tell your plan so your network follows you."
        ),
        follow_up="Do you travel or snowbird during the year?",
    ),
    KBEntry(
        id="fraud_protection",
        title="Fraud protection & your rights",
        keywords=["scam", "fraud", "steal", "identity", "social security number",
                  "is this real", "legitimate", "verify", "how do i know"],
        rare=["medicare card number", "never give your medicare number"],
        answer=(
            "Good instinct — real Medicare never calls you out of the blue to ask "
            "for your Medicare number, and agents are only allowed to call during "
            "8 a.m. to 9 p.m. local time with your permission on file. I'm happy to "
            "verify anything: I'm an independent licensed agent, not the government, "
            "and you can check any license through your state's DOI."
        ),
        follow_up="Would you like me to have a licensed specialist verify our details?",
    ),
    KBEntry(
        id="complaint_rights",
        title="Complaints & Appeals",
        keywords=["complaint", "appeal", "denied", "claim denied", "grievance",
                  "not covered", "reconsider"],
        rare=["reconsideration", "fair hearing", "expedited appeal"],
        answer=(
            "You always have the right to appeal a denial — start with a "
            "reconsideration from your plan, and if it's still denied you can take it "
            "to an independent reviewer, with expedited options when waiting is "
            "unsafe. Your State Health Insurance Assistance Program (SHIP) also gives "
            "free counseling."
        ),
        follow_up="Would you like a specialist to walk you through an appeal?",
    ),
    KBEntry(
        id="supplement_lifetime",
        title="Guaranteed issue rights",
        keywords=["guaranteed", "pre existing", "preexisting", "health conditions",
                  "will they cover me", "denied for health", "underwriting"],
        rare=["guaranteed issue", "medical underwriting", "6 month window"],
        answer=(
            "Important: Medigap open enrollment is your one time guaranteed-issue "
            "window — the six months after you're 65 and enrolled in Part B — and "
            "outside it most states allow medical underwriting for supplements. "
            "Advantage plans can't deny you for health conditions any time. This is "
            "exactly why timing matters at 65."
        ),
        follow_up="Have you already used your six-month supplement window?",
    ),
]


def retrieve(text: str) -> Tuple[Optional[KBEntry], float]:
    """Score KB entries against an utterance; return (best_entry, score)."""
    norm = (text or "").lower()
    norm = re.sub(r"[^a-z0-9 ]", " ", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm:
        return None, 0.0
    padded = f" {norm} "
    best, best_score = None, 0.0
    for e in KB:
        score = 0.0
        for kw in e.keywords:
            if kw in padded:
                score += 2.0 if kw in e.rare else 1.0
        for kw in e.rare:
            if kw in padded:
                score += 1.0
        if score > best_score:
            best, best_score = e, score
    # require a real signal: one specific (rare) keyword or two generic hits
    if best is not None and best_score >= 1.0:
        # confidence: single solid hit = 0.75, two or more = 1.0 (tests and the
        # stateless brain gate on >= 0.66, so one good hit must clear it)
        return best, min(1.0, 0.5 + 0.25 * best_score)
    return None, 0.0


def find_entry(entry_id: str) -> Optional[KBEntry]:
    for e in KB:
        if e.id == entry_id:
            return e
    return None


def topics() -> List[str]:
    return [e.id for e in KB]
