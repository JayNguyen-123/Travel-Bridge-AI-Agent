// server/static/app.js
//
// Minimal browser client for the /ws/{user_id} voice endpoint in server/main.py.
// Sends/receives the same {"mime_type": "text/plain"|"audio/pcm", "data": ...}
// JSON frame shape the server expects (mirrored from Google's official ADK
// streaming example, adapted from SSE+POST to a single WebSocket).
//
// Audio: captures the mic at 16kHz mono PCM16 (Gemini Live's expected input
// format) using a ScriptProcessorNode (simple and broadly supported; an
// AudioWorklet is the non-deprecated upgrade path for a real production
// build). Playback assumes 24kHz mono PCM16 chunks coming back, queued
// gapless onto a single AudioContext.

const statusEl = document.getElementById("status");
const connectBtn = document.getElementById("connectBtn");
const micBtn = document.getElementById("micBtn");
const transcriptEl = document.getElementById("transcript");
const textForm = document.getElementById("textForm");
const textInput = document.getElementById("textInput");
const sendBtn = document.getElementById("sendBtn");

const USER_ID = "web-" + Math.random().toString(36).slice(2, 10);
const INPUT_SAMPLE_RATE = 16000;
const OUTPUT_SAMPLE_RATE = 24000;

let ws = null;
let micStream = null;
let micContext = null;
let micProcessor = null;
let micSource = null;
let isMicOn = false;

let playbackContext = null;
let nextPlaybackTime = 0;

function setStatus(text) {
  statusEl.textContent = text;
}

function appendLine(who, text) {
  const div = document.createElement("div");
  div.className = "line " + who;
  div.textContent = (who === "agent" ? "Agent: " : "You: ") + text;
  transcriptEl.appendChild(div);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

// ---------------------------------------------------------------------------
// WebSocket connection
// ---------------------------------------------------------------------------

function connect(isAudio) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws/${USER_ID}?is_audio=${isAudio}`;
  ws = new WebSocket(url);

  ws.onopen = () => {
    setStatus("Connected (" + (isAudio ? "audio" : "text") + " mode).");
    connectBtn.textContent = "Disconnect";
    micBtn.disabled = false;
    textInput.disabled = false;
    sendBtn.disabled = false;
  };

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.turn_complete !== undefined || msg.interrupted !== undefined) {
      if (msg.interrupted) stopPlayback();
      return;
    }
    if (msg.mime_type === "text/plain") {
      appendLine("agent", msg.data);
    } else if (msg.mime_type === "audio/pcm") {
      playPcm16Base64(msg.data);
    }
  };

  ws.onclose = () => {
    setStatus("Disconnected.");
    connectBtn.textContent = "Connect";
    micBtn.disabled = true;
    micBtn.classList.remove("active");
    micBtn.textContent = "🎙 Start mic";
    textInput.disabled = true;
    sendBtn.disabled = true;
    stopMic();
  };

  ws.onerror = () => setStatus("Connection error -- see browser console.");
}

connectBtn.addEventListener("click", () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.close();
  } else {
    setStatus("Connecting...");
    connect(false); // start in text mode; mic button upgrades to audio mode
  }
});

textForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = textInput.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ mime_type: "text/plain", data: text }));
  appendLine("user", text);
  textInput.value = "";
});

// ---------------------------------------------------------------------------
// Microphone capture -> 16kHz mono PCM16 -> base64 -> websocket
// ---------------------------------------------------------------------------

micBtn.addEventListener("click", async () => {
  if (isMicOn) {
    stopMic();
    return;
  }
  // Reconnect in audio mode so the server sets response_modalities=["AUDIO"].
  if (ws) ws.close();
  setStatus("Connecting (audio mode)...");
  connect(true);
  await new Promise((resolve) => {
    const check = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        clearInterval(check);
        resolve();
      }
    }, 50);
  });
  await startMic();
});

async function startMic() {
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    setStatus("Microphone permission denied: " + err.message);
    return;
  }

  micContext = new (window.AudioContext || window.webkitAudioContext)();
  micSource = micContext.createMediaStreamSource(micStream);
  // ScriptProcessorNode is deprecated but simple and broadly supported;
  // swap for an AudioWorkletNode in a real production build.
  micProcessor = micContext.createScriptProcessor(4096, 1, 1);

  micProcessor.onaudioprocess = (event) => {
    const input = event.inputBuffer.getChannelData(0);
    const downsampled = downsampleTo16k(input, micContext.sampleRate);
    const pcm16 = floatTo16BitPCM(downsampled);
    const base64 = arrayBufferToBase64(pcm16.buffer);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ mime_type: "audio/pcm", data: base64 }));
    }
  };

  micSource.connect(micProcessor);
  micProcessor.connect(micContext.destination);

  isMicOn = true;
  micBtn.classList.add("active");
  micBtn.textContent = "🎙 Stop mic";
  setStatus("Listening...");
}

function stopMic() {
  isMicOn = false;
  micBtn.classList.remove("active");
  micBtn.textContent = "🎙 Start mic";
  if (micProcessor) { micProcessor.disconnect(); micProcessor = null; }
  if (micSource) { micSource.disconnect(); micSource = null; }
  if (micContext) { micContext.close(); micContext = null; }
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
}

function downsampleTo16k(buffer, inputSampleRate) {
  if (inputSampleRate === INPUT_SAMPLE_RATE) return buffer;
  const ratio = inputSampleRate / INPUT_SAMPLE_RATE;
  const newLength = Math.round(buffer.length / ratio);
  const result = new Float32Array(newLength);
  for (let i = 0; i < newLength; i++) {
    result[i] = buffer[Math.floor(i * ratio)];
  }
  return result;
}

function floatTo16BitPCM(floatSamples) {
  const out = new Int16Array(floatSamples.length);
  for (let i = 0; i < floatSamples.length; i++) {
    const s = Math.max(-1, Math.min(1, floatSamples[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function arrayBufferToBase64(buffer) {
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
  return window.btoa(binary);
}

// ---------------------------------------------------------------------------
// Playback: base64 PCM16 (24kHz mono) -> gapless AudioBufferSourceNode queue
// ---------------------------------------------------------------------------

function playPcm16Base64(base64) {
  if (!playbackContext) {
    playbackContext = new (window.AudioContext || window.webkitAudioContext)();
    nextPlaybackTime = playbackContext.currentTime;
  }
  const binary = window.atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  const int16 = new Int16Array(bytes.buffer);

  const audioBuffer = playbackContext.createBuffer(1, int16.length, OUTPUT_SAMPLE_RATE);
  const channel = audioBuffer.getChannelData(0);
  for (let i = 0; i < int16.length; i++) channel[i] = int16[i] / 0x8000;

  const source = playbackContext.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(playbackContext.destination);

  const startAt = Math.max(nextPlaybackTime, playbackContext.currentTime);
  source.start(startAt);
  nextPlaybackTime = startAt + audioBuffer.duration;
}

function stopPlayback() {
  if (playbackContext) {
    nextPlaybackTime = playbackContext.currentTime;
  }
}
