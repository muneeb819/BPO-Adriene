*** Begin Patch
*** Update File: backend/app/main.py
@@
 @app.get("/api/calls/{call_id}")
def get_call(call_id: int, db: Session = Depends(get_session)):
@@
     return {"id": rec.id, "session_id": rec.session_id, "state": rec.state, "transcripts": tx}
+
+
+@app.post('/api/transfer/test')
+async def test_transfer(payload: dict, db: Session = Depends(get_session)):
+    """Manual test transfer for QA: {"call_id": <int>}"""
+    call_id = payload.get('call_id')
+    if not call_id:
+        return JSONResponse({'error': 'call_id required'}, status_code=400)
+    rec = db.exec(select(CallRecord).where(CallRecord.id == call_id)).first()
+    if not rec:
+        return JSONResponse({'error': 'call not found'}, status_code=404)
+    # create transfer record
+    from app.models import TransferRecord
+    tr = TransferRecord(call_id=rec.id, session_id=rec.session_id, target=None, status='requested')
+    db.add(tr)
+    db.commit()
+    db.refresh(tr)
+    # attempt transfer
+    try:
+        ok = transfer_adapter.transfer_call(rec.session_id, target=None)
+        tr.status = 'completed' if ok else 'failed'
+        db.add(tr)
+        db.commit()
+        return {'status': tr.status, 'transfer_id': tr.id}
+    except Exception as e:
+        tr.status = 'failed'
+        db.add(tr)
+        db.commit()
+        return JSONResponse({'error': 'transfer failed', 'detail': str(e)}, status_code=500)
*** End Patch