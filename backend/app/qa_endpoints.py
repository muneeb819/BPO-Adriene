"""QA report & artifact endpoints.

Serves the artifacts produced by the QA runner (backend/scripts/qa_runner.py),
which writes everything under backend/qa_outputs/:

  - report_{scenario_id}.json   per-scenario QA report
  - {scenario_id}_turn_N_agent.wav  per-turn agent audio
  - results_summary.csv         consolidated results of `qa_runner.py --all`

Routes (mounted by app.main):
  GET /api/qa/reports             list reports and artifacts
  GET /api/qa/report/{filename}   return a report JSON
  GET /api/qa/download/{filename} stream audio / csv / other artifacts
"""
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/qa", tags=["qa"])

QA_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "qa_outputs"

_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".csv": "text/csv",
    ".json": "application/json",
}


def _resolve_artifact(filename: str) -> Path:
    """Resolve `filename` inside qa_outputs, rejecting path traversal."""
    base = QA_OUTPUT_DIR.resolve()
    target = (base / filename).resolve()
    if target != base and base not in target.parents:
        raise HTTPException(status_code=400, detail="invalid file")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return target


@router.get("/reports")
def list_qa_reports():
    """List available QA reports and artifacts in backend/qa_outputs."""
    if not QA_OUTPUT_DIR.exists():
        return {"reports": [], "artifacts": []}

    reports = []
    artifacts = []
    for path in sorted(QA_OUTPUT_DIR.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if name.startswith("report_") and name.endswith(".json"):
            try:
                with path.open("r", encoding="utf-8") as f:
                    payload = json.load(f)
            except (OSError, ValueError):
                artifacts.append({"file": name})
                continue
            reports.append(
                {
                    "file": name,
                    "scenario_id": payload.get("scenario_id"),
                    "name": payload.get("name"),
                    "path": f"/api/qa/report/{name}",
                }
            )
        else:
            artifacts.append({"file": name})
    return {"reports": reports, "artifacts": artifacts}


@router.get("/report/{filename}")
def get_qa_report(filename: str):
    """Return the full JSON report for a scenario."""
    target = _resolve_artifact(filename)
    return FileResponse(str(target), media_type="application/json", filename=filename)


@router.get("/download/{filename}")
def download_qa_artifact(filename: str):
    """Stream audio / csv / other artifacts from qa_outputs."""
    target = _resolve_artifact(filename)
    media_type = _MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(str(target), media_type=media_type, filename=filename)
