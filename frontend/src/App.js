diff --git a/frontend/src/App.js b/frontend/src/App.js
index 0000000..0000000
--- a/frontend/src/App.js
+++ b/frontend/src/App.js
@@
 function ConfigPanel(){
   const [cfg, setCfg] = useState(null);
+  const [creds, setCreds] = useState([]);
+  const [newCred, setNewCred] = useState({provider:'openai', api_key:''});
   useEffect(()=>{
     fetch('/api/config').then(r=>r.json()).then(setCfg)
+    fetch('/api/credentials').then(r=>r.json()).then(setCreds)
   },[])
@@
     <div style={{marginTop:8}}>
       <button onClick={update}>Save Config</button>
     </div>
+    <div style={{marginTop:12, borderTop:'1px dashed #ddd', paddingTop:8}}>
+      <h4>API Keys</h4>
+      <div>
+        <select value={newCred.provider} onChange={e=>setNewCred({...newCred, provider:e.target.value})}>
+          <option value="openai">OpenAI</option>
+          <option value="deepgram">Deepgram</option>
+          <option value="elevenlabs">ElevenLabs</option>
+        </select>
+        <input placeholder="API key" value={newCred.api_key} onChange={e=>setNewCred({...newCred, api_key:e.target.value})} style={{marginLeft:8}} />
+        <button onClick={async ()=>{
+          await fetch('/api/credentials', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(newCred)});
+          const c = await fetch('/api/credentials').then(r=>r.json());
+          setCreds(c);
+          setNewCred({provider:'openai', api_key:''});
+        }} style={{marginLeft:8}}>Save Key</button>
+      </div>
+      <div style={{marginTop:8}}>
+        {creds.map((c,i)=> <div key={i}><strong>{c.provider}</strong>: {c.api_key_masked}</div>)}
+      </div>
+    </div>
   </div>
   )
 }
*** End Patch
