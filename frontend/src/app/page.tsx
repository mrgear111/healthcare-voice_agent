"use client";

import { useState, useEffect, useRef } from 'react';

// --- Components ---

const GlassCard = ({ children, className = "", title }: { children: React.ReactNode, className?: string, title?: string }) => (
  <div className={`glass-panel p-6 ${className}`}>
    {title && <h3 className="text-sm font-semibold uppercase tracking-wider text-slate-400 mb-4">{title}</h3>}
    {children}
  </div>
);

const Waveform = ({ analyzer }: { analyzer: AnalyserNode | null }) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rafRef = useRef<number>(0);

  useEffect(() => {
    if (!analyzer || !canvasRef.current) return;

    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d')!;
    const bufferLength = analyzer.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);

    const draw = () => {
      rafRef.current = requestAnimationFrame(draw);
      analyzer.getByteFrequencyData(dataArray);

      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const barWidth = (canvas.width / bufferLength) * 2.5;
      let x = 0;

      for (let i = 0; i < bufferLength; i++) {
        const barHeight = (dataArray[i] / 255) * canvas.height;
        ctx.fillStyle = `rgba(99, 102, 241, ${0.4 + (dataArray[i] / 512)})`;
        ctx.fillRect(x, canvas.height - barHeight, barWidth, barHeight);
        x += barWidth + 1;
      }
    };

    draw();
    return () => cancelAnimationFrame(rafRef.current);
  }, [analyzer]);

  return <canvas ref={canvasRef} className="w-full h-32 opacity-60" width={400} height={128} />;
};

// --- Main Page ---

export default function Home() {
  const [isRecording, setIsRecording] = useState(false);
  const [agentResponse, setAgentResponse] = useState("");
  const [status, setStatus] = useState<"idle" | "listening" | "thinking" | "speaking">("idle");
  const [language, setLanguage] = useState<"en" | "hi" | "ta">("en");
  const [reasoningTrace, setReasoningTrace] = useState<string[]>([]);

  const socketRef = useRef<WebSocket | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const analyzerRef = useRef<AnalyserNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const workletNodeRef = useRef<AudioWorkletNode | null>(null);
  const audioQueue = useRef<Int16Array[]>([]);
  const isPlaying = useRef(false);
  const nextStartTime = useRef<number>(0);
  const [sessionId] = useState(() => `sess_${Math.random().toString(36).substring(7)}`);

  // ──────────────────────────────────────────────────────────
  // GAPLESS AUDIO ENGINE
  // Key fixes:
  //   1. AudioContext runs at device native rate (no browser resampling)
  //   2. We manually downsample from 16kHz → native in JS (linear interp)
  //   3. All chunks are pre-scheduled using Web Audio clock (no onended gaps)
  //   4. 80ms pre-roll buffer absorbs network jitter
  // ──────────────────────────────────────────────────────────

  const TTS_SAMPLE_RATE = 16000; // Deepgram Aura always outputs 16kHz

  const getOrCreateAudioContext = (): AudioContext => {
    if (!audioContextRef.current || audioContextRef.current.state === 'closed') {
      // Use device native sample rate to avoid browser resampling artifacts
      audioContextRef.current = new (window.AudioContext || (window as any).webkitAudioContext)();
      const analyser = audioContextRef.current.createAnalyser();
      analyser.fftSize = 512;
      analyser.connect(audioContextRef.current.destination);
      analyzerRef.current = analyser;
      nextStartTime.current = 0;
    }
    return audioContextRef.current;
  };

  const scheduleChunk = (chunk: Int16Array) => {
    const ctx = getOrCreateAudioContext();
    const nativeSR = ctx.sampleRate;

    // Convert Int16 PCM → Float32
    const float32 = new Float32Array(chunk.length);
    for (let i = 0; i < chunk.length; i++) {
      float32[i] = chunk[i] / 32768.0;
    }

    // Resample 16kHz → native sample rate using linear interpolation
    let samples: Float32Array;
    if (nativeSR !== TTS_SAMPLE_RATE) {
      const ratio = TTS_SAMPLE_RATE / nativeSR; // e.g. 16000/48000 = 0.333
      const outLen = Math.ceil(float32.length / ratio);
      samples = new Float32Array(outLen);
      for (let i = 0; i < outLen; i++) {
        const srcIdx = i * ratio;
        const lo = Math.floor(srcIdx);
        const hi = Math.min(lo + 1, float32.length - 1);
        const t = srcIdx - lo;
        samples[i] = float32[lo] * (1 - t) + float32[hi] * t;
      }
    } else {
      samples = float32;
    }

    const buffer = ctx.createBuffer(1, samples.length, nativeSR);
    buffer.getChannelData(0).set(samples);

    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(analyzerRef.current!);

    const now = ctx.currentTime;
    // Apply pre-roll: if clock has overtaken our schedule, anchor from now + 80ms
    if (nextStartTime.current < now + 0.01) {
      nextStartTime.current = now + 0.08;
    }

    source.start(nextStartTime.current);
    nextStartTime.current += buffer.duration;

    source.onended = () => {
      // Only mark idle when queue is drained AND scheduled audio has finished
      if (audioQueue.current.length === 0 && ctx.currentTime >= nextStartTime.current - 0.05) {
        isPlaying.current = false;
        setStatus("idle");
      }
    };
  };

  const drainQueue = () => {
    // Schedule ALL pending chunks immediately — Web Audio clock handles timing
    while (audioQueue.current.length > 0) {
      scheduleChunk(audioQueue.current.shift()!);
    }
  };

  // ──────────────────────────────────────────────────────────
  // RECORDING & WEBSOCKET
  // ──────────────────────────────────────────────────────────

  const startRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      streamRef.current = stream;

      const ctx = getOrCreateAudioContext();
      if (ctx.state === 'suspended') await ctx.resume();
      console.log('[Audio] Context running, sampleRate:', ctx.sampleRate);

      await ctx.audioWorklet.addModule('/worklets/audio-processor.js');
      console.log('[Audio] Worklet loaded');

      const micSource = ctx.createMediaStreamSource(stream);
      const workletNode = new AudioWorkletNode(ctx, 'audio-processor');
      workletNodeRef.current = workletNode;

      // Connect through silent GainNode to destination so AudioContext clock stays alive
      const silentGain = ctx.createGain();
      silentGain.gain.value = 0;
      micSource.connect(workletNode);
      workletNode.connect(silentGain);
      silentGain.connect(ctx.destination);

      workletNode.port.onmessage = (event) => {
        const inputData: Float32Array = event.data;
        const pcmData = new Int16Array(inputData.length);
        for (let i = 0; i < inputData.length; i++) {
          pcmData[i] = Math.max(-32768, Math.min(32767, Math.round(inputData[i] * 32767)));
        }
        if (socketRef.current?.readyState === WebSocket.OPEN) {
          socketRef.current.send(pcmData.buffer);
        }
      };

      const wsUrl = `ws://localhost:8000/ws/voice?session_id=${sessionId}&language=${language}&sample_rate=${ctx.sampleRate}`;
      console.log('[WS] Connecting to:', wsUrl);
      const socket = new WebSocket(wsUrl);
      socketRef.current = socket;

      socket.onopen = () => {
        console.log('[WS] Connection opened');
        setIsRecording(true);
        setStatus("listening");
      };

      socket.onmessage = async (event) => {
        if (typeof event.data === "string") {
          try {
            const data = JSON.parse(event.data);
            console.log('[WS] Text msg:', data.type);
            if (data.type === "llm_text") {
              setAgentResponse(prev => prev + data.content);
              setStatus("thinking");
            } else if (data.type === "stt_text") {
              setAgentResponse(`You: ${data.content}${data.is_final ? "" : "..."}`);
              if (data.is_final) setStatus("thinking");
            } else if (data.type === "reasoning") {
              setReasoningTrace(prev => [...prev.slice(-4), data.content]);
            }
          } catch (_) { /* not JSON */ }
        } else {
          // Binary audio data — push to queue and drain
          const arrayBuffer = await event.data.arrayBuffer();
          const int16Array = new Int16Array(arrayBuffer);
          console.log('[Audio] TTS chunk received:', int16Array.length, 'samples');
          audioQueue.current.push(int16Array);

          // Resume context (browser may suspend on tab switch or first play)
          const ctx = getOrCreateAudioContext();
          if (ctx.state === 'suspended') await ctx.resume();

          if (!isPlaying.current) {
            isPlaying.current = true;
            setStatus("speaking");
          }
          drainQueue();
        }
      };

      socket.onclose = (e) => {
        console.log('[WS] Closed:', e.code, e.reason || '');
        // Full cleanup so clicking the orb again starts fresh
        audioQueue.current = [];
        isPlaying.current = false;
        nextStartTime.current = 0;
        streamRef.current?.getTracks().forEach(track => track.stop());
        streamRef.current = null;
        workletNodeRef.current?.disconnect();
        workletNodeRef.current = null;
        socketRef.current = null;
        setIsRecording(false);
        setStatus("idle");
        if (e.code === 1012 || e.code === 1006) {
          // Service restart or abnormal close — notify user
          setAgentResponse("Connection lost (server restarted). Click the orb to reconnect.");
        }
      };

      socket.onerror = (err) => {
        console.error('[WS] Error:', err);
      };

      setAgentResponse("");
    } catch (err) {
      console.error("Error accessing microphone:", err);
    }
  };

  const stopRecording = () => {
    setIsRecording(false);
    setStatus("idle");
    audioQueue.current = [];
    isPlaying.current = false;
    streamRef.current?.getTracks().forEach(track => track.stop());
    workletNodeRef.current?.disconnect();
    workletNodeRef.current = null;
    socketRef.current?.close();
    socketRef.current = null;
  };

  const toggleRecording = () => {
    if (isRecording) stopRecording();
    else startRecording();
  };

  return (
    <main className="min-h-screen bg-[#020617] text-slate-100 p-6 md:p-12 overflow-hidden relative font-sans">
      <div className="absolute top-0 left-0 w-full h-full bg-[radial-gradient(circle_at_50%_0%,rgba(99,102,241,0.1),transparent_50%)] pointer-events-none" />
      
      {/* Header */}
      <header className="flex justify-between items-center mb-12 relative z-10">
        <div>
          <h1 className="text-4xl font-bold tracking-tight bg-gradient-to-r from-white to-slate-400 bg-clip-text text-transparent">
            Aura <span className="text-indigo-500">Pro</span>
          </h1>
          <p className="text-slate-500 text-sm font-medium tracking-widest uppercase mt-1">Clinical AI Network</p>
        </div>
        <div className="flex items-center gap-4">
          <div className="flex bg-slate-900/50 p-1 rounded-full border border-slate-800">
            {(['en', 'hi', 'ta'] as const).map(l => (
              <button 
                key={l}
                onClick={() => setLanguage(l)}
                className={`px-4 py-1.5 rounded-full text-xs font-bold transition-all ${language === l ? 'bg-indigo-600 text-white shadow-lg' : 'text-slate-500 hover:text-slate-300'}`}
              >
                {l.toUpperCase()}
              </button>
            ))}
          </div>
          <div className="px-4 py-1.5 glass-panel rounded-full flex items-center gap-2">
            <span className={`status-indicator ${isRecording ? 'status-online' : 'bg-slate-700'}`} />
            <span className="text-[10px] font-bold uppercase tracking-tighter text-slate-400">{isRecording ? 'System Online' : 'Standby'}</span>
          </div>
        </div>
      </header>

      {/* Main Dashboard */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 relative z-10">
        
        {/* Left Sidebar: Session Info */}
        <div className="lg:col-span-3 space-y-6">
          <GlassCard title="Active Session">
            <div className="space-y-4">
              <div className="p-3 bg-white/5 rounded-xl border border-white/5">
                <p className="text-[10px] text-slate-500 uppercase font-bold mb-1">Session ID</p>
                <p className="text-xs font-mono text-indigo-300 truncate">{sessionId}</p>
              </div>
              <div className="p-3 bg-white/5 rounded-xl border border-white/5">
                <p className="text-[10px] text-slate-500 uppercase font-bold mb-1">Latency Target</p>
                <div className="flex justify-between items-end">
                  <p className="text-sm font-bold text-slate-200">~320ms</p>
                  <p className="text-[10px] text-emerald-400 font-bold tracking-tighter">OPTIMAL</p>
                </div>
              </div>
              <div className="p-3 bg-white/5 rounded-xl border border-white/5">
                <p className="text-[10px] text-slate-500 uppercase font-bold mb-1">Status</p>
                <p className={`text-xs font-bold capitalize ${
                  status === 'listening' ? 'text-emerald-400' :
                  status === 'thinking' ? 'text-yellow-400' :
                  status === 'speaking' ? 'text-indigo-400' :
                  'text-slate-500'
                }`}>{status}</p>
              </div>
            </div>
          </GlassCard>

          <GlassCard title="System Reasoning" className="h-[260px] flex flex-col">
            <div className="flex-1 space-y-3 overflow-y-auto pr-2" style={{ scrollbarWidth: 'thin', scrollbarColor: 'rgba(99,102,241,0.2) transparent' }}>
              {reasoningTrace.map((trace, i) => (
                <div key={i} className="text-[10px] font-mono text-indigo-400/80 leading-relaxed border-l-2 border-indigo-500/30 pl-3 py-1">
                  {trace}
                </div>
              ))}
              {reasoningTrace.length === 0 && <p className="text-slate-600 text-[10px] italic">Awaiting cognitive signals...</p>}
            </div>
          </GlassCard>
        </div>

        {/* Center: Core Voice Node */}
        <div className="lg:col-span-6 flex flex-col items-center">
          <div className="relative group cursor-pointer" onClick={toggleRecording}>
            {/* Outer Glow */}
            <div className={`absolute inset-0 bg-indigo-500/20 blur-[100px] rounded-full transition-all duration-1000 ${isRecording ? 'opacity-100 scale-125' : 'opacity-0 scale-50'}`} />
            
            {/* The Orb */}
            <div className={`w-64 h-64 rounded-full glass-panel flex flex-col items-center justify-center relative z-20 transition-all duration-700 border-2 ${
              isRecording ? 'border-indigo-400/50 shadow-[0_0_100px_rgba(99,102,241,0.3)]' : 'border-slate-800 hover:border-slate-700'
            }`}>
              {isRecording ? (
                <div className="space-y-4 flex flex-col items-center">
                  <div className="flex gap-2 items-end h-10">
                    {[...Array(5)].map((_, i) => (
                      <div 
                        key={i} 
                        className={`w-1.5 rounded-full bg-indigo-400 animate-pulse`}
                        style={{ 
                          height: `${20 + Math.random() * 20}px`,
                          animationDelay: `${i * 0.12}s`,
                          animationDuration: '0.8s'
                        }}
                      />
                    ))}
                  </div>
                  <span className={`text-[10px] font-bold tracking-[0.3em] uppercase animate-pulse ${
                    status === 'listening' ? 'text-emerald-400' :
                    status === 'thinking' ? 'text-yellow-400' :
                    'text-indigo-300'
                  }`}>{status}</span>
                </div>
              ) : (
                <div className="flex flex-col items-center gap-3">
                  <svg className="w-16 h-16 text-slate-700 transition-colors group-hover:text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1} d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m8 0h-4m4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" />
                  </svg>
                  <span className="text-slate-600 text-[10px] font-bold tracking-widest uppercase opacity-0 group-hover:opacity-100 transition-opacity">Tap to initialize</span>
                </div>
              )}
            </div>
          </div>

          <div className="mt-12 w-full max-w-lg">
            <Waveform analyzer={analyzerRef.current} />
          </div>

          {/* Transcript Box */}
          <div className="mt-8 w-full glass-panel p-8 min-h-[140px] max-h-[200px] overflow-y-auto flex items-center justify-center" style={{ scrollbarWidth: 'thin', scrollbarColor: 'rgba(99,102,241,0.2) transparent' }}>
            {agentResponse ? (
              <p className="text-lg font-light text-slate-200 leading-relaxed text-center italic">
                &ldquo;{agentResponse}&rdquo;
              </p>
            ) : (
              <p className="text-slate-600 font-light italic text-sm">Waiting for audio input...</p>
            )}
          </div>
        </div>

        {/* Right Sidebar: Clinical Stats/Actions */}
        <div className="lg:col-span-3 space-y-6">
          <GlassCard title="Real-time Analytics">
            <div className="space-y-6">
              <div>
                <div className="flex justify-between items-center mb-2">
                  <span className="text-[10px] text-slate-500 font-bold uppercase">Voice Confidence</span>
                  <span className="text-xs text-indigo-400 font-bold">98.4%</span>
                </div>
                <div className="w-full bg-slate-900 rounded-full h-1">
                  <div className="bg-indigo-500 h-1 rounded-full w-[98.4%] shadow-[0_0_10px_rgba(99,102,241,0.5)]"></div>
                </div>
              </div>

              <div>
                <div className="flex justify-between items-center mb-2">
                  <span className="text-[10px] text-slate-500 font-bold uppercase">STT Precision</span>
                  <span className="text-xs text-cyan-400 font-bold">99.1%</span>
                </div>
                <div className="w-full bg-slate-900 rounded-full h-1">
                  <div className="bg-cyan-400 h-1 rounded-full w-[99.1%] shadow-[0_0_10px_rgba(34,211,238,0.5)]"></div>
                </div>
              </div>

              <div>
                <div className="flex justify-between items-center mb-2">
                  <span className="text-[10px] text-slate-500 font-bold uppercase">Audio Quality</span>
                  <span className="text-xs text-emerald-400 font-bold">Gapless</span>
                </div>
                <div className="w-full bg-slate-900 rounded-full h-1">
                  <div className="bg-emerald-400 h-1 rounded-full w-full shadow-[0_0_10px_rgba(16,185,129,0.5)]"></div>
                </div>
              </div>
            </div>
          </GlassCard>

          <GlassCard title="Quick Controls">
            <div className="grid grid-cols-1 gap-3">
              <button
                onClick={() => { setAgentResponse(""); setReasoningTrace([]); }}
                className="flex items-center justify-between p-3 rounded-xl clinical-card text-[10px] font-bold text-slate-400 hover:text-white group"
              >
                <span>CLEAR HISTORY</span>
                <svg className="w-4 h-4 group-hover:text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" /></svg>
              </button>
            </div>
          </GlassCard>
        </div>

      </div>
    </main>
  );
}
