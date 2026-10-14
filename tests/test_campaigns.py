"""Campaign store: create / edit / delete / deploy, plus the other 3 campaigns.

Covers the dashboard's "create a brand-new campaign" flow and proves the gate
engine is generic — Medicare is just data, not a special case.
"""
from __future__ import annotations

import pytest

from agent.campaign import DEFAULTS, CampaignStore, get_store
from agent.config import Config
from agent.conversation import Conversation
from tests.conftest import ScriptedLLM


def convo_on(cid: str) -> Conversation:
    return Conversation(Config.load(), ScriptedLLM(), campaign=get_store().get(cid))


# ---------------- store ----------------

def test_four_builtin_campaigns_seed(tmp_store):
    ids = {c["id"] for c in tmp_store.list()}
    assert ids == set(DEFAULTS)
    assert tmp_store.active_id() == "medicare"


def test_every_campaign_declares_builtin_flag(tmp_store):
    assert all(c["builtin"] is True for c in tmp_store.list())


def test_create_empty_campaign(tmp_store):
    c = tmp_store.create(name="My New Campaign")
    assert c.id == "mynewcampaign"
    assert tmp_store.get(c.id) is not None
    assert c.questions, "new campaign must open on a valid step"
    assert c.disclosure, "new campaign should ship a compliance disclosure"


def test_create_clones_an_existing_script(tmp_store):
    src = tmp_store.get("medicare")
    clone = tmp_store.create(name="Medicare Variant", base="medicare")
    assert clone.id != "medicare"
    assert len(clone.questions) == len(src.questions)
    assert clone.questions[0].ask_line == src.questions[0].ask_line
    assert clone.name == "Medicare Variant"


def test_duplicate_names_get_unique_ids(tmp_store):
    a = tmp_store.create(name="Same Name")
    b = tmp_store.create(name="Same Name")
    assert a.id != b.id


def test_custom_campaign_can_be_deployed(tmp_store):
    c = tmp_store.create(name="Deployable")
    assert tmp_store.set_active(c.id) is True
    assert tmp_store.active_id() == c.id
    assert tmp_store.active().id == c.id


def test_unknown_campaign_cannot_be_deployed(tmp_store):
    assert tmp_store.set_active("does-not-exist") is False
    assert tmp_store.active_id() == "medicare"


def test_builtin_campaigns_are_never_deletable(tmp_store):
    for cid in DEFAULTS:
        assert tmp_store.delete(cid) is False, f"{cid} must be protected"
    assert {c["id"] for c in tmp_store.list()} == set(DEFAULTS)


def test_custom_campaign_is_deletable(tmp_store):
    c = tmp_store.create(name="Temp Campaign")
    assert tmp_store.delete(c.id) is True
    assert tmp_store.get(c.id) is None


def test_live_campaign_cannot_be_deleted(tmp_store):
    c = tmp_store.create(name="Going Live")
    tmp_store.set_active(c.id)
    assert tmp_store.delete(c.id) is False, "refuse to delete the live campaign"


def test_delete_unknown_returns_false(tmp_store):
    assert tmp_store.delete("nope") is False


def test_reset_defaults_restores_originals(tmp_store, tmp_path):
    store = tmp_store
    store.get("medicare").name = "Edited Name"
    store.save(store.get("medicare"))
    store.reset_defaults()
    assert store.get("medicare").name == "Medicare Fronter"
    # a reseed rewrites the file, so defaults are intact on disk too
    assert store.get("medicare").disclosure
    assert store.get("medicare").questions


def test_store_persists_across_instances(tmp_store, tmp_path):
    created = tmp_store.create(name="Persistent")
    again = CampaignStore(path=str(tmp_path / "campaigns.json"))
    assert again.get(created.id) is not None


# ---------------- migration from older stores ----------------

def _legacy_store_json(clear_disclosure_for: str | None = None) -> dict:
    from dataclasses import asdict

    out = {"campaigns": {}, "active": "medicare"}
    for cid, factory in DEFAULTS.items():
        d = asdict(factory())
        if cid == clear_disclosure_for:
            d["disclosure"] = ""          # operator deliberately cleared it
        else:
            d.pop("disclosure", None)     # written by a build before the field
        out["campaigns"][cid] = d
    return out


def test_legacy_store_backfills_missing_disclosure(tmp_path):
    import json

    path = tmp_path / "campaigns.json"
    path.write_text(json.dumps(_legacy_store_json()), encoding="utf-8")
    store = CampaignStore(path=str(path))
    assert store.get("medicare").disclosure, "old store must gain the disclosure"
    assert store.get("hi").disclosure
    # and it must be persisted, not just computed on read
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["campaigns"]["medicare"]["disclosure"]


def test_operator_cleared_disclosure_is_not_resurrected(tmp_path):
    import json

    path = tmp_path / "campaigns.json"
    path.write_text(json.dumps(_legacy_store_json(clear_disclosure_for="aca")),
                    encoding="utf-8")
    store = CampaignStore(path=str(path))
    assert store.get("aca").disclosure == ""
    assert store.get("medicare").disclosure, "the other campaigns still migrate"


def test_migration_does_not_reset_custom_edits(tmp_path):
    import json
    from dataclasses import asdict

    legacy = _legacy_store_json()
    custom = asdict(DEFAULTS["medicare"]())
    custom.pop("disclosure", None)
    custom["id"] = "custom1"
    custom["name"] = "My Edited Campaign"
    legacy["campaigns"]["custom1"] = custom

    path = tmp_path / "campaigns.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    store = CampaignStore(path=str(path))
    assert store.get("custom1").name == "My Edited Campaign", "edits must survive"
    assert store.get("custom1").disclosure == "", "customs aren't force-filled"


# ---------------- disclosure round-trip ----------------

def test_disclosure_survives_save_and_reload(tmp_store):
    c = tmp_store.get("medicare")
    c.disclosure = "This call is recorded."
    tmp_store.save(c)
    reloaded = tmp_store.get("medicare")
    assert reloaded.disclosure == "This call is recorded."
    assert reloaded.compliance()["recording_disclosure"] == "This call is recorded."


def test_clearing_disclosure_removes_it(tmp_store):
    c = tmp_store.get("aca")
    c.disclosure = ""
    tmp_store.save(c)
    assert tmp_store.get("aca").disclosure == ""


# ---------------- generic engine across campaigns ----------------

def gate(c: Conversation, greeting_reply="Sure, hi there") -> str:
    c.start()
    beat = c.hear(greeting_reply)
    assert beat.end_call is False
    return beat.ai_speech


@pytest.mark.parametrize("cid", sorted(DEFAULTS))
def test_every_campaign_asks_its_first_question(tmp_store, tmp_memory, cid):
    c = convo_on(cid)
    asked = gate(c)
    assert asked, f"{cid} must ask its first question"
    assert c.ctx.q_index == 0


# Each campaign's first question has its OWN disqualifying answer (ACA's
# qualifying answer is literally "No" — uninsured — and HI asks age first),
# so drive each one with the answer that must end the call.
DISQUALIFYING = [
    ("medicare", "No, I don't have Medicare", "AI-NO-MEDICARE"),
    ("final_expense", "No thanks, not interested", "AI-NOT-INTERESTED"),
    ("aca", "Yes I have insurance through my employer", "AI-HAS-INSURANCE"),
    ("hi", "I'm 30", "AI-NOT-ELIGIBLE"),
]


@pytest.mark.parametrize("cid,answer,expected", DISQUALIFYING)
def test_every_campaign_disqualifies_correctly(tmp_store, tmp_memory, cid, answer, expected):
    c = convo_on(cid)
    gate(c)
    beat = c.hear(answer)
    assert beat.end_call is True, f"{cid} must end on its disqualifying answer"
    assert beat.disposition == expected
    assert c.ended is True
    assert c.ctx.disposition == expected


@pytest.mark.parametrize("cid", sorted(DEFAULTS))
def test_every_campaign_ends_on_dnc(tmp_store, tmp_memory, cid):
    c = convo_on(cid)
    gate(c)
    beat = c.hear("Take me off your list please")
    assert beat.end_call is True
    assert beat.disposition == "AI-DNC"


@pytest.mark.parametrize("cid", sorted(DEFAULTS))
def test_every_campaign_welcomes_and_discloses(tmp_store, tmp_memory, cid):
    c = convo_on(cid)
    line = c.start()
    assert c.campaign.disclosure in line
    assert "{name}" not in line and "{campaign}" not in line, "tokens must be filled"


def test_custom_campaign_runs_the_generic_engine(tmp_store, tmp_memory):
    new = get_store().create(name="Custom Gate")
    c = Conversation(Config.load(), ScriptedLLM(), campaign=get_store().get(new.id))
    line = c.start()
    assert line
    beat = c.hear("Sure, go ahead")
    assert beat.end_call is False
    # custom template routes YES -> transfer, NO -> end
    assert c.hear("Yes please").disposition == "AI-TRANSFER"


def test_unknown_placeholders_never_crash_the_opening_line(tmp_store, tmp_memory):
    """Regression: a script using {agent}/{business} or an unknown token must
    still produce a speakable greeting instead of raising KeyError."""
    c = convo_on("medicare")
    c.campaign.lines["greet"] = "Hi, this is {agent} from {business}, got {nonsense}?"
    line = c.start()
    assert line and "{" not in line
    assert "Lina" in line and "EverTrust" in line


def test_malformed_format_spec_does_not_crash(tmp_store, tmp_memory):
    c = convo_on("medicare")
    c.campaign.lines["greet"] = "Hello {min_age!r:>5} there"
    assert c.start()


def test_medicare_script_has_no_final_expense_language(tmp_store, tmp_memory):
    c = convo_on("medicare")
    c.start()
    asked = c.hear("hello")
    text = (asked.ai_speech + " " + transcript(c)).lower()
    for banned in ("burial", "final expense", "funeral cost", "casket"):
        assert banned not in text, f"Medicare script leaked '{banned}'"


def transcript(c: Conversation) -> str:
    return " ".join(t["text"] for t in c.ctx.transcript)
