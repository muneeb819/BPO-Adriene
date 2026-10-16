import React, {useState, useEffect, useRef} from 'react';

export default function App(){
  const [sessionId, setSessionId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const wsRef = useRef(null);

  async function startCall(){
    const res = await fetch('http://localhost:8000/api/call/start', {method: 'POST'});
    const j = await res.json();
    setSessionId(j.session_id);
    setMessages(m => [...m, {from:'bot', text: j.bot_prompt}]);
    // open websocket
    const ws = new WebSocket(`ws://localhost:8000/ws/call/${j.session_id}`);
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
      <div style={{marginBottom:10}}>
        <button onClick={startCall}>Start Mock Call</button>
      </div>
      <div style={{border:'1px solid #ddd', padding:10, height:300, overflow:'auto'}}>
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
  );
}
