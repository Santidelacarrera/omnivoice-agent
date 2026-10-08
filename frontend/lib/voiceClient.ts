export type AgentState = "idle" | "connecting" | "listening" | "processing" | "responding" | "interrupted" | "error";

export interface VoiceEvents {
  onState: (s: AgentState) => void;
  onTranscript: (who: "user" | "agent", text: string) => void;
  onMetrics: (m: { firstAudioMs?: number; bargeInMs?: number }) => void;
  onTool: (name: string, phase: "start" | "end", ok?: boolean) => void;
  onLevel: (rms: number) => void;
  onError?: (message: string) => void;
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

  constructor(private token: string, private ev: VoiceEvents, private agentId?: string) {}

  async start() {
    this.ev.onState("connecting");
    const auth = { Authorization: `Bearer ${this.token}`, "Content-Type": "application/json" };
    // 1) crear sesión  2) pedir conexión: el navegador solo recibe un ticket WS de 30 s y un solo uso;
    // ni el JWT viaja en la URL ni la API key del proveedor sale del backend.
    const created = await fetch(`${API}/api/v1/sessions`, {
      method: "POST", headers: auth, body: JSON.stringify(this.agentId ? { agent_id: this.agentId } : {}),
    });
    if (!created.ok) throw new Error(`No se pudo crear la sesión (${created.status})`);
    const { session_id } = await created.json();
    const conn = await fetch(`${API}/api/v1/sessions/${session_id}/connect`, { method: "POST", headers: auth });
    if (!conn.ok) throw new Error(`No se pudo preparar la conexión (${conn.status})`);
    const { ws_path } = await conn.json();

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
    // Pausa inmediata (el usuario oye silencio en ms) y pista al servidor. El servidor confirma con su VAD de
    // modelo ("audio.clear") o lo descarta como ruido ("audio.resume"); así el ruido de fondo no corta al agente.
    this.bargeAt = performance.now();
    this.playback?.port.postMessage({ type: "pause" });
    this.ws?.send(JSON.stringify({ type: "barge_in" }));
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
      case "audio.clear":
        this.agentSpeaking = false;
        this.playback?.port.postMessage({ type: "clear" });
        this.ev.onState("interrupted");
        break;
      case "audio.resume": this.bargeAt = 0; this.playback?.port.postMessage({ type: "resume" }); break;
      case "transcript_user": this.ev.onTranscript("user", msg.text); break;
      case "transcript_agent": this.ev.onTranscript("agent", msg.text); break;
      case "metrics": this.ev.onMetrics({ firstAudioMs: msg.first_audio_ms }); break;
      case "tool.start": this.ev.onTool(msg.name, "start"); this.ev.onState("processing"); break;
      case "tool.end": this.ev.onTool(msg.name, "end", msg.ok); break;
      case "state": this.agentSpeaking = false; this.ev.onState("listening"); break;
      case "error":
        // El servidor avisa de un fallo (p. ej. el proveedor sin crédito): se libera el micrófono y se informa.
        this.ev.onError?.(String(msg.message ?? "error"));
        void this.shutdown().then(() => this.ev.onState("error"));
        break;
    }
  }

  private async shutdown() {
    if (this.ws) this.ws.onclose = null;
    try { this.ws?.send(JSON.stringify({ type: "end" })); } catch { /* ya cerrado */ }
    this.ws?.close();
    this.stream?.getTracks().forEach((t) => t.stop());
    try { await this.ctx?.close(); } catch { /* ya cerrado */ }
  }

  async stop() {
    await this.shutdown();
    this.ev.onState("idle");
  }
}
