import { getPHFModule } from './phf-view.js?v=1.0.2';

// Protocol Packet IDs
const PKT_VIDEO = 0x01;
const PKT_AUDIO = 0x02;
const PKT_JSON  = 0x03;

let ws = null;
let phfMod = null;
let wsPort = 8765;

// Elements
const canvas = document.getElementById('videoCanvas');
const ctx = canvas.getContext('2d');
const loadingOverlay = document.getElementById('loadingOverlay');
const loadingText = document.getElementById('loadingText');
const connStatus = document.getElementById('connStatus');

const hudFps = document.getElementById('hudFps');
const hudLatency = document.getElementById('hudLatency');
const hudBitrate = document.getElementById('hudBitrate');
const hudDecodeTime = document.getElementById('hudDecodeTime');
const hudRatio = document.getElementById('hudRatio');

const videoSelect = document.getElementById('videoSelect');
const btnRefreshVideos = document.getElementById('btnRefreshVideos');
const noVideosAlert = document.getElementById('noVideosAlert');
const videoMetaBox = document.getElementById('videoMetaBox');

const metaRes = document.getElementById('metaRes');
const metaFps = document.getElementById('metaFps');
const metaDuration = document.getElementById('metaDuration');
const metaAudio = document.getElementById('metaAudio');
const metaSize = document.getElementById('metaSize');

const btnToggleAudio = document.getElementById('btnToggleAudio');
const btnAudioText = document.getElementById('btnAudioText');
const volSlider = document.getElementById('volSlider');
const volVal = document.getElementById('volVal');
const vuBar = document.getElementById('vuBar');

const btnPlayPause = document.getElementById('btnPlayPause');
const playIcon = document.getElementById('playIcon');
const btnLoop = document.getElementById('btnLoop');
const timelineSlider = document.getElementById('timelineSlider');
const currentTime = document.getElementById('currentTime');
const totalDuration = document.getElementById('totalDuration');

const alertBanner = document.getElementById('alertBanner');
const alertMsg = document.getElementById('alertMsg');
const btnCloseAlert = document.getElementById('btnCloseAlert');

// Audio Context (Web Audio API)
let audioCtx = null;
let gainNode = null;
let nextPlayTime = 0;
let isAudioEnabled = false;

// State
let isPlaying = true;
let isLooping = true;
let isUserSeeking = false;
let currentVideos = [];
let currentVideoInfo = null;

// Metrics
let frameCounter = 0;
let bytesCounter = 0;
let lastFpsTime = performance.now();

// WASM Memory buffers
let wasmSrcPtr = 0;
let wasmSrcCapacity = 0;
let wasmDstPtr = 0;
let wasmDstCapacity = 0;
let cachedImgData = null;

function setConnectionState(state, text) {
    const dot = connStatus.querySelector('.dot');
    const label = connStatus.querySelector('.text');
    dot.className = `dot ${state}`;
    label.innerText = text;
}

function formatTime(seconds) {
    if (!seconds || isNaN(seconds) || seconds < 0) return '00:00';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}

// ---------------------------------------------------------------------------
// Audio Pipeline (Web Audio API 16-bit PCM streaming)
// ---------------------------------------------------------------------------
function initAudio() {
    if (!audioCtx) {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        audioCtx = new AudioContext({ sampleRate: 44100 });
        gainNode = audioCtx.createGain();
        gainNode.gain.value = parseFloat(volSlider.value);
        gainNode.connect(audioCtx.destination);
    }
    if (audioCtx.state === 'suspended') {
        audioCtx.resume();
    }
}

volSlider.addEventListener('input', (e) => {
    const val = parseFloat(e.target.value);
    volVal.innerText = `${Math.round(val * 100)}%`;
    if (gainNode) {
        gainNode.gain.value = val;
    }
});

function playPcmChunk(u8Packet) {
    if (!isAudioEnabled || !audioCtx) return;

    // Header layout: [PKT_AUDIO (0x02), 0x00] guarantees 2-byte alignment
    const pcmBuf = u8Packet.buffer.slice(u8Packet.byteOffset + 2, u8Packet.byteOffset + u8Packet.byteLength);
    const int16 = new Int16Array(pcmBuf);
    const n = int16.length;
    if (n === 0) return;

    const float32 = new Float32Array(n);
    let sumSquares = 0;
    for (let i = 0; i < n; i++) {
        const val = int16[i] / 32768.0;
        float32[i] = val;
        sumSquares += val * val;
    }

    const rms = Math.sqrt(sumSquares / n);
    const vuPercent = Math.min(100, Math.round(rms * 500));
    vuBar.style.width = `${vuPercent}%`;

    const audioBuf = audioCtx.createBuffer(1, n, 44100);
    audioBuf.copyToChannel(float32, 0);

    const source = audioCtx.createBufferSource();
    source.buffer = audioBuf;
    source.connect(gainNode);

    const now = audioCtx.currentTime;
    if (nextPlayTime < now || nextPlayTime > now + 0.25) {
        nextPlayTime = now + 0.02; // Small 20ms jitter buffer
    }
    source.start(nextPlayTime);
    nextPlayTime += audioBuf.duration;
}

// ---------------------------------------------------------------------------
// Video Frame Pipeline (WASM SIMD128 Decoder)
// ---------------------------------------------------------------------------
function handleVideoPacket(u8) {
    if (!phfMod) return;

    // Header layout (21 bytes):
    // [1B PKT_VIDEO] [8B capture_ts] [4B enc_ms] [4B pos_sec] [4B duration_sec] [PHF payload]
    const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
    const captureTsSec = dv.getFloat64(1, true);
    const encMs = dv.getFloat32(9, true);
    const posSec = dv.getFloat32(13, true);
    const durationSec = dv.getFloat32(17, true);

    const phfBytes = u8.subarray(21);
    const phfLen = phfBytes.byteLength;
    if (phfLen < 20) return;

    const width = dv.getUint32(21 + 8, true);
    const height = dv.getUint32(21 + 12, true);
    const channels = dv.getUint8(21 + 5);

    if (width === 0 || height === 0) return;

    if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
        cachedImgData = null;
    }

    if (wasmSrcCapacity < phfLen) {
        if (wasmSrcPtr) phfMod._free(wasmSrcPtr);
        wasmSrcCapacity = Math.max(phfLen * 2, 65536);
        wasmSrcPtr = phfMod._malloc(wasmSrcCapacity);
        if (!wasmSrcPtr) {
            wasmSrcCapacity = 0;
            console.error('[PHF] Out of WASM memory for input buffer');
            return;
        }
    }
    phfMod.HEAPU8.set(phfBytes, wasmSrcPtr);

    const outBytes = width * height * 4;
    if (wasmDstCapacity < outBytes) {
        if (wasmDstPtr) phfMod._free(wasmDstPtr);
        wasmDstCapacity = Math.max(outBytes * 2, 1048576);
        wasmDstPtr = phfMod._malloc(wasmDstCapacity);
        if (!wasmDstPtr) {
            wasmDstCapacity = 0;
            console.error('[PHF] Out of WASM memory for output buffer');
            return;
        }
    }

    const t0 = performance.now();
    const rc = phfMod._phf_decode_memory(wasmSrcPtr, phfLen, wasmDstPtr, width, height, 4);
    const decMs = performance.now() - t0;

    if (rc === 0) {
        if (!cachedImgData || cachedImgData.width !== width || cachedImgData.height !== height) {
            cachedImgData = ctx.createImageData(width, height);
        }
        cachedImgData.data.set(new Uint8Array(phfMod.HEAPU8.buffer, wasmDstPtr, outBytes));
        ctx.putImageData(cachedImgData, 0, 0);

        loadingOverlay.classList.add('hidden');

        // Dynamically update timeline slider & timer position on every decoded frame
        if (!isUserSeeking && durationSec > 0) {
            const percent = Math.min(100, Math.max(0, (posSec / durationSec) * 100));
            timelineSlider.value = percent;
            currentTime.innerText = formatTime(posSec);
            totalDuration.innerText = formatTime(durationSec);
        }

        const totalLatencyMs = (Date.now() / 1000.0 - captureTsSec) * 1000.0;
        frameCounter++;
        bytesCounter += phfLen;

        hudLatency.innerText = `${Math.max(0, Math.round(totalLatencyMs))} ms`;
        hudDecodeTime.innerText = `${decMs.toFixed(1)} ms`;

        const rawBytes = width * height * channels;
        const ratio = (rawBytes / phfLen).toFixed(2);
        const savings = ((1.0 - phfLen / rawBytes) * 100).toFixed(0);
        hudRatio.innerText = `${ratio}x (-${savings}%)`;
    } else {
        console.error(`[PHF] Decode failed: code ${rc}`);
    }
}

// ---------------------------------------------------------------------------
// WebSocket Networking
// ---------------------------------------------------------------------------
function connectWebSocket() {
    setConnectionState('connecting', 'Connecting...');
    const wsHost = window.location.hostname || 'localhost';
    const wsUrl = `ws://${wsHost}:${wsPort}`;

    ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
        setConnectionState('connected', 'Live Stream Active');
        loadingText.innerText = 'Receiving PHF video stream...';
    };

    ws.onmessage = (event) => {
        if (typeof event.data === 'string') {
            try {
                handleJsonMessage(JSON.parse(event.data));
            } catch (e) {
                console.error(e);
            }
            return;
        }

        const u8 = new Uint8Array(event.data);
        if (u8.length === 0) return;

        const pktType = u8[0];
        if (pktType === PKT_VIDEO) {
            handleVideoPacket(u8);
        } else if (pktType === PKT_AUDIO) {
            playPcmChunk(u8);
        } else if (pktType === PKT_JSON) {
            const jsonStr = new TextDecoder().decode(u8.subarray(1));
            try {
                handleJsonMessage(JSON.parse(jsonStr));
            } catch (e) {
                console.error(e);
            }
        }
    };

    ws.onclose = () => {
        setConnectionState('error', 'Disconnected');
        loadingOverlay.classList.remove('hidden');
        loadingText.innerText = 'Connection lost. Reconnecting in 2s...';
        setTimeout(connectWebSocket, 2000);
    };

    ws.onerror = () => {
        setConnectionState('error', 'Connection Error');
    };
}

function handleJsonMessage(msg) {
    if (msg.type === 'video_list') {
        currentVideos = msg.videos || [];
        videoSelect.innerHTML = '';

        if (currentVideos.length > 0) {
            noVideosAlert.classList.add('hidden');
            currentVideos.forEach(v => {
                const opt = document.createElement('option');
                opt.value = v.id;
                opt.innerText = v.label || v.name;
                videoSelect.appendChild(opt);
            });

            if (msg.current_video && msg.current_video.video_id) {
                videoSelect.value = msg.current_video.video_id;
            } else {
                const def720 = currentVideos.find(v => v.id.toLowerCase().includes('720p') || v.height === 720);
                if (def720) {
                    videoSelect.value = def720.id;
                }
            }
            updateVideoMetadata(videoSelect.value);
        } else {
            noVideosAlert.classList.remove('hidden');
            videoSelect.innerHTML = '<option value="">No videos found in videos/ directory</option>';
            loadingText.innerText = 'Pasta videos/ vazia. Adicione arquivos de vídeo.';
            clearMetadata();
        }
    } else if (msg.type === 'video_status') {
        if (!msg.success && msg.error) {
            alertMsg.innerText = `Erro ao abrir vídeo: ${msg.error}`;
            alertBanner.classList.remove('hidden');
        } else {
            alertBanner.classList.add('hidden');
        }

        if (msg.info) {
            currentVideoInfo = msg.info;
            isPlaying = !msg.info.paused;
            playIcon.innerText = isPlaying ? '⏸️' : '▶️';

            isLooping = !!msg.info.loop;
            if (isLooping) {
                btnLoop.classList.add('active');
            } else {
                btnLoop.classList.remove('active');
            }

            if (!isUserSeeking && msg.info.duration_sec > 0) {
                timelineSlider.value = msg.info.progress_percent || 0;
                const curSec = (msg.info.progress_percent / 100.0) * msg.info.duration_sec;
                currentTime.innerText = formatTime(curSec);
                totalDuration.innerText = formatTime(msg.info.duration_sec);
            }
        }
    } else if (msg.type === 'audio_status') {
        if (isAudioEnabled && !msg.enabled) {
            alertMsg.innerText = 'Este vídeo não possui faixa de áudio.';
            alertBanner.classList.remove('hidden');
            setAudioButton(false);
        }
    }
}

function updateVideoMetadata(videoId) {
    const v = currentVideos.find(x => x.id === videoId);
    if (v) {
        metaRes.innerText = `${v.width}x${v.height}`;
        metaFps.innerText = `${v.fps} FPS`;
        metaDuration.innerText = formatTime(v.duration_sec);
        metaAudio.innerText = v.has_audio ? 'Available (PCM 44.1kHz)' : 'No Audio';
        metaSize.innerText = `${v.size_mb} MB`;
    }
}

function clearMetadata() {
    metaRes.innerText = '--';
    metaFps.innerText = '--';
    metaDuration.innerText = '--';
    metaAudio.innerText = '--';
    metaSize.innerText = '--';
}

function setAudioButton(enabled) {
    isAudioEnabled = enabled;
    if (enabled) {
        btnToggleAudio.className = 'btn btn-toggle audio-on';
        btnToggleAudio.querySelector('.icon').innerText = '🔊';
        btnAudioText.innerText = 'Audio Streaming (Live)';
    } else {
        btnToggleAudio.className = 'btn btn-toggle audio-off';
        btnToggleAudio.querySelector('.icon').innerText = '🔇';
        btnAudioText.innerText = 'Enable Audio';
        vuBar.style.width = '0%';
    }
}

btnToggleAudio.addEventListener('click', async () => {
    initAudio();
    if (audioCtx && audioCtx.state === 'suspended') {
        try {
            await audioCtx.resume();
        } catch (e) {
            console.error('AudioContext resume failed:', e);
        }
    }
    setAudioButton(!isAudioEnabled);

    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
            action: 'toggle_audio',
            enabled: isAudioEnabled
        }));
    }
});

// ---------------------------------------------------------------------------
// Controls Interaction
// ---------------------------------------------------------------------------
videoSelect.addEventListener('change', () => {
    const videoId = videoSelect.value;
    if (!videoId || !ws || ws.readyState !== WebSocket.OPEN) return;

    updateVideoMetadata(videoId);
    loadingOverlay.classList.remove('hidden');
    loadingText.innerText = `Loading ${videoId}...`;

    ws.send(JSON.stringify({
        action: 'set_video',
        id: videoId
    }));
});

btnRefreshVideos.addEventListener('click', () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
        videoSelect.innerHTML = '<option value="">Scanning videos directory...</option>';
        ws.send(JSON.stringify({ action: 'get_videos', refresh: true }));
    }
});

btnPlayPause.addEventListener('click', () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const nextAction = isPlaying ? 'pause' : 'resume';
    ws.send(JSON.stringify({ action: nextAction }));
});

btnLoop.addEventListener('click', () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    isLooping = !isLooping;
    ws.send(JSON.stringify({ action: 'set_loop', loop: isLooping }));
});

timelineSlider.addEventListener('mousedown', () => { isUserSeeking = true; });
timelineSlider.addEventListener('touchstart', () => { isUserSeeking = true; });

timelineSlider.addEventListener('input', (e) => {
    if (currentVideoInfo && currentVideoInfo.duration_sec > 0) {
        const curSec = (parseFloat(e.target.value) / 100.0) * currentVideoInfo.duration_sec;
        currentTime.innerText = formatTime(curSec);
    }
});

timelineSlider.addEventListener('change', (e) => {
    isUserSeeking = false;
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
            action: 'seek_percent',
            percent: parseFloat(e.target.value)
        }));
    }
});

btnCloseAlert.addEventListener('click', () => {
    alertBanner.classList.add('hidden');
});

// Periodic FPS & Bitrate calculation
setInterval(() => {
    const now = performance.now();
    const dt = (now - lastFpsTime) / 1000.0;
    if (dt >= 1.0) {
        const fps = (frameCounter / dt).toFixed(1);
        const mbps = ((bytesCounter * 8) / (dt * 1000000)).toFixed(2);

        hudFps.innerText = fps;
        hudBitrate.innerText = `${mbps} Mbps`;

        frameCounter = 0;
        bytesCounter = 0;
        lastFpsTime = now;
    }
}, 1000);

// App initialization
(async () => {
    try {
        loadingText.innerText = 'Initializing WebAssembly SIMD128 decoder...';
        phfMod = await getPHFModule();
        try {
            const cfg = await (await fetch('config.json', { cache: 'no-store' })).json();
            if (Number.isInteger(cfg.ws_port)) wsPort = cfg.ws_port;
        } catch (e) {
            console.warn('No server config.json, using default WebSocket port', wsPort);
        }
        connectWebSocket();
    } catch (err) {
        console.error('Failed to initialize PHF WASM Module:', err);
        loadingText.innerText = 'Error loading WebAssembly engine: ' + err.message;
    }
})();
