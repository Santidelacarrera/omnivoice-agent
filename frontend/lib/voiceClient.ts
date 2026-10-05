export type AgentState = "idle" | "connecting" | "listening" | "processing" | "responding" | "interrupted" | "error";

export interface VoiceEvents {
  onState: (s: AgentState) => void;
  onTranscript: (who: "user" | "agent", text: string) => void;
  onMetrics: (m: { firstAudioMs?: number; bargeInMs?: number }) => void;
  onTool: (name: string, phase: "start" | "end", ok?: boolean) => void;
  onLevel: (rms: number) => void;
}

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const SR = 24000;

export class VoiceClient {
  private ws?: WebSocket;
  private ctx?: AudioContext;
  private capture?: AudioWorkletNode;
  private playback?: AudioWorkletNode;
  private stream?: MediaStream;
  private seq = 0;
  private bargeAt = 0;
  private agentSpeaking = false;
  muted = false;

  constructor(private token: string, private ev: VoiceEvents) {}

  async start() {
    this.ev.onState("connecting");
    // El navegador solo recibe un ticket de 30 s; la API key del proveedor nunca sale del backend.
    const r = await fetch(`${API}/api/v1/sessions`, { method: "POST", headers: { Authorization: `Bearer ${this.token}` } });
    if (!r.ok) throw new Error(`No se pudo crear la sesión (${r.status})`);
    const { ws_path } = await r.json();

    this.ctx = new AudioContext({ sampleRate: SR });
    await this.ctx.audioWorklet.addModule("/worklets/capture.js");
    await this.ctx.audioWorklet.addModule("/worklets/playback.js");
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.capture = new AudioWorkletNode(this.ctx, "capture", { processorOptions: { threshold: 0.02, minSpeechMs: 100 } });
    this.playback = new AudioWorkletNode(this.ctx, "playback");
    src.connect(this.capture);
    this.playback.connect(this.ctx.destination);

    this.playback.port.onmessage = (e) => {
      if (e.data.type === "silenced" && this.bargeAt) {
        this.ev.onMetrics({ bargeInMs: Math.round(performance.now() - this.bargeAt) });
        this.bargeAt = 0;
      }
    };

    this.ws = new WebSocket(API.replace(/^http/, "ws") + ws_path);
    this.ws.binaryType = "arraybuffer";
    this.ws.onmessage = (m) => this.onMessage(m);
    this.ws.onclose = () => this.ev.onState("idle");
    this.ws.onerror = () => this.ev.onState("error");

    this.capture.port.onmessage = (e) => {
      if (e.data.type === "speech_start") {
        // Corte local inmediato: no esperamos a la red para silenciar al agente.
        if (this.agentSpeaking) this.localBargeIn();
      } else if (e.data.type === "pcm" && !this.muted && this.ws?.readyState === WebSocket.OPEN) {
        const pcm = new Uint8Array(e.data.pcm);
        const frame = new Uint8Array(4 + pcm.length);
        new DataView(frame.buffer).setUint32(0, this.seq++ >>> 0, false);
        frame.set(pcm, 4);
        this.ws.send(frame);
        const s = new Int16Array(e.data.pcm);
        let sum = 0;
        for (let i = 0; i < s.length; i++) sum += (s[i] / 32768) ** 2;
        this.ev.onLevel(Math.sqrt(sum / s.length));
      }
    };
  }

  private localBargeIn() {
    this.bargeAt = performance.now();
    this.agentSpeaking = false;
    this.playback?.port.postMessage({ type: "clear" });
    this.ws?.send(JSON.stringify({ type: "barge_in" }));
    this.ev.onState("interrupted");
  }

  private onMessage(m: MessageEvent) {
    if (m.data instanceof ArrayBuffer) {
      this.agentSpeaking = true;
      this.ev.onState("responding");
      this.playback?.port.postMessage({ type: "push", pcm: m.data }, [m.data]);
      return;
    }
    const msg = JSON.parse(m.data);
    switch (msg.type) {
      case "session.ready": this.ev.onState("listening"); break;
      case "audio.clear": this.agentSpeaking = false; this.playback?.port.postMessage({ type: "clear" }); break;
      case "transcript_user": this.ev.onTranscript("user", msg.text); break;
      case "transcript_agent": this.ev.onTranscript("agent", msg.text); break;
      case "metrics": this.ev.onMetrics({ firstAudioMs: msg.first_audio_ms }); break;
      case "tool.start": this.ev.onTool(msg.name, "start"); this.ev.onState("processing"); break;
      case "tool.end": this.ev.onTool(msg.name, "end", msg.ok); break;
      case "state": this.agentSpeaking = false; this.ev.onState("listening"); break;
      case "error": this.ev.onState("error"); break;
    }
  }

  async stop() {
    this.ws?.send(JSON.stringify({ type: "end" }));
    this.ws?.close();
    this.stream?.getTracks().forEach((t) => t.stop());
    await this.ctx?.close();
    this.ev.onState("idle");
  }
}
