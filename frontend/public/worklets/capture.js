// AudioWorklet de captura: convierte Float32 a PCM16, calcula energía (VAD local) y emite frames de 20 ms.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const { threshold = 0.02, minSpeechMs = 100 } = options.processorOptions || {};
    this.threshold = threshold;
    this.minSpeechMs = minSpeechMs;
    this.speechMs = 0;
    this.speaking = false;
    this.buf = [];
    this.frame = Math.round(sampleRate * 0.02);
  }
  process(inputs) {
    const ch = inputs[0][0];
    if (!ch) return true;
    let sum = 0;
    for (let i = 0; i < ch.length; i++) sum += ch[i] * ch[i];
    const rms = Math.sqrt(sum / ch.length);
    const ms = (ch.length / sampleRate) * 1000;
    if (rms > this.threshold) {
      this.speechMs += ms;
      if (!this.speaking && this.speechMs >= this.minSpeechMs) {
        this.speaking = true;
        this.port.postMessage({ type: "speech_start" });
      }
    } else {
      if (this.speaking && rms < this.threshold * 0.5) this.speaking = false;
      if (!this.speaking) this.speechMs = 0;
    }
    for (let i = 0; i < ch.length; i++) this.buf.push(ch[i]);
    while (this.buf.length >= this.frame) {
      const chunk = this.buf.splice(0, this.frame);
      const pcm = new Int16Array(chunk.length);
      for (let i = 0; i < chunk.length; i++) {
        const s = Math.max(-1, Math.min(1, chunk[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage({ type: "pcm", pcm: pcm.buffer }, [pcm.buffer]);
    }
    return true;
  }
}
registerProcessor("capture", CaptureProcessor);
