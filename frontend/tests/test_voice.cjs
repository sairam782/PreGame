const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'../static/voice.js'),'utf8');
const tick = () => new Promise(resolve=>setImmediate(resolve));
function setup(getUserMedia) {
  const elements = new Map();
  const container = {isConnected:true, set innerHTML(value) {this.html=value;elements.clear();},get innerHTML(){return this.html;},querySelector(selector){if(!elements.has(selector))elements.set(selector,{textContent:'',addEventListener(){},append(){}});return elements.get(selector);}};
  let stopped=0;
  const stream={getTracks:()=>[{stop:()=>stopped++}]};
  class Recorder {
    static isTypeSupported(t){return t.startsWith('audio/webm');}
    constructor(){this.state='inactive';this.mimeType='audio/webm';}
    start(){this.state='recording';}
    stop(){this.state='inactive';this.ondataavailable?.({data:new Blob(['audio'])});this.onstop?.();}
  }
  class Audio {setAttribute(){} pause(){} load(){} removeAttribute(){} play(){return Promise.resolve();}}
  const calls=[];let transcript='Clara runs the money.';
  const context={window:{MediaRecorder:Recorder,addEventListener(){}},navigator:{mediaDevices:{getUserMedia:getUserMedia||(()=>Promise.resolve(stream))}},MediaRecorder:Recorder,Audio,Blob,AbortController,URL:{createObjectURL:()=> 'blob:test',revokeObjectURL(){}},setTimeout,clearTimeout,setInterval,clearInterval,Date,
    fetch:async(url,opts)=>{calls.push({url,opts});return {ok:true,json:async()=>url.endsWith('/status')?{enabled:true}:{text:transcript},blob:async()=>new Blob(['audio'])};}};
  vm.runInNewContext(source,context);
  const voice=context.window.DebriefVoice;
  const received=[];
  const options={container,clientId:'C08',token:'csrf-token',isSaving:()=>false,onTranscript:t=>received.push(t),getReadback:()=> 'Proposed note. Retirement: 2027.'};
  voice.mount(options);
  return {voice,container,options,calls,received,stream,stopped:()=>stopped};
}
test('recording is opt-in, stops tracks, and submits only after Stop',async()=>{
  const f=setup();await tick();
  assert.equal(f.calls.length,1); // Status check only, no audio upload.
  await f.container.querySelector('[data-record]').onclick();
  assert.equal(f.voice.busy(),true);
  assert.equal(f.calls.length,1);
  f.container.querySelector('[data-record]').onclick();await tick();
  assert.equal(f.stopped(),1);
  assert.deepEqual(f.received,['Clara runs the money.']);
  const sent=f.calls.find(c=>c.url.endsWith('/transcribe'));
  assert.equal(sent.opts.headers['X-Workspace-Token'],'csrf-token');
  assert.equal(sent.opts.headers['Content-Type'],'audio/webm');
  assert.equal(f.voice.busy(),false);f.voice.close();
});
test('closing cancels recording without uploading it',async()=>{
  const f=setup();await tick();await f.container.querySelector('[data-record]').onclick();
  f.voice.close();await tick();
  assert.equal(f.stopped(),1);assert.equal(f.calls.filter(c=>c.url.endsWith('/transcribe')).length,0);
});
test('late microphone permission after client switch cannot attach to a new client',async()=>{
  let resolve;
  const f=setup(()=>new Promise(r=>resolve=r));await tick();
  const recording=f.container.querySelector('[data-record]').onclick();
  f.voice.close();resolve(f.stream);await recording;
  assert.equal(f.stopped(),1);assert.equal(f.calls.length,1);assert.equal(f.received.length,0);
});
test('read-back uses supplied note text and no browser API credential',async()=>{
  const f=setup();await tick();await f.container.querySelector('[data-read]').onclick();
  const sent=f.calls.find(c=>c.url.endsWith('/speak'));
  assert.deepEqual(JSON.parse(sent.opts.body),{text:'Proposed note. Retirement: 2027.'});
  assert.equal(Object.keys(sent.opts.headers).some(x=>x.toLowerCase()==='xi-api-key'),false);
  f.voice.close();
});
