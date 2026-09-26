/* Microphone and playback are opt-in, scoped to the active debrief, and never carry an API key. */
(() => {
  'use strict';
  let active;
  const mic = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/></svg>';
  const speaker = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m11 4-6 5H2v6h3l6 5V4Zm5 4a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/></svg>';
  class Voice {
    constructor(options) {
      this.options = options; this.state = 'idle'; this.message = 'Checking voice connection…';
      this.enabled = false; this.closed = false; this.audio = new Audio(); this.audio.controls = true;
      this.audio.setAttribute('aria-label','Debrief voice playback');
      this.check();
    }
    async check() {
      try {
        const r = await fetch('/api/workspace/voice/status');
        if (!r.ok) throw new Error();
        const status = await r.json(); this.enabled = status.enabled;
        this.message = this.enabled ? 'Ready · record up to 90 seconds' : 'Voice is not configured on this laptop. You can still type.';
      } catch { this.message = 'Voice is unavailable. You can still type and review notes.'; }
      this.render();
    }
    busy() { return ['permission','recording','transcribing','generating'].includes(this.state); }
    render() {
      if (this.closed || !this.options.container.isConnected) return;
      const recording = this.state === 'recording', busy = this.busy();
      const supported = Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
      this.options.container.innerHTML = `<div class="voice-controls"><div class="voice-buttons"><button type="button" data-record ${!this.enabled || !supported || (busy && !recording) || this.options.isSaving() ? 'disabled':''} aria-pressed="${recording}">${mic}<span>${recording?'Stop recording':'Record debrief'}</span></button><button type="button" data-read ${!this.enabled || busy || this.options.isSaving() ? 'disabled':''}>${speaker}Read back</button>${busy?'<button type="button" class="text-button" data-cancel>Cancel</button>':''}</div><p class="voice-status ${recording?'recording':''}" role="status" aria-live="polite"></p><div class="voice-playback"></div><p class="voice-disclosure">Voice uses ElevenLabs online. Recording starts only when you click Record; review the transcript before proposing notes.</p></div>`;
      this.options.container.querySelector('.voice-status').textContent = !supported && this.enabled ? 'Microphone recording is unavailable here. Open localhost in Chrome or Edge; read-back still works.' : this.message;
      this.options.container.querySelector('[data-record]').onclick = () => recording ? this.stop() : this.record();
      this.options.container.querySelector('[data-read]').onclick = () => this.speak();
      this.options.container.querySelector('[data-cancel]')?.addEventListener('click',()=>this.cancel());
      if (this.audioURL) this.options.container.querySelector('.voice-playback').append(this.audio);
    }
    releaseMicrophone() {
      clearInterval(this.timer); this.timer = null;
      this.stream?.getTracks().forEach(track=>track.stop()); this.stream = null;
    }
    cancel() {
      this.generation = (this.generation || 0) + 1;
      this.abort?.abort();
      if (this.recorder?.state === 'recording') { this.recorder.onstop = null; this.recorder.stop(); }
      this.releaseMicrophone(); this.audio.pause(); this.state='idle'; this.message='Voice stopped. Your typed text is unchanged.'; this.render();
    }
    close() {
      this.cancel(); this.closed=true;
      this.audio.removeAttribute('src'); this.audio.load();
      if(this.audioURL) URL.revokeObjectURL(this.audioURL);
    }
    async record() {
      if (this.busy()) return;
      this.audio.pause(); this.state='permission'; this.message='Allow microphone access to begin recording.'; this.render();
      const generation = this.generation = (this.generation || 0) + 1;
      try {
        const stream = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}});
        if (this.closed || this.generation !== generation) { stream.getTracks().forEach(t=>t.stop()); return; }
        this.stream=stream;
        const mime=['audio/webm;codecs=opus','audio/mp4','audio/ogg;codecs=opus','audio/webm'].find(t=>MediaRecorder.isTypeSupported(t));
        this.recorder=new MediaRecorder(stream,mime?{mimeType:mime}:undefined);
        const chunks=[];let bytes=0, overflow=false;
        this.recorder.ondataavailable=e=>{if(e.data.size){chunks.push(e.data);bytes+=e.data.size;if(bytes>8*1024*1024){overflow=true;this.stop();}}};
        this.recorder.onerror=()=>{this.cancel();this.message='The microphone stopped unexpectedly. Please record again.';this.render();};
        this.recorder.onstop=async()=>{
          this.releaseMicrophone();
          if(this.closed || this.generation!==generation)return;
          if(overflow){this.state='idle';this.message='Recording exceeded 8 MB. Record a shorter summary.';this.render();return;}
          await this.transcribe(new Blob(chunks,{type:this.recorder.mimeType || mime || 'audio/webm'}),generation);
        };
        this.recorder.start(1000);this.started=Date.now();this.state='recording';this.message='Recording · 0:00 / 1:30';this.render();
        this.timer=setInterval(()=>{
          const seconds=Math.floor((Date.now()-this.started)/1000);
          this.message=`Recording · ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')} / 1:30`;
          const status=this.options.container.querySelector('.voice-status');if(status)status.textContent=this.message;
          if(seconds>=90)this.stop();
        },1000);
      } catch(error) {
        this.releaseMicrophone();
        if(this.closed || this.generation!==generation)return;
        this.state='idle';this.message=error.name==='NotAllowedError'?'Microphone access was denied. Allow it in your browser or type your debrief.':'Could not open the microphone. Check that a microphone is connected.';this.render();
      }
    }
    stop() {
      if(this.recorder?.state==='recording'){
        clearInterval(this.timer);this.state='transcribing';this.message='Transcribing your debrief…';this.render();this.recorder.stop();
      }
    }
    async post(path,body,type) {
      this.abort=new AbortController();
      const timeout=setTimeout(()=>this.abort?.abort(),70000);
      try {
        const response=await fetch(path,{method:'POST',headers:{'Content-Type':type,'X-Workspace-Token':this.options.token},body,signal:this.abort.signal});
        if(!response.ok){let result;try{result=await response.json();}catch{}throw new Error(result?.error || 'Voice request failed. Try again.');}
        return response;
      } finally { clearTimeout(timeout); }
    }
    async transcribe(blob,generation) {
      this.state='transcribing';this.message='Transcribing your debrief…';this.render();
      try {
        const response=await this.post('/api/workspace/voice/transcribe',blob,blob.type);
        const result=await response.json();
        if(this.closed || this.generation!==generation)return;
        this.options.onTranscript(result.text);
        this.message='Transcript added. Review it, then select Propose notes.';
      } catch(error) {
        if(this.closed || this.generation!==generation)return;
        this.message=error.name==='AbortError'?'Voice timed out. Please try again.':error.message;
      } finally {if(!this.closed&&this.generation===generation){this.state='idle';this.render();}}
    }
    async speak() {
      if(this.busy())return;
      const text=this.options.getReadback();
      if(!text.trim()){this.message='Type or record a debrief first, or propose notes to hear them read back.';this.render();return;}
      this.audio.pause();this.state='generating';this.message='Preparing spoken read-back…';this.render();
      const generation=this.generation=(this.generation||0)+1;
      try {
        const response=await this.post('/api/workspace/voice/speak',JSON.stringify({text}),'application/json');
        const blob=await response.blob();
        if(this.closed||this.generation!==generation)return;
        if(this.audioURL)URL.revokeObjectURL(this.audioURL);
        this.audioURL=URL.createObjectURL(blob);this.audio.src=this.audioURL;this.state='idle';this.message='Read-back ready. Use the player to pause or replay.';this.render();
        try{await this.audio.play();}catch{this.message='Read-back ready. Press Play below to listen.';this.render();}
      } catch(error) {
        if(this.closed||this.generation!==generation)return;
        this.state='idle';this.message=error.name==='AbortError'?'Voice timed out. Please try again.':error.message;this.render();
      }
    }
  }
  window.DebriefVoice={
    mount(options){if(active?.options.clientId===options.clientId&&!active.closed){active.options=options;active.render();}else{active?.close();active=new Voice(options);active.render();}},
    close(){active?.close();active=null;},
    busy(){return Boolean(active?.busy());}
  };
  window.addEventListener('pagehide',()=>window.DebriefVoice.close());
})();
