// AudioWorklet de reproducción con cola. "clear" vacía el buffer al instante (barge-in local).
class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0;
    this.paused = false;
    this.port.onmessage = (e) => {
      if (e.data.type === "push") this.queue.push(new Int16Array(e.data.pcm));
      else if (e.data.type === "clear") {
        this.queue = [];
        this.offset = 0;
        this.paused = false;
        this.port.postMessage({ type: "silenced" });
      } else if (e.data.type === "pause") {
        // El VAD del navegador oyó algo: se silencia ya, sin descartar, hasta que el servidor confirme o rechace.
        this.paused = true;
        this.port.postMessage({ type: "silenced" });
      } else if (e.data.type === "resume") this.paused = false;
    };
  }
  process(_i, outputs) {
    const out = outputs[0][0];
    let n = 0;
    while (!this.paused && n < out.length && this.queue.length) {
      const cur = this.queue[0];
      while (n < out.length && this.offset < cur.length) out[n++] = cur[this.offset++] / 32768;
      if (this.offset >= cur.length) {
        this.queue.shift();
        this.offset = 0;
      }
    }
    for (; n < out.length; n++) out[n] = 0;
    return true;
  }
}
registerProcessor("playback", PlaybackProcessor);
