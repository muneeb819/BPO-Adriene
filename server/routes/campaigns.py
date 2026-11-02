"""Campaign routes: choose, edit, deploy and reset the call scripts.

The campaign store is a JSON file on disk (agent/campaign.py); these routes are
the dashboard's editor for it, plus /api/insights for the self-learning memory.
"""
from __future__ import annotations


from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


router = APIRouter()


@router.get("/api/campaigns")
def campaigns_list():
    from agent.campaign import get_store

    store = get_store()
    return {"campaigns": store.list(), "active": store.active_id()}


@router.post("/api/campaigns")
async def campaign_create(request: Request):
    """Create a brand-new campaign (optionally cloning an existing script)."""
    from agent.campaign import get_store

    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    store = get_store()
    base = (body.get("base") or "").strip() or None
    if base and store.get(base) is None:
        return JSONResponse({"error": f"base campaign '{base}' not found"}, status_code=404)
    c = store.create(name=name, description=(body.get("description") or "").strip(), base=base)
    return {"ok": True, "id": c.id, "name": c.name}


@router.delete("/api/campaigns/{cid}")
def campaign_delete(cid: str):
    """Delete a custom campaign. Refuses the live one and the built-in defaults."""
    from agent.campaign import DEFAULTS, get_store

    store = get_store()
    if cid == store.active_id():
        return JSONResponse({"error": "can't delete the live campaign — deploy another one first"}, status_code=409)
    if cid in DEFAULTS:
        return JSONResponse({"error": "built-in campaigns can't be deleted (use Reset instead)"}, status_code=409)
    if not store.delete(cid):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True, "deleted": cid}


@router.get("/api/campaigns/active")
def campaign_active():
    from agent.campaign import get_store

    c = get_store().active()
    return {"id": c.id, "name": c.name, "compliance": c.compliance()}


@router.post("/api/campaigns/active")
async def campaign_deploy(request: Request):
    from agent.campaign import get_store

    data = await request.json()
    cid = (data.get("id") or "").strip()
    ok = get_store().set_active(cid)
    if not ok:
        return JSONResponse({"error": f"unknown campaign '{cid}'"}, status_code=404)
    return {"ok": True, "active": cid}


@router.get("/api/campaigns/{cid}")
def campaign_get(cid: str):
    from agent.campaign import get_store

    c = get_store().get(cid)
    if c is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    from dataclasses import asdict

    return asdict(c)


@router.put("/api/campaigns/{cid}")
async def campaign_save(cid: str, request: Request):
    from agent.campaign import get_store, _from_dict

    body = await request.json()
    body["id"] = cid  # id is immutable; path wins
    try:
        campaign = _from_dict(body)
    except Exception as e:
        return JSONResponse({"error": f"invalid campaign: {e}"}, status_code=400)
    get_store().save(campaign)
    return {"ok": True, "id": cid, "updated": campaign.updated}


@router.post("/api/campaigns/{cid}/reset")
def campaign_reset(cid: str):
    from agent.campaign import DEFAULTS, get_store

    factory = DEFAULTS.get(cid)
    if factory is None:
        return JSONResponse({"error": "no default for this campaign"}, status_code=404)
    get_store().save(factory())
    return {"ok": True, "id": cid}


@router.post("/api/campaigns/reset-all")
def campaigns_reset_all():
    from agent.campaign import get_store

    get_store().reset_defaults()
    return {"ok": True}


# ---------------- SELF-LEARNING INSIGHTS ----------------

@router.get("/api/insights")
def insights():
    from agent.memory import get_memory

    return get_memory().insights()
