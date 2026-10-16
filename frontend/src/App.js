import React, {useState, useEffect, useRef} from 'react';

/* ------------------------------------------------------------------ *
 *  Voice engine — makes speechSynthesis sound like a real person:
 *  best available voice, sentence-by-sentence pacing (breath between
 *  sentences), warm/young pitch, empathetic slowdown, no stale queues.
 * ------------------------------------------------------------------ */
let speakToken = 0;

function scoreVoice(v){
  const n = ((v.name||'') + ' ' + (v.lang||'')).toLowerCase();
  let s = 0;
  if(/natural|neural|online/.test(n)) s += 6;
  if(/aria|ava|jenny|sonia|libby|emma|samantha|zira|susan|karen|moira|google us english/.test(n)) s += 4;
  if(/female/.test(n)) s += 3;
  if(/en-us|en_us|english.*united states/.test(n)) s += 2;
  if(/david|male desktop|robot|compact|espeak/.test(n)) s -= 5;
  if((v.lang||'').toLowerCase().startsWith('en')) s += 1; else s -= 10;
  return s;
}

function pickVoice(voices, saved){
  if(!voices || !voices.length) return null;
  if(saved){ const hit = voices.find(v => v.name === saved); if(hit) return hit; }
  return [...voices].sort((a,b) => scoreVoice(b) - scoreVoice(a))[0];
}

function speak(text, voices){
  if(!window.speechSynthesis || !text) return;
  const synth = window.speechSynthesis;
  const token = ++speakToken;
  synth.cancel();
  const saved = localStorage.getItem('adriene_voice') || '';
  const voice = pickVoice(voices, saved);
  const baseRate = parseFloat(localStorage.getItem('adriene_rate') || '1.03');
  const empathetic = /understand|sorry|fair|apolog|no rush|completely|appreciate/i.test(text);
  const segments = (text.match(/[^.!?…]+[.!?…]*/g) || [text]).map(s => s.trim()).filter(Boolean);
  segments.forEach((seg, i) => {
    setTimeout(() => {
      if(token !== speakToken) return;              // superseded by a newer line
      const u = new SpeechSynthesisUtterance(seg);
      if(voice){ u.voice = voice; u.lang = voice.lang; }
      const jitter = Math.sin(Date.now()/900 + i) * 0.03;
      u.rate  = Math.max(0.75, Math.min(1.45,
                 (empathetic ? baseRate - 0.09 : baseRate) + jitter));
      u.pitch = Math.max(0.8, Math.min(1.5,
                 (empathetic ? 1.04 : 1.10) + (i % 2) * 0.05));
      u.volume = 1;
      synth.speak(u);
    }, i * 120);                                    // a breath between sentences
  });
}

function VoicePanel(){
  const [voices, setVoices] = useState([]);
  const [sel, setSel] = useState(localStorage.getItem('adriene_voice') || '');
  const [rate, setRate] = useState(parseFloat(localStorage.getItem('adriene_rate') || '1.03'));

  useEffect(()=>{
    const load = () => { const v = window.speechSynthesis ? window.speechSynthesis.getVoices() : []; setVoices(v); };
    load();
    if(window.speechSynthesis) window.speechSynthesis.onvoiceschanged = load;
    return () => { if(window.speechSynthesis) window.speechSynthesis.onvoiceschanged = null; };
  },[]);

  function changeVoice(name){
    setSel(name); localStorage.setItem('adriene_voice', name);
    speak('Hi, this is Adriene — is this voice warm enough for you?', voices);
  }
  function changeRate(r){
    setRate(r); localStorage.setItem('adriene_rate', String(r));
  }

  const ordered = [...voices].sort((a,b)=>scoreVoice(b)-scoreVoice(a));
  const active = sel || (pickVoice(voices, '') ? pickVoice(voices, '').name : '');
  return (
    <div style={{marginBottom:20, border:'1px solid #e6e6f0', borderRadius:10, padding:12, background:'#fbfbff'}}>
      <h3 style={{marginTop:0}}>🔊 Adriene's Voice</h3>
      <div>
        <label>Voice: </label>
        <select value={active} onChange={e=>changeVoice(e.target.value)}>
          {ordered.length === 0 && <option value="">(browser voices loading…)</option>}
          {ordered.map(v => <option key={v.name} value={v.name}>{v.name} — {v.lang}</option>)}
        </select>
        <button style={{marginLeft:8}} onClick={()=>speak('Hey! Adriene here — ready when you are.', voices)}>Preview</button>
      </div>
      <div style={{marginTop:8}}>
        <label>Pace: </label>
        <input type="range" min="0.8" max="1.3" step="0.01" value={rate}
               onChange={e=>changeRate(parseFloat(e.target.value))} />
        <span style={{marginLeft:6, fontSize:12, color:'#555'}}>{rate.toFixed(2)}×</span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 *  Config panel
 * ------------------------------------------------------------------ */
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
      <div style={{marginTop:8, fontSize:12, color:'#555'}}>
        “mock” now runs the full local brain (NLU + Medicare knowledge +
        compliance + memory). Add OPENAI_API_KEY to auto-upgrade wording.
      </div>
      <div style={{marginTop:8}}>
        <button onClick={update}>Save Config</button>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ *
 *  Self-learning status
 * ------------------------------------------------------------------ */
function BrainPanel(){
  const [status, setStatus] = useState(null);
  const load = () => fetch('/api/brain/status').then(r=>r.json()).then(setStatus).catch(()=>{});
  useEffect(load, []);
  if(!status) return null;
  const m = status.memory || {};
  return (
    <div style={{marginBottom:20, border:'1px solid #e6e6f0', borderRadius:10, padding:12, background:'#f7fffb'}}>
      <h3 style={{marginTop:0}}>🧠 Self-Learning Brain</h3>
      <div style={{fontSize:13}}>
        <span>persona: <b>{status.persona?.name}, {status.persona?.age}</b> ({status.persona?.style})</span><br/>
        <span>brain: <b>{status.llm_provider}</b></span> ·
        <span> learned phrasings: <b>{m.learned_count}</b></span> ·
        <span> unknowns queued: <b>{m.unknown_count}</b></span> ·
        <span> feedback: <b>{m.feedback_count}</b></span>
        <button style={{marginLeft:8}} onClick={load}>Refresh</button>
      </div>
      {m.top_unknowns && m.top_unknowns.length > 0 && (
        <div style={{marginTop:6, fontSize:12, color:'#8a6d3b'}}>
          wants to learn: {m.top_unknowns.slice(0,3).map(u=><code key={u.utterance} style={{marginRight:6}}>{u.utterance}</code>)}
        </div>
      )}
    </div>
  );
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
  if(!detail) return <div>Loading...</div>;
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

/* ------------------------------------------------------------------ *
 *  Main app: live call with expressive voice + feedback learning
 * ------------------------------------------------------------------ */
export default function App(){
  const [sessionId, setSessionId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [selectedCall, setSelectedCall] = useState(null);
  const [voices, setVoices] = useState([]);
  const [thinking, setThinking] = useState(false);
  const [lastUser, setLastUser] = useState('');
  const wsRef = useRef(null);

  useEffect(()=>{
    const load = () => setVoices(window.speechSynthesis ? window.speechSynthesis.getVoices() : []);
    load();
    if(window.speechSynthesis) window.speechSynthesis.onvoiceschanged = load;
  },[]);

  async function startCall(){
    const res = await fetch('/api/call/start', {method: 'POST'});
    const j = await res.json();
    setSessionId(j.session_id);
    setMessages(m => [...m, {from:'bot', text: j.bot_prompt}]);
    speak(j.bot_prompt, voices);
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss://' : 'ws://'}${location.host}/ws/call/${j.session_id}`);
    ws.onmessage = (ev) => {
      const d = JSON.parse(ev.data);
      if(d.type === 'bot_prompt' || d.type === 'bot_reply'){
        setMessages(m => [...m, {from:'bot', text: d.text, reply: d.type === 'bot_reply'}]);
        setThinking(false);
        speak(d.text, voices);
      }
      if(d.type === 'transfer'){
        setMessages(m => [...m, {from:'system', text: '📞 Transfer triggered — a Benefits Specialist is taking over.'}]);
        setThinking(false);
      }
      if(d.type === 'error'){
        setMessages(m => [...m, {from:'system', text: `Error: ${d.message}`}]);
        setThinking(false);
      }
    };
    ws.onclose = () => console.log('ws closed');
    wsRef.current = ws;
  }

  function sendUtterance(){
    if(!input.trim()) return;
    if(!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN){
      setMessages(m => [...m, {from:'system', text: 'Call not open — press Start Mock Call first.'}]);
      return;
    }
    wsRef.current.send(JSON.stringify({type: 'user_utterance', text: input}));
    setMessages(m => [...m, {from:'user', text: input}]);
    setLastUser(input);
    setInput('');
    setThinking(true);
  }

  function onKey(e){ if(e.key === 'Enter') sendUtterance(); }

  async function sendFeedback(msg, verdict){
    try{
      await fetch('/api/brain/feedback', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
          utterance: lastUser, bot_reply: msg.text, verdict,
          state: sessionId || '', session_id: sessionId || '',
        }),
      });
      // messages are append-only; bubble() renders a copy carrying the index
      setMessages(m => m.map((x, idx) => idx === msg.id ? {...x, saved: verdict} : x));
    }catch(e){ console.error(e); }
  }

  const bubble = (m) => {
    const isBot = m.from === 'bot';
    const isUser = m.from === 'user';
    return (
      <div key={m.id} style={{
        display:'flex', justifyContent: isUser ? 'flex-end' : 'flex-start', marginBottom:8
      }}>
        <div style={{
          maxWidth:'78%', padding:'8px 12px', borderRadius:14,
          background: isBot ? '#eef2ff' : (isUser ? '#1f6feb' : '#fff8e1'),
          color: isUser ? '#fff' : '#222',
          border: isBot ? '1px solid #d7e0ff' : '1px solid transparent',
          fontSize:14, lineHeight:1.45,
        }}>
          {!isUser && m.from === 'system' && <span style={{fontSize:12, color:'#8a6d00'}}>{m.text}</span>}
          {isBot && <span>{m.text}</span>}
          {isUser && <span>{m.text}</span>}
          {isBot && m.reply && (
            <span style={{marginLeft:8, whiteSpace:'nowrap'}}>
              <button title="Helpful" onClick={()=>sendFeedback(m,'good')}
                      style={{border:'none', background:'transparent', cursor:'pointer', fontSize:13}}>
                {m.saved === 'good' ? '👍✓' : '👍'}
              </button>
              <button title="Wrong answer — brain will learn" onClick={()=>sendFeedback(m,'bad')}
                      style={{border:'none', background:'transparent', cursor:'pointer', fontSize:13}}>
                {m.saved === 'bad' ? '👎✓' : '👎'}
              </button>
            </span>
          )}
        </div>
      </div>
    );
  };

  return (
    <div style={{padding:20,fontFamily:'Arial', maxWidth:960, margin:'0 auto'}}>
      <h2>BPO Adriene — AI Voice Bot</h2>
      <VoicePanel />
      <BrainPanel />
      <ConfigPanel />
      <Dashboard onOpenCall={(c)=>setSelectedCall(c)} />
      <CallDetail call={selectedCall} />

      <div style={{marginTop:20}}>
        <div style={{marginBottom:10}}>
          <button onClick={startCall} style={{padding:'8px 16px', fontSize:15}}>
            📞 Start Mock Call
          </button>
          {sessionId && <span style={{marginLeft:10, fontSize:12, color:'#555'}}>session {sessionId.slice(0,8)}…</span>}
        </div>
        <div style={{border:'1px solid #ddd', borderRadius:10, padding:12, height:280, overflow:'auto', background:'#fff'}}>
          {messages.length === 0 && <div style={{color:'#888'}}>Transcript will appear here…</div>}
          {messages.map((m, i) => bubble({...m, id:i}))}
          {thinking && <div style={{color:'#888', fontStyle:'italic', marginLeft:6}}>Adriene is thinking…</div>}
        </div>
        <div style={{marginTop:10}}>
          <input value={input} onChange={e=>setInput(e.target.value)} onKeyDown={onKey}
                 placeholder="Answer naturally — any wording works…" style={{width:'70%', padding:8}} />
          <button onClick={sendUtterance} style={{padding:'8px 16px', marginLeft:6}}>Send</button>
        </div>
        <div style={{marginTop:12, fontSize:12, color:'#555', lineHeight:1.6}}>
          <b>Try anything:</b> “nah, not yet” · “yeah I've had it for years” · “sixty-seven” ·
          “what's the difference between Advantage and Medigap?” · “is this a scam?” ·
          “take me off your list” · “call me back tomorrow” · “I'm confused” —
          the brain understands paraphrases, answers from Medicare knowledge,
          stays compliant, and <b>learns</b> from 👍/👎.
        </div>
      </div>
    </div>
  );
}
