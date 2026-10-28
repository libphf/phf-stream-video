# PHF Video Stream — Lossless Video File Streaming

Low-latency video file streaming server & WebAssembly player powered by the **Pure High-Fidelity Format (PHF)**: every decoded frame is sent as a lossless RGB PHF image and reaches the browser bit-exact.

### What "lossless" means here

PHF is lossless **relative to the frames the server decodes**. If the source file is H.264/H.265
(like the bundled `.mp4` demos, which are 4:2:0), its compression artifacts and chroma
subsampling are already in those frames, and PHF delivers them faithfully. It does not restore
detail the source never had. The quality advantage only exists with lossless sources
(PNG/TIFF sequences, FFV1, ProRes 4444, uncompressed AVI…).

It also costs far more bandwidth than the source file. Measured on the bundled demos
(PHF ratio vs. raw RGB, stream bitrate at 30 fps vs. the MP4's own bitrate):

| Demo | PHF ratio | PHF stream | MP4 file |
| :--- | :--- | :--- | :--- |
| 640x360 | 3.4x | ~49 Mbps | 1.9 Mbps |
| 854x480 | 3.5x | ~85 Mbps | 2.8 Mbps |
| 1280x720 | 4.2x | ~157 Mbps | 5.0 Mbps |
| 1920x1080 | 4.4x | ~337 Mbps | ~20 Mbps |

Plan for localhost or a wired/fast LAN at 720p and above.

---

## ⚡ Architecture

```text
phf-stream-video/
├── bin/                      # Pre-built native dynamic libraries (self-contained)
│   ├── phf.dll               # Pre-compiled Windows x64 native engine
│   ├── libphf.dylib          # Pre-compiled macOS universal (arm64 + x86_64) engine
│   └── README.md             # Compilation instructions for Linux / macOS
├── server/
│   ├── base_source.py        # Abstract base class for video sources
│   ├── video_file_source.py  # File directory scanner, OpenCV validator & clock-paced playback
│   ├── encoder.py            # High-speed PHF ctypes compression wrapper (libphf)
│   └── stream_server.py      # Hardened HTTP + WebSocket streaming server with per-client queues
├── videos/                   # Place your video files here (.mp4, .mkv, .avi, .mov, .webm)
├── web/
│   ├── index.html            # Web video dashboard with player controls & live HUD
│   ├── style.css             # Glassmorphic dark theme
│   ├── stream-client.js      # WebSocket client, timeline seeking, and WASM bridge
│   ├── phf-view.js           # PHF WebAssembly loader
│   ├── phf.js                # Emscripten runtime
│   └── phf.wasm              # SIMD128 PHF decoder
├── requirements.txt          # Python dependencies
├── run.py                    # Single-command launcher
└── README.md
```

---

## 🚀 Quick Start

### 1. Place Videos in `videos/`
Drop any video files (`.mp4`, `.mkv`, `.avi`, `.mov`, `.webm`) into the `videos/` folder inside the project.

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Launch the Server
```bash
python run.py
```

Optional parameters:
```bash
python run.py --host 127.0.0.1 --http-port 8080 --ws-port 8765
```

### 4. Open in Browser
👉 **`http://localhost:8080`**

---

## 💎 Features

* **Automatic Video Directory Scanning:** Detects all video files in `videos/`, probes stream readability and metadata (resolution, native framerate, total frames, duration, file size).
* **Integrity Validation:** Decodes the first frame of each file before listing it in the UI; skips corrupt or unreadable files.
* **Precise Clock Pacing:** Streams frames synchronized with the video's native framerate (e.g. 24fps, 30fps, 60fps) using high-precision monotonic clock scheduling.
* **Interactive Web Player Controls:**
  - Dynamic video switcher dropdown with instant rescan button.
  - Play / Pause controls.
  - Seamless loop toggle.
  - Timeline progress bar with frame-accurate drag-to-seek (also previews while paused).
  - Play after the end (loop off) restarts from the beginning.
  - Playback is shared: every connected client sees and controls the same player.
* **Security & Concurrency Hardening:**
  - Bound to `127.0.0.1` by default (opt-in `--host 0.0.0.0` for LAN streaming; there is no authentication).
  - Clients can only pick videos found by the scan of `videos/`; ids never become file paths.
  - Strict Origin header verification preventing cross-site WebSocket hijacking & DNS rebinding.
  - Multi-client isolation with dedicated send queues (`ClientSession`).
  - Worker thread encoding via `asyncio.to_thread`, three frames in flight, so 1080p30 keeps up on a single-threaded libphf.
* **Live Performance HUD:** Real-time display of FPS, capture-to-render latency (ms), bitrate (Mbps), WASM SIMD128 decode time (ms), and lossless compression savings.

---

## 📜 License
MIT License. Part of the [PHF (libphf)](https://github.com/libphf/phf) ecosystem.
