import React, {useState, useEffect, useRef} from 'react';

/* ------------------------------------------------------------------ *
 *  Voice engine — makes speech sound like a real person.
 *
 *  Two engines:
 *   • "neural" — local Kokoro-82M on the backend (20 US-English voices
 *     with model-card grades). Replies are fetched per sentence and
 *     pipelined: chunk N plays while chunk N+1 is being synthesized.
 *   • "browser" — speechSynthesis fallback (auto-used if the neural
 *     model isn't installed or a fetch fails mid-sentence).
 * ------------------------------------------------------------------ */
let speakToken = 0;
let neuralToken = 0;
let neuralAudio = null;      // currently playing <audio> (neural engine)
let neuralReady = false;     // flipped true once /api/tts/voices says installed
let neuralVoices = [];       // Kokoro voice registry (name, gender, grade…)

function splitSentences(text){
  return (text.match(/[^.!?…]+[.!?…]*/g) || [text]).map(s => s.trim()).filter(Boolean);
}

function stopNeural(){
  neuralToken++;                       // invalidate in-flight fetches/plays
  if(neuralAudio){ try{ neuralAudio.pause(); }catch(e){} neuralAudio = null; }
}

function fetchChunk(text, voice, speed){
  return fetch('/api/tts', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({text, voice, speed}),
  })
    .then(r => r.ok ? r.blob() : Promise.reject(r.status))
    .then(b => URL.createObjectURL(b))
    .catch(() => null);
}

function playWav(url, token){
  return new Promise(resolve => {
    const a = new Audio(url);
    neuralAudio = a;
    const done = (ok) => { URL.revokeObjectURL(url); resolve(ok); };
    a.onended = () => done(token === neuralToken);
    a.onerror = () => done(false);
    a.play().then(() => {}, () => done(false));   // autoplay rejection
  });
}

async function speakNeural(text){
  const token = ++neuralToken;
  const voice = localStorage.getItem('adriene_kvoice') || 'af_heart';
  const speed = parseFloat(localStorage.getItem('adriene_kspeed') || '1.0');
  const segs = splitSentences(text);
  // bundle very short fragments so one-word lines still sound natural
  const chunks = [];
  segs.forEach(s => {
    if(s.split(/\s+/).length < 4 && chunks.length) chunks[chunks.length-1] += ' ' + s;
    else chunks.push(s);
  });
  // generous guard: CPU synth can be a few seconds per chunk
  armSpeechEndGuard(chunks.reduce((a, c) => a + c.length * 0.09, 0) * 1000 + 15000);
  let pending = fetchChunk(chunks[0], voice, speed);   // prefetch first now…
  for(let i = 0; i < chunks.length; i++){
    const wavP = pending;
    pending = (i + 1 < chunks.length)
      ? fetchChunk(chunks[i+1], voice, speed)          // …synthesize next while playing
      : null;
    const wav = await wavP;
    if(token !== neuralToken){ return; }               // superseded — drop quietly
    if(!wav){                                          // neural hiccup → finish in browser voice
      speakBrowser(chunks.slice(i).join(' '), window.__browserVoices || []);
      return;
    }
    const ok = await playWav(wav, token);
    if(!ok){ return; }
  }
  fireBotSpeechEnd();                                  // whole reply spoken — release the turn
}

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

function speakBrowser(text, voices){
  if(!window.speechSynthesis || !text) return;
  const synth = window.speechSynthesis;
  const token = ++speakToken;
  synth.cancel();
  const saved = localStorage.getItem('adriene_voice') || '';
  const voice = pickVoice(voices, saved);
  const baseRate = parseFloat(localStorage.getItem('adriene_rate') || '1.03');
  const empathetic = /understand|sorry|fair|apolog|no rush|completely|appreciate/i.test(text);
  const segments = splitSentences(text);
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
      if(i === segments.length - 1) u.onend = fireBotSpeechEnd;   // turn is over
      synth.speak(u);
    }, i * 120);                                    // a breath between sentences
  });
  // guard: if the browser swallows onend (known Chrome quirk after cancel),
  // still release the turn so hands-free mode can re-arm the mic
  const estMs = segments.reduce((a, s) => a + s.length / (13 * baseRate), 0) * 1000
              + segments.length * 120 + 2500;
  armSpeechEndGuard(estMs);
}

/* Engine-aware entry point: neural (Kokoro) when selected & ready,
 * browser speechSynthesis otherwise. */
let botSpeaking = false;
let onBotSpeechEndCb = null;    // App hooks this: re-arm the mic in hands-free mode
let speechEndTimer = null;      // safety net — some browsers miss onend after cancel()
let autoListen = false;         // App keeps these in sync (half-duplex mic gating)
let dictationInst = null;

function markBotSpeaking(){ botSpeaking = true; if(speechEndTimer){ clearTimeout(speechEndTimer); speechEndTimer = null; } }
function fireBotSpeechEnd(){
  if(!botSpeaking) return;
  botSpeaking = false;
  if(speechEndTimer){ clearTimeout(speechEndTimer); speechEndTimer = null; }
  if(onBotSpeechEndCb) onBotSpeechEndCb();
}
function armSpeechEndGuard(ms){
  if(speechEndTimer) clearTimeout(speechEndTimer);
  speechEndTimer = setTimeout(fireBotSpeechEnd, ms);
}

function speak(text, voices){
  if(!text) return;
  stopNeural();                                     // cut any neural audio in flight
  speakToken++;                                     // and any queued browser lines
  if(window.speechSynthesis) window.speechSynthesis.cancel();
  markBotSpeaking();
  // hands-free mode is half-duplex: mute the mic while Adriene is talking
  if(autoListen && dictationInst && dictationInst.active) dictationInst.stop();
  const eng = localStorage.getItem('adriene_engine') || 'auto';
  if((eng === 'neural' || eng === 'auto') && neuralReady){
    speakNeural(text);
    return;
  }
  speakBrowser(text, voices);
}

/* ------------------------------------------------------------------ *
 *  Dictation — customer speaks, bot understands (browser SpeechRecognition).
 *  Chrome/Edge on localhost; graceful error hints otherwise.
 * ------------------------------------------------------------------ */
const SRClass = (typeof window !== 'undefined') &&
  (window.SpeechRecognition || window.webkitSpeechRecognition);

class Dictation {
  constructor(h){ this.h = h; this.rec = null; this.active = false; }
  start(){
    if(!SRClass){ this.h.onError('unsupported'); return false; }
    if(this.active) return true;
    try{
      const rec = new SRClass();
      this.rec = rec;
      rec.lang = 'en-US';
      rec.continuous = false;          // one utterance per arm; auto mode re-arms
      rec.interimResults = true;       // live "hearing…" transcript
      rec.maxAlternatives = 1;
      rec.onstart = () => { this.active = true; this.h.onState(true); };
      rec.onresult = (e) => {
        let interim = '', final = '';
        for(let i = e.resultIndex; i < e.results.length; i++){
          const r = e.results[i];
          if(r.isFinal) final += r[0].transcript; else interim += r[0].transcript;
        }
        if(interim) this.h.onInterim(interim);
        if(final){
          const text = final.trim();
          this.h.onInterim('');
          if(text) this.h.onFinal(text);
        }
      };
      rec.onerror = (e) => {
        if(e.error === 'not-allowed' || e.error === 'service-not-allowed') this.h.onError('denied');
        else if(e.error === 'network') this.h.onError('network');
        else if(e.error !== 'no-speech' && e.error !== 'aborted') this.h.onError(e.error);
      };
      rec.onend = () => { this.active = false; this.h.onState(false); this.h.onEnd(); };
      rec.start();
      return true;
    }catch(err){ this.h.onError(String(err)); return false; }
  }
  stop(){ try{ if(this.rec) this.rec.stop(); }catch(e){} }
}

function VoicePanel(){
  const [bVoices, setBVoices] = useState([]);
  const [engine, setEngine] = useState(localStorage.getItem('adriene_engine') || 'auto');
  const [kVoices, setKVoices] = useState([]);
  const [status, setStatus] = useState(null);        // /api/tts/voices snapshot
  const [kSel, setKSel] = useState(localStorage.getItem('adriene_kvoice') || 'af_heart');
  const [kSpeed, setKSpeed] = useState(parseFloat(localStorage.getItem('adriene_kspeed') || '1.0'));
  const [bSel, setBSel] = useState(localStorage.getItem('adriene_voice') || '');
  const [bRate, setBRate] = useState(parseFloat(localStorage.getItem('adriene_rate') || '1.03'));

  useEffect(()=>{
    const load = () => { const v = window.speechSynthesis ? window.speechSynthesis.getVoices() : []; setBVoices(v); window.__browserVoices = v; };
    load();
    if(window.speechSynthesis) window.speechSynthesis.onvoiceschanged = load;
    fetch('/api/tts/voices').then(r=>r.ok?r.json():null).then(j=>{
      if(!j) return;
      setStatus(j); setKVoices(j.voices || []);
      neuralVoices = j.voices || [];
      neuralReady = !!j.installed;
      if(j.installed && !j.warmed) fetch('/api/tts/warm', {method:'POST'}).catch(()=>{});
    }).catch(()=>{});
    return () => { if(window.speechSynthesis) window.speechSynthesis.onvoiceschanged = null; };
  },[]);

  function changeEngine(e){
    const eng = e.target.value;
    setEngine(eng); localStorage.setItem('adriene_engine', eng);
    previewNow(eng);
  }
  function changeKVoice(name){
    setKSel(name); localStorage.setItem('adriene_kvoice', name);
    previewNow(engine);
  }
  function changeKSpeed(s){
    setKSpeed(s); localStorage.setItem('adriene_kspeed', String(s));
    fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({TTS_VOICE: kSel, TTS_SPEED: String(s)})}).catch(()=>{});
  }
  function changeBVoice(name){
    setBSel(name); localStorage.setItem('adriene_voice', name);
    speakBrowser('Hi, this is Adriene — is this voice warm enough for you?', bVoices);
  }
  function changeBRate(r){
    setBRate(r); localStorage.setItem('adriene_rate', String(r));
  }
  function previewNow(eng){
    const text = 'Hey! Adriene here — ready when you are.';
    const want = eng || activeEngine;
    stopNeural(); speakToken++; if(window.speechSynthesis) window.speechSynthesis.cancel();
    if(want === 'neural' && neuralReady){
      const token = neuralToken;   // stopNeural() just bumped it — play under that id
      fetchChunk(text, kSel, kSpeed).then(u => { if(u) playWav(u, token); });
    } else {
      speakBrowser(text, bVoices);
    }
  }

  const activeEngine = engine === 'auto' ? (neuralReady ? 'neural' : 'browser') : engine;
  const useNeural = activeEngine === 'neural' && neuralReady;
  const females = kVoices.filter(v=>v.gender==='f');
  const males   = kVoices.filter(v=>v.gender==='m');
  const opt = v => (
    <option key={v.name} value={v.name}>
      {v.name} · {v.traits} {v.grade} — {v.blurb}
    </option>
  );

  return (
    <div style={{marginBottom:20, border:'1px solid #e6e6f0', borderRadius:10, padding:12, background:'#fbfbff'}}>
      <h3 style={{marginTop:0}}>🔊 Adriene's Voice</h3>

      <div>
        <label>Engine: </label>
        <select value={neuralReady ? activeEngine : 'browser'} onChange={changeEngine} disabled={!neuralReady}>
          <option value="neural">Adriene Neural — Kokoro-82M (20 US voices, local)</option>
          <option value="browser">Browser voices (offline fallback)</option>
        </select>
        {status && !status.installed &&
          <span style={{marginLeft:8, fontSize:12, color:'#a66'}}>neural unavailable — install kokoro</span>}
        {status && status.installed && !status.warmed &&
          <span style={{marginLeft:8, fontSize:12, color:'#888', fontStyle:'italic'}}>warming model… (first reply may pause briefly)</span>}
      </div>

      {useNeural ? (
        <div style={{marginTop:8}}>
          <label>Voice: </label>
          <select value={kSel} onChange={e=>changeKVoice(e.target.value)}>
            <optgroup label="🚺 Female">{females.map(opt)}</optgroup>
            <optgroup label="🚹 Male">{males.map(opt)}</optgroup>
          </select>
          <button style={{marginLeft:8}} onClick={()=>previewNow()}>Preview</button>
        </div>
      ) : (
        <div style={{marginTop:8}}>
          <label>Voice: </label>
          <select value={bSel || (pickVoice(bVoices,'')||{}).name || ''} onChange={e=>changeBVoice(e.target.value)}>
            {bVoices.length === 0 && <option value="">(browser voices loading…)</option>}
            {[...bVoices].sort((a,b)=>scoreVoice(b)-scoreVoice(a)).map(v =>
              <option key={v.name} value={v.name}>{v.name} — {v.lang}</option>)}
          </select>
          <button style={{marginLeft:8}} onClick={()=>previewNow()}>Preview</button>
        </div>
      )}

      <div style={{marginTop:8}}>
        <label>{useNeural ? 'Pace (Kokoro speed)' : 'Pace'}: </label>
        {useNeural ? (
          <input type="range" min="0.7" max="1.5" step="0.05" value={kSpeed}
                 onChange={e=>changeKSpeed(parseFloat(e.target.value))} />
        ) : (
          <input type="range" min="0.8" max="1.3" step="0.01" value={bRate}
                 onChange={e=>changeBRate(parseFloat(e.target.value))} />
        )}
        <span style={{marginLeft:6, fontSize:12, color:'#555'}}>
          {(useNeural ? kSpeed : bRate).toFixed(2)}×</span>
        <span style={{marginLeft:10, fontSize:11, color:'#999'}}>
          Kokoro sounds best at 100–200 tokens — replies are auto-chunked into sentences.</span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 *  Config panel
 * ------------------------------------------------------------------ */
function ConfigPanel(){
  const [cfg, setCfg] = useState(null);
  const [saved, setSaved] = useState(false);
  const load = () => fetch('/api/config').then(r=>r.json()).then(setCfg).catch(()=>{});
  useEffect(load, []);
  function update(){
    fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(cfg)})
      .then(r=>r.json())
      .then(()=>{ setSaved(true); load(); })        // refresh: shows what's actually active
      .catch(()=>setSaved(false));
  }
  if(!cfg) return null;
  const a = cfg._active || {};
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
          <option value="kokoro">kokoro (local neural)</option>
          <option value="elevenlabs">elevenlabs</option>
        </select>
      </div>
      <div>
        <label>LLM Provider: </label>
        <select value={cfg.LLM_PROVIDER} onChange={e=>setCfg({...cfg, LLM_PROVIDER: e.target.value})}>
          <option value="mock">mock (local brain)</option>
          <option value="openai">openai</option>
          <option value="anthropic">anthropic</option>
          <option value="gemini">gemini</option>
        </select>
      </div>
      <div style={{marginTop:8, fontSize:12, color:'#555'}}>
        “mock” runs the full local brain (NLU + Medicare knowledge + compliance +
        memory). To attach a real LLM, just set the env key — no code change:
        <code> OPENAI_API_KEY</code> · <code>ANTHROPIC_API_KEY</code> ·
        <code> GEMINI_API_KEY</code> (models via <code>OPENAI_MODEL</code> /
        <code>ANTHROPIC_MODEL</code> / <code>GEMINI_MODEL</code>). The brain
        then re-words its decisions through the model; any failure falls back
        to the local voice instantly, so a call never dies.
      </div>
      {a.llm && (
        <div style={{marginTop:8, fontSize:12, color:'#2f6f4f', background:'#f0faf4',
                     border:'1px solid #d7ecdf', borderRadius:6, padding:'6px 8px'}}>
          <b>Active right now:</b> STT {a.stt} (browser mic does the real
          speech→text) · TTS {a.tts} → <b>{a.tts_engine}</b> ·
          LLM {a.llm} → <b>{a.llm_engine}</b> · transfer {a.transfer}
        </div>
      )}
      <div style={{marginTop:8}}>
        <button onClick={update}>Save Config</button>
        {saved && <span style={{marginLeft:8, fontSize:12, color:'#2f6f4f'}}>✓ saved</span>}
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
  const [micOn, setMicOn] = useState(false);
  const [interim, setInterim] = useState('');
  const [micErr, setMicErr] = useState('');
  const [autoMode, setAutoMode] = useState(false);
  const wsRef = useRef(null);
  const autoRef = useRef(false);
  const micOnRef = useRef(false);
  const sessionRef = useRef(null);
  const dictRef = useRef(null);

  const micSupported = !!(typeof window !== 'undefined') &&
    !!(window.SpeechRecognition || window.webkitSpeechRecognition);

  function submitUtterance(text){
    const t = (text || '').trim();
    if(!t) return;
    if(!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN){
      setMessages(m => [...m, {from:'system', text: 'Call not open — press Start Mock Call first.'}]);
      return;
    }
    wsRef.current.send(JSON.stringify({type: 'user_utterance', text: t}));
    setMessages(m => [...m, {from:'user', text: t}]);
    setLastUser(t);
    setInput('');
    setInterim('');
    setThinking(true);
  }

  function startMic(quiet){
    if(!sessionRef.current){
      if(!quiet) setMicErr('Start the call first, then press 🎤.');
      return;
    }
    // barge-in: cut Adriene off mid-sentence — the customer floor is live
    stopNeural(); speakToken++;
    if(window.speechSynthesis) window.speechSynthesis.cancel();
    fireBotSpeechEnd();               // release the bot's turn cleanly
    setInterim('');
    setMicErr('');
    dictRef.current.start();
  }

  if(!dictRef.current){
    dictRef.current = new Dictation({
      onFinal: (t) => submitUtterance(t),
      onInterim: (t) => setInterim(t),
      onState: (s) => { micOnRef.current = s; setMicOn(s); if(s) setMicErr(''); },
      onError: (kind) => {
        const msg = kind === 'denied'
          ? 'Microphone blocked — allow mic access (icon in the address bar), then press 🎤 again.'
          : kind === 'network'
          ? 'Speech input needs an internet connection (Chrome voice typing). Typing still works.'
          : kind === 'unsupported'
          ? 'This browser has no speech recognition — use Chrome or Edge, or type instead.'
          : 'Mic error: ' + kind;
        setMicErr(msg);
        autoRef.current = false; setAutoMode(false);
      },
      onEnd: () => {
        // hands-free: re-arm unless Adriene is talking (half-duplex, no echo)
        if(autoRef.current && !micOnRef.current && !botSpeaking){
          setTimeout(() => { if(autoRef.current && !micOnRef.current && !botSpeaking) startMic(true); }, 350);
        }
      },
    });
  }
  dictationInst = dictRef.current;

  useEffect(()=>{
    onBotSpeechEndCb = () => {
      if(autoRef.current && sessionRef.current){
        setTimeout(() => { if(autoRef.current && !micOnRef.current) startMic(true); }, 250);
      }
    };
    return () => { onBotSpeechEndCb = null; };
  },[]);

  useEffect(()=>{
    const load = () => setVoices(window.speechSynthesis ? window.speechSynthesis.getVoices() : []);
    load();
    if(window.speechSynthesis) window.speechSynthesis.onvoiceschanged = load;
  },[]);

  function toggleMic(){
    if(micOnRef.current){ dictRef.current.stop(); autoRef.current = false; setAutoMode(false); }
    else startMic(false);
  }
  function changeAuto(e){
    const on = e.target.checked;
    autoRef.current = on; autoListen = on; setAutoMode(on);
    if(on){ if(sessionRef.current) startMic(true); setMicErr(''); }
    else if(micOnRef.current){ dictRef.current.stop(); }
  }

  async function startCall(){
    const res = await fetch('/api/call/start', {method: 'POST'});
    const j = await res.json();
    setSessionId(j.session_id);
    sessionRef.current = j.session_id;
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

  function sendUtterance(){ submitUtterance(input); }

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
          <button onClick={toggleMic} title={micOn ? 'Stop listening' : 'Speak your answer'}
                  style={{padding:'8px 14px', marginRight:6, fontSize:16,
                          background: micOn ? '#dc3545' : '#fff', color: micOn ? '#fff' : '#333',
                          border: micOn ? '1px solid #b02a37' : '1px solid #ccc', borderRadius:8,
                          cursor:'pointer'}}>
            {micOn ? '⏹' : '🎤'}
          </button>
          <input value={input} onChange={e=>setInput(e.target.value)} onKeyDown={onKey}
                 placeholder={micOn ? 'Listening… (or type here)'
                                    : 'Answer naturally — any wording works…'}
                 style={{width:'62%', padding:8}} />
          <button onClick={sendUtterance} style={{padding:'8px 16px', marginLeft:6}}>Send</button>
          <label style={{marginLeft:10, fontSize:13, cursor:'pointer', userSelect:'none'}}>
            <input type="checkbox" checked={autoMode} onChange={changeAuto}
                   disabled={!micSupported} style={{marginRight:4}} />
            🔄 Hands-free (mic auto-listens after Adriene speaks)
          </label>
        </div>
        {interim && (
          <div style={{color:'#1f6feb', fontSize:13, marginTop:4, fontStyle:'italic'}}>
            🎤 hearing: “{interim}…”
          </div>
        )}
        {micOn && !interim && (
          <div style={{color:'#888', fontSize:12, marginTop:4}}>🎤 listening — go ahead, Adriene can hear you…</div>
        )}
        {micErr && (
          <div style={{color:'#b02a37', fontSize:13, marginTop:4}}>⚠ {micErr}</div>
        )}
        {!micSupported && (
          <div style={{color:'#8a6d00', fontSize:12, marginTop:4}}>
            💡 Voice input works in Chrome/Edge — this browser only supports typing.
          </div>
        )}
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
