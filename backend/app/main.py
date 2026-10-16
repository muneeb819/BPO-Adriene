*** Begin Patch
*** Update File: backend/app/main.py
@@
 @app.websocket("/ws/call/{session_id}")
 async def websocket_call(websocket: WebSocket, session_id: str):
@@
-                    if should_transfer:
-                        session_obj.state = "transferring"
-                        db_rec.state = "transferring"
-                        db_rec.updated_at = __import__('datetime').datetime.utcnow()
-                        dbconn.add(db_rec)
-                        dbconn.commit()
-                        # trigger transfer event via adapter
-                        transfer_adapter.transfer_call(session_id)
-                        media.stop_call(session_id)
-                        await websocket.send_json({"type": "transfer", "message": "transfer_triggered"})
-                        await websocket.close()
-                        break
+                    if should_transfer:
+                        session_obj.state = "transferring"
+                        db_rec.state = "transferring"
+                        db_rec.updated_at = __import__('datetime').datetime.utcnow()
+                        dbconn.add(db_rec)
+                        dbconn.commit()
+                        # Create a TransferRecord
+                        from app.models import TransferRecord
+                        tr = TransferRecord(call_id=db_rec.id, session_id=session_id, target=None, status='requested')
+                        dbconn.add(tr)
+                        dbconn.commit()
+                        # trigger transfer event via adapter (mock by default)
+                        try:
+                            transfer_adapter.transfer_call(session_id, target=None)
+                            tr.status = 'completed'
+                        except Exception as e:
+                            tr.status = 'failed'
+                        tr.updated_at = __import__('datetime').datetime.utcnow()
+                        dbconn.add(tr)
+                        dbconn.commit()
+                        media.stop_call(session_id)
+                        await websocket.send_json({"type": "transfer", "message": "transfer_triggered", "status": tr.status})
+                        await websocket.close()
+                        break
*** End Patch