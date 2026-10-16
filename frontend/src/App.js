*** Begin Patch
*** Update File: frontend/src/App.js
@@
 function CallDetail({call}){
   const [detail, setDetail] = useState(null);
   useEffect(()=>{
     if(!call) return;
     fetch(`http://localhost:8000/api/calls/${call.id}`).then(r=>r.json()).then(setDetail)
   },[call])
   if(!call) return null;
   if(!detail) return <div>Loading...</div>
   return (
     <div>
       <h3>Call Detail — {detail.session_id}</h3>
+      <div style={{marginBottom:8}}>
+        <button onClick={async ()=>{
+          const r = await fetch('/api/transfer/test', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({call_id: call.id})});
+          const j = await r.json();
+          if(r.ok) alert('Transfer test result: ' + j.status);
+          else alert('Transfer failed: ' + (j.error || JSON.stringify(j)));
+        }}>Test Transfer</button>
+      </div>
       <div style={{border:'1px solid #ddd', padding:10, maxHeight:300, overflow:'auto'}}>
         {detail.transcripts.map((t,i)=> (
           <div key={i}><strong>{t.role}:</strong> {t.text} <span style={{color:'#999'}}>{new Date(t.timestamp).toLocaleString()}</span></div>
         ))}
       </div>
     </div>
   )
 }
*** End Patch