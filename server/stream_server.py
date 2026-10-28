import os
import sys
import json
import time
import struct
import asyncio
import math
import ipaddress
import threading
from collections import deque
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Any, Optional
from urllib.parse import urlsplit

import websockets
from websockets.asyncio.server import serve as ws_serve

from video_file_source import VideoFileSource
from encoder import PHFEncoder

# Packet types
PKT_VIDEO = 0x01
PKT_AUDIO = 0x02
PKT_JSON  = 0x03

LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}

# Frames encoded concurrently (ctypes releases the GIL). libphf encodes one frame on a
# single core without OpenMP; three in flight keep 1080p above 30 fps. Packets still go
# out in capture order.
ENCODE_PIPELINE_DEPTH = 3

def _is_local_name(hostname: str) -> bool:
    if hostname in LOOPBACK_NAMES or hostname.endswith(".local"):
        return True
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return False

def origin_allowed(origin: Optional[str], host_header: Optional[str], http_port: int) -> bool:
    if origin is None:
        return True
    o = urlsplit(origin)
    if o.scheme not in ("http", "https") or not o.hostname:
        return False
    origin_port = o.port or (443 if o.scheme == "https" else 80)
    dialed_host = urlsplit("//" + (host_header or "")).hostname
    return (origin_port == http_port
            and o.hostname == dialed_host
            and _is_local_name(o.hostname))

class NoCacheHTTPHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, ws_port: int, **kwargs):
        self.ws_port = ws_port
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/config.json":
            body = json.dumps({"ws_port": self.ws_port}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, format, *args):
        pass

class ClientSession:
    AUDIO_BACKLOG = 16

    def __init__(self, websocket):
        self.ws = websocket
        self.wants_audio = False
        self._control = deque()
        self._audio = deque(maxlen=self.AUDIO_BACKLOG)
        self._video: Optional[bytes] = None
        self._wake = asyncio.Event()

    def push_control(self, data: bytes):
        self._control.append(data)
        self._wake.set()

    def push_audio(self, data: bytes):
        self._audio.append(data)
        self._wake.set()

    def push_video(self, data: bytes):
        self._video = data
        self._wake.set()

    async def run(self):
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._control or self._audio or self._video is not None:
                if self._control:
                    data = self._control.popleft()
                elif self._audio:
                    data = self._audio.popleft()
                else:
                    data, self._video = self._video, None
                await self.ws.send(data)

class VideoStreamServer:
    def __init__(self, web_dir: str, videos_dir: str, host: str = "127.0.0.1", http_port: int = 8080, ws_port: int = 8765):
        self.web_dir = web_dir
        self.videos_dir = videos_dir
        self.host = host
        self.http_port = http_port
        self.ws_port = ws_port

        self.video_source = VideoFileSource(videos_dir)
        self.encoder = PHFEncoder()

        self.clients: Dict[Any, ClientSession] = {}
        self._running = False
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._device_lock: Optional[asyncio.Lock] = None
        self._last_status_key = None

        self._fps_counter = 0
        self._fps_last_time = time.perf_counter()
        self._measured_fps = 0.0

    def start_http(self):
        handler = lambda *args, **kwargs: NoCacheHTTPHandler(
            *args, directory=self.web_dir, ws_port=self.ws_port, **kwargs)
        self._httpd = ThreadingHTTPServer((self.host, self.http_port), handler)
        http_thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        http_thread.start()
        print(f"[HTTP] Web dashboard serving at: http://{self.host}:{self.http_port}")

    def _check_origin(self, connection, request):
        origin = request.headers.get("Origin")
        if not origin_allowed(origin, request.headers.get("Host"), self.http_port):
            print(f"[WS] Rejected handshake from origin {origin!r}")
            return connection.respond(HTTPStatus.FORBIDDEN, "Origin not allowed\n")
        return None

    async def handle_client(self, websocket):
        session = ClientSession(websocket)
        self.clients[websocket] = session
        sender = asyncio.create_task(session.run())
        peer = websocket.remote_address
        print(f"[WS] Client connected from {peer}. Active clients: {len(self.clients)}")

        try:
            init_msg = await asyncio.to_thread(self._build_video_list_msg)
            session.push_control(self._pack_json(init_msg))

            async for raw_msg in websocket:
                try:
                    data = json.loads(raw_msg)
                    action = data.get("action")

                    if action == "get_videos":
                        force = bool(data.get("refresh", False))
                        async with self._device_lock:
                            msg = await asyncio.to_thread(self._build_video_list_msg, force)
                        session.push_control(self._pack_json(msg))

                    elif action == "set_video":
                        video_id = data.get("id")
                        print(f"[Control] Changing video to: {video_id}")
                        async with self._device_lock:
                            ok, err = await asyncio.to_thread(self.video_source.start, video_id)
                        status_msg = {
                            "type": "video_status",
                            "success": ok,
                            "error": err,
                            "info": self.video_source.get_info()
                        }
                        self.broadcast(self._pack_json(status_msg))

                    elif action == "toggle_audio":
                        session.wants_audio = bool(data.get("enabled", True))
                        print(f"[Control] Audio enabled for {peer}: {session.wants_audio}")
                        audio_status = {
                            "type": "audio_status",
                            "enabled": session.wants_audio and self.video_source.has_audio()
                        }
                        session.push_control(self._pack_json(audio_status))

                    elif action == "pause":
                        self.video_source.pause()
                        self._broadcast_status()

                    elif action == "resume":
                        self.video_source.resume()
                        self._broadcast_status()

                    elif action == "set_loop":
                        loop = bool(data.get("loop", True))
                        self.video_source.set_loop(loop)
                        self._broadcast_status()

                    elif action == "seek_percent":
                        pct = float(data.get("percent", 0.0))
                        dur = self.video_source.get_info().get("duration_sec", 0.0)
                        if dur > 0 and math.isfinite(pct):
                            target_sec = (pct / 100.0) * dur
                            self.video_source.seek_seconds(target_sec)
                            self._broadcast_status()

                except Exception as e:
                    print(f"[WS] Error processing message: {e}")

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            sender.cancel()
            self.clients.pop(websocket, None)
            print(f"[WS] Client disconnected {peer}. Remaining: {len(self.clients)}")

    def _pack_json(self, obj: Dict[str, Any]) -> bytes:
        payload = json.dumps(obj).encode("utf-8")
        return bytes([PKT_JSON]) + payload

    def _build_video_list_msg(self, force_refresh: bool = False) -> Dict[str, Any]:
        # Absolute paths stay on the server (they reveal the user name and folder layout)
        videos = [{k: v for k, v in meta.items() if k != "path"}
                  for meta in self.video_source.list_devices(force_refresh=force_refresh)]
        return {
            "type": "video_list",
            "videos": videos,
            "current_video": self.video_source.get_info()
        }

    def _status_key(self, info: Dict[str, Any]):
        return (info["video_id"], info["paused"], info["loop"])

    def _broadcast_status(self):
        info = self.video_source.get_info()
        self._last_status_key = self._status_key(info)
        msg = {
            "type": "video_status",
            "success": True,
            "info": info
        }
        self.broadcast(self._pack_json(msg))

    def broadcast(self, data: bytes):
        for session in self.clients.values():
            session.push_control(data)

    async def stream_loop(self):
        last_frame_ts = 0.0
        in_flight = deque()  # (encode task, capture_ts, pos_sec, duration_sec) in capture order
        
        while self._running:
            # 1. Start encoding the newest frame if a pipeline slot is free
            frame_item = self.video_source.read_frame()
            if (frame_item is not None and frame_item[0] != last_frame_ts
                    and self.clients and len(in_flight) < ENCODE_PIPELINE_DEPTH):
                capture_ts, rgb_frame, pos_sec, duration_sec = frame_item
                last_frame_ts = capture_ts
                task = asyncio.ensure_future(asyncio.to_thread(self.encoder.encode, rgb_frame))
                in_flight.append((task, capture_ts, pos_sec, duration_sec))

            # 2. Send finished encodes in order
            while in_flight and in_flight[0][0].done():
                task, capture_ts, pos_sec, duration_sec = in_flight.popleft()
                encode_res = task.result()
                if encode_res is not None:
                    phf_bytes, enc_ms, raw_sz, phf_sz = encode_res
                    
                    # Header layout (21 bytes): [PKT_VIDEO (1B)] [ts (8B)] [enc_ms (4B)] [pos_sec (4B)] [duration_sec (4B)]
                    hdr = struct.pack("<Bdfff", PKT_VIDEO, capture_ts, enc_ms, pos_sec, duration_sec)
                    packet = hdr + phf_bytes
                    for session in self.clients.values():
                        session.push_video(packet)
                    
                    self._fps_counter += 1
                    now = time.perf_counter()
                    dt = now - self._fps_last_time
                    if dt >= 1.0:
                        self._measured_fps = self._fps_counter / dt
                        self._fps_counter = 0
                        self._fps_last_time = now

            # 3. Audio chunks: always drained, so nobody gets seconds of stale audio on enable
            listeners = [s for s in self.clients.values() if s.wants_audio]
            chunk = self.video_source.get_audio_chunk()
            while chunk is not None:
                if listeners:
                    # 2-byte header: [PKT_AUDIO, 0x00] guarantees 2-byte alignment for Int16Array
                    audio_pkt = bytes([PKT_AUDIO, 0x00]) + chunk
                    for session in listeners:
                        session.push_audio(audio_pkt)
                chunk = self.video_source.get_audio_chunk()

            # 4. Playback state changed on its own (e.g. paused at the end without loop)
            if self.clients and self._status_key(self.video_source.get_info()) != self._last_status_key:
                self._broadcast_status()

            await asyncio.sleep(0.002)

    async def run(self):
        self._running = True
        self._device_lock = asyncio.Lock()
        self.start_http()

        videos = self.video_source.list_devices()
        if videos:
            # Default to 720p (HD) if available, otherwise first
            default_vid = next((v["id"] for v in videos if "720p" in v["id"].lower() or v.get("height") == 720), videos[0]["id"])
            print(f"[VideoSource] Auto-starting default video (720p): {default_vid}")
            self.video_source.start(default_vid)
        else:
            print(f"[VideoSource] No video files found in {self.videos_dir}. Waiting for user to add files...")

        print(f"[WS] WebSocket streaming server listening on ws://{self.host}:{self.ws_port}")

        # compression=None: PHF frames are already entropy-coded, and per-message deflate
        # spent ~28 ms of event-loop time per 1080p frame per client to save ~5%
        async with ws_serve(self.handle_client, self.host, self.ws_port,
                            process_request=self._check_origin,
                            compression=None):
            await self.stream_loop()

    def stop(self):
        self._running = False
        self.video_source.stop()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
