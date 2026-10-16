import React, {useState, useEffect, useRef} from 'react';

function ConfigPanel(){
  const [cfg, setCfg] = useState(null);
  const [creds, setCreds] = useState([]);
  const [newCred, setNewCred] = useState({provider:'openai', api_key:''});
  useEffect(()=>{
    fetch('/api/config').then(r=>r.json()).then(setCfg)
    fetch('/api/credentials').then(r=>r.json()).then(setCreds)
  },[])
  function update(){
    fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(cfg)}).then(r=>r.json()).then(j=>alert('Config updated'))
  }
  if(!cfg) return null;
  return (
    <div style={{marginBottom:20}}>
      <h3>Runtime Config</h3>
      <div>
        <label>STT Provider: </label>
        <select value={cfg.STT_PROVIDER} onChange={e=>setCfg({...cfg, STT_PROVIDER: e.target.value})}>
          <option value="mock">mock</option>
          <option value="deepgram">deepgram</option>
        </select>
      </div>
      <div>
        <label>TTS Provider: </label>
        <select value={cfg.TTS_PROVIDER} onChange={e=>setCfg({...cfg, TTS_PROVIDER: e.target.value})}>
          <option value="mock">mock</option>
          <option value="elevenlabs">elevenlabs</option>
        </select>
      </div>
      <div>
        <label>LLM Provider: </label>
        <select value={cfg.LLM_PROVIDER} onChange={e=>setCfg({...cfg, LLM_PROVIDER: e.target.value})}>
          <option value="mock">mock</option>
          <option value="openai">openai</option>
        </select>
      </div>
      <div>
        <label>Transfer Provider: </label>
        <select value={cfg.TRANSFER_PROVIDER} onChange={e=>setCfg({...cfg, TRANSFER_PROVIDER: e.target.value})}>
          <option value="mock">mock</option>
          <option value="vici">vici</option>
          <option value="twilio">twilio</option>
        </select>
      </div>
      <div style={{marginTop:8}}>
        <button onClick={update}>Save Config</button>
      </div>
      <div style={{marginTop:12, borderTop:'1px dashed #ddd', paddingTop:8}}>
        <h4>API Keys</h4>
        <div>
          <select value={newCred.provider} onChange={e=>setNewCred({...newCred, provider:e.target.value})}>
            <option value="openai">OpenAI</option>
            <option value="deepgram">Deepgram</option>
            <option value="elevenlabs">ElevenLabs</option>
            <option value="vici">ViciDial</option>
            <option value="twilio">Twilio</option>
          </select>
          <input placeholder="API key" value={newCred.api_key} onChange={e=>setNewCred({...newCred, api_key:e.target.value})} style={{marginLeft:8}} />
          <button onClick={async ()=>{
            await fetch('/api/credentials', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(newCred)});
            const c = await fetch('/api/credentials').then(r=>r.json());
            setCreds(c);
            setNewCred({provider:'openai', api_key:''});
          }} style={{marginLeft:8}}>Save Key</button>
        </div>
        <div style={{marginTop:8}}>
          {creds.map((c,i)=> (
            <div key={i} style={{display:'flex',alignItems:'center',gap:8}}>
              <div><strong>{c.provider}</strong>: {c.api_key_masked}</div>
              <button onClick={async ()=>{
                const r = await fetch(`/api/credentials/${c.provider}/reveal`);
                if(r.ok){
                  const j = await r.json();
                  alert(`Full key for ${c.provider}: ${j.api_key}`);
                } else {
                  alert('Failed to reveal key');
                }
              }}>Reveal</button>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
*** End Patch