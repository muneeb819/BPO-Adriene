import React, {useState, useEffect, useRef} from 'react';

function ConfigPanel(){
  const [cfg, setCfg] = useState(null);
  useEffect(()=>{
    fetch('/api/config').then(r=>r.json()).then(setCfg)
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
      <div style={{marginTop:8}}>
        <button onClick={update}>Save Config</button>
      </div>
    </div>
  )
}

function Dashboard({onOpenCall}){
  const [calls, setCalls] = useState([]);
  useEffect(()=>{
    fetch('/api/calls').then(r=>r.json()).then(setCalls)
  },[])
  return (
    <div style={{marginBottom:20}}>
      <h3>Recent Calls</h3>
      <table style={{width:'100%',borderCollapse:'collapse'}}>
        <thead><tr><th>ID</th><th>Session</th><th>State</th><th>Created</th></tr></thead>
        <tbody>
          {calls.map(c => (
            <tr key={c.id} style={{borderTop:'1px solid #eee',cursor:'pointer'}} onClick={()=>onOpenCall(c)}>
              <td>{c.id}</td>
              <td>{c.session_id}</td>
              <td>{c.state}</td>
              <td>{new Date(c.created_at).toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function CallDetail({call}){
  const [detail, setDetail] = useState(null);
  useEffect(()=>{
    if(!call) return;
    fetch(`/api/calls/${call.id}`).then(r=>r.json()).then(setDetail)
  },[call])
  if(!call) return null;
  if(!detail) return <div>Loading...</div>
  return (
    <div>
      <h3>Call Detail — {detail.session_id}</h3>
      <div style={{border:'1px solid #ddd', padding:10, maxHeight:300, overflow:'auto'}}>
        {detail.transcripts.map((t,i)=> (
          <div key={i}><strong>{t.role}:</strong> {t.text} <span style={{color:'#999'}}>{new Date(t.timestamp).toLocaleString()}</span></div>
        ))}
      </div>
    </div>
  )
}

export default function App(){
  const [sessionId, setSessionId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [selectedCall, setSelectedCall] = useState(null);
  const wsRef = useRef(null);

  async function startCall(){
    const res = await fetch('/api/call/start', {method: 'POST'});
    const j = await res.json();
    setSessionId(j.session_id);
    setMessages(m => [...m, {from:'bot', text: j.bot_prompt}]);
    // open websocket
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss://' : 'ws://'}${location.host}/ws/call/${j.session_id}`);
    ws.onmessage = (ev) => {
      const d = JSON.parse(ev.data);
      if(d.type === 'bot_prompt' || d.type === 'bot_reply'){
        setMessages(m => [...m, {from:'bot', text: d.text}]);
        speak(d.text);
      }
      if(d.type === 'transfer'){
        setMessages(m => [...m, {from:'system', text: 'Transfer triggered'}]);
      }
    };
    ws.onclose = () => console.log('ws closed');
    wsRef.current = ws;
  }

  function sendUtterance(){
    if(!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(JSON.stringify({type: 'user_utterance', text: input}));
    setMessages(m => [...m, {from:'user', text: input}]);
    setInput('');
  }

  function speak(text){
    if(!window.speechSynthesis) return;
    const utter = new SpeechSynthesisUtterance(text);
    utter.rate = 1;
    utter.pitch = 1;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utter);
  }

  return (
    <div style={{padding:20,fontFamily:'Arial'}}>
      <h2>BPO Adriene — Local Debug</h2>
      <ConfigPanel />
      <Dashboard onOpenCall={(c)=>setSelectedCall(c)} />
      <CallDetail call={selectedCall} />

      <div style={{marginTop:20}}>
        <div style={{marginBottom:10}}>
          <button onClick={startCall}>Start Mock Call</button>
        </div>
        <div style={{border:'1px solid #ddd', padding:10, height:200, overflow:'auto'}}>
          {messages.map((m,i)=> (
            <div key={i} style={{marginBottom:8}}>
              <strong>{m.from}:</strong> {m.text}
            </div>
          ))}
        </div>
        <div style={{marginTop:10}}>
          <input value={input} onChange={e=>setInput(e.target.value)} style={{width:'70%'}} />
          <button onClick={sendUtterance}>Send</button>
        </div>
        <div style={{marginTop:20,fontSize:12,color:'#555'}}>
          Tip: type short replies like "yes", "no", or an age like "67" to simulate caller responses.
        </div>
      </div>
    </div>
  );
}
