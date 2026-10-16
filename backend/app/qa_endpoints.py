from fastapi.responses import FileResponse
from pathlib import Path
import os

QA_OUTPUT_DIR = Path(__file__).resolve().parents[1] / 'qa_outputs'

@app.get('/api/qa/reports')
def list_qa_reports():
    """List available QA reports and artifacts in backend/qa_outputs."""
    if not QA_OUTPUT_DIR.exists():
        return {"reports": [], "artifacts": []}
    reports = []
    artifacts = []
    for p in sorted(QA_OUTPUT_DIR.iterdir()):
        if p.is_file():
            name = p.name
            if name.startswith('report_') and name.endswith('.json'):
                try:
                    with p.open('r', encoding='utf-8') as f:
                        import json
                        j = json.load(f)
                    reports.append({
                        'file': name,
                        'scenario_id': j.get('scenario_id'),
                        'name': j.get('name'),
                        'path': f'/api/qa/report/{name}'
                    })
                except Exception:
                    artifacts.append({'file': name})
            else:
                artifacts.append({'file': name})
    return {"reports": reports, "artifacts": artifacts}


@app.get('/api/qa/report/{filename}')
def get_qa_report(filename: str):
    safe = QA_OUTPUT_DIR / filename
    try:
        safe = safe.resolve()
        if QA_OUTPUT_DIR.resolve() not in safe.parents and QA_OUTPUT_DIR.resolve() != safe.parent:
            return JSONResponse({'error': 'invalid file'}, status_code=400)
    except Exception:
        return JSONResponse({'error': 'invalid file'}, status_code=400)
    if not safe.exists():
        return JSONResponse({'error': 'not found'}, status_code=404)
    return FileResponse(str(safe), media_type='application/json', filename=filename)


@app.get('/api/qa/download/{filename}')
def download_qa_artifact(filename: str):
    # Serve audio/csv/artifact files from qa_outputs. Protect against path traversal.
    safe = QA_OUTPUT_DIR / filename
    try:
        safe = safe.resolve()
        if QA_OUTPUT_DIR.resolve() not in safe.parents and QA_OUTPUT_DIR.resolve() != safe.parent:
            return JSONResponse({'error': 'invalid file'}, status_code=400)
    except Exception:
        return JSONResponse({'error': 'invalid file'}, status_code=400)
    if not safe.exists() or not safe.is_file():
        return JSONResponse({'error': 'not found'}, status_code=404)
    # guess media type by suffix
    mt = 'application/octet-stream'
    if filename.endswith('.wav'):
        mt = 'audio/wav'
    elif filename.endswith('.mp3'):
        mt = 'audio/mpeg'
    elif filename.endswith('.csv'):
        mt = 'text/csv'
    return FileResponse(str(safe), media_type=mt, filename=filename)
