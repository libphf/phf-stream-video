import os
import time
import queue
import threading
from typing import List, Dict, Any, Optional, Tuple
import numpy as np

try:
    import av
    HAS_AV = True
except ImportError:
    HAS_AV = False
    # Fallback only: opencv-python and PyAV each bundle their own FFmpeg, and loading
    # both in one process on macOS triggers duplicate Objective-C class warnings
    import cv2

from base_source import BaseVideoSource

SUPPORTED_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.wmv', '.m4v')

class VideoFileSource(BaseVideoSource):
    """
    Video & Audio file playback source for PHF Streaming.
    Supports multi-format container demuxing, synchronized 16-bit PCM audio extraction,
    precise clock pacing, seeking, and live playback timestamp tracking.
    """

    def __init__(self, videos_dir: str):
        self.videos_dir = os.path.abspath(videos_dir)
        self._running = False
        self._paused = False
        self._loop = True
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self._current_video_id: Optional[str] = None
        self._current_video_path: Optional[str] = None
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_ts: float = 0.0
        self._current_pos_sec: float = 0.0
        self._seek_target_sec: Optional[float] = None
        self._frame_count: int = 0
        self._current_frame_idx: int = 0
        self._total_frames: int = 0

        self._width: int = 0
        self._height: int = 0
        self._fps: float = 30.0
        self._duration_sec: float = 0.0
        self._has_audio: bool = False
        self._cached_videos: Optional[List[Dict[str, Any]]] = None

        self._audio_queue = queue.Queue(maxsize=150)

    def list_devices(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        if self._cached_videos is not None and not force_refresh:
            return self._cached_videos

        os.makedirs(self.videos_dir, exist_ok=True)
        videos = []

        try:
            entries = sorted(os.listdir(self.videos_dir))
        except OSError as e:
            print(f"[VideoSource] Error reading directory {self.videos_dir}: {e}")
            return []

        for fname in entries:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue

            full_path = os.path.join(self.videos_dir, fname)
            if not os.path.isfile(full_path):
                continue

            w, h, fps, total_frames, duration_sec, has_audio = 0, 0, 30.0, 0, 0.0, False

            if HAS_AV:
                try:
                    with av.open(full_path) as container:
                        if not container.streams.video:
                            continue
                        vs = container.streams.video[0]
                        w = vs.width or 0
                        h = vs.height or 0
                        if vs.average_rate:
                            fps = float(vs.average_rate)
                        total_frames = vs.frames or int(container.duration / 1000000.0 * fps) if container.duration else 0
                        duration_sec = float(container.duration / 1000000.0) if container.duration else 0.0
                        has_audio = len(container.streams.audio) > 0
                        # Validate that the stream really decodes, not just that headers parse
                        next(container.decode(video=0))
                except StopIteration:
                    print(f"[VideoSource] Skipping {fname}: no decodable video frame")
                    continue
                except Exception as e:
                    print(f"[VideoSource] PyAV probe error on {fname}: {e}")
                    continue
            else:
                cap = cv2.VideoCapture(full_path)
                if not cap.isOpened():
                    cap.release()
                    continue
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                fps = float(cap.get(cv2.CAP_PROP_FPS))
                total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                duration_sec = total_frames / fps if total_frames > 0 and fps > 0 else 0.0
                decodable, _ = cap.read()
                cap.release()
                if not decodable:
                    print(f"[VideoSource] Skipping {fname}: no decodable video frame")
                    continue

            if w <= 0 or h <= 0:
                continue

            if fps <= 0 or np.isnan(fps) or fps > 240:
                fps = 30.0

            if total_frames <= 0 and duration_sec > 0:
                total_frames = int(duration_sec * fps)

            size_mb = os.path.getsize(full_path) / (1024 * 1024)
            mins = int(duration_sec // 60)
            secs = int(duration_sec % 60)
            audio_tag = " [Audio]" if has_audio else ""
            label = f"{fname} ({w}x{h} @ {fps:.1f}fps, {mins:02d}:{secs:02d}){audio_tag}"

            videos.append({
                "id": fname,
                "name": fname,
                "label": label,
                "path": full_path,
                "width": w,
                "height": h,
                "fps": round(fps, 2),
                "total_frames": total_frames,
                "duration_sec": round(duration_sec, 2),
                "has_audio": has_audio,
                "size_mb": round(size_mb, 2),
                "valid": True
            })

        self._cached_videos = videos
        return videos

    def start(self, device_id: Any, width: int = 0, height: int = 0, fps: int = 0) -> Tuple[bool, Optional[str]]:
        # The id comes from the client: only names found by the directory scan are
        # accepted, so it can never become a path outside videos_dir
        video_name = str(device_id)
        meta = next((v for v in self.list_devices() if v["id"] == video_name), None)
        if meta is None:
            meta = next((v for v in self.list_devices(force_refresh=True) if v["id"] == video_name), None)
        if meta is None:
            return False, f"Unknown video: {video_name}"

        self.stop()
        time.sleep(0.05)
        self._drain_audio()

        self._current_video_id = video_name
        self._current_video_path = meta["path"]
        self._width = meta["width"]
        self._height = meta["height"]
        self._fps = meta["fps"]
        self._total_frames = meta["total_frames"]
        self._duration_sec = meta["duration_sec"]
        self._has_audio = meta["has_audio"]

        self._current_frame_idx = 0
        self._current_pos_sec = 0.0
        self._seek_target_sec = None
        self._frame_count = 0
        self._running = True
        self._paused = False

        self._thread = threading.Thread(target=self._playback_loop_av if HAS_AV else self._playback_loop_cv, daemon=True)
        self._thread.start()

        return True, None

    def _playback_loop_av(self):
        while self._running:
            try:
                container = av.open(self._current_video_path)
            except Exception as e:
                print(f"[VideoSource] Error opening container: {e}")
                break

            v_streams = container.streams.video
            if not v_streams:
                container.close()
                break
            v_stream = v_streams[0]
            a_streams = container.streams.audio
            a_stream = a_streams[0] if a_streams else None

            resampler = av.AudioResampler(format='s16', layout='mono', rate=44100) if a_stream else None
            demux_streams = [v_stream] + ([a_stream] if a_stream else [])

            frame_interval = 1.0 / self._fps if self._fps > 0 else 1.0 / 30.0
            next_frame_time = time.perf_counter()
            skip_until: Optional[float] = None  # after a seek: decode silently up to the target
            show_one = False                    # publish one frame even while paused (seek preview)

            for packet in container.demux(*demux_streams):
                if not self._running:
                    break

                # Handle seek request
                if self._seek_target_sec is not None:
                    target_s = self._seek_target_sec
                    self._seek_target_sec = None
                    try:
                        # Lands on the keyframe at or before the target
                        container.seek(int(target_s / float(v_stream.time_base)), stream=v_stream)
                        for st in demux_streams:
                            flush = getattr(st.codec_context, "flush_buffers", None)
                            if flush:
                                flush()
                        self._drain_audio()
                        skip_until = target_s
                        show_one = True
                        next_frame_time = time.perf_counter()
                        self._current_pos_sec = target_s
                        self._current_frame_idx = int(target_s * self._fps)
                    except Exception as e:
                        print(f"[VideoSource] Seek error: {e}")
                    # This packet was read before the seek
                    continue

                if self._paused and not show_one:
                    while self._paused and self._running and self._seek_target_sec is None:
                        time.sleep(0.02)
                    next_frame_time = time.perf_counter()
                    if self._seek_target_sec is not None:
                        continue

                try:
                    frames = packet.decode()
                except Exception:
                    continue

                for frame in frames:
                    if isinstance(frame, av.VideoFrame):
                        pos_s = float(frame.pts * v_stream.time_base) if (frame.pts is not None and v_stream.time_base) else (self._current_frame_idx / self._fps)
                        if skip_until is not None:
                            if pos_s < skip_until - frame_interval / 2:
                                continue
                            skip_until = None

                        self._current_frame_idx += 1
                        ts = time.time()
                        rgb = frame.to_ndarray(format='rgb24')

                        with self._lock:
                            self._latest_frame = np.ascontiguousarray(rgb)
                            self._latest_ts = ts
                            self._current_pos_sec = pos_s
                            self._frame_count += 1

                        if show_one:
                            show_one = False
                            next_frame_time = time.perf_counter()
                            if self._paused:
                                continue

                        # Precision Clock pacing
                        next_frame_time += frame_interval
                        sleep_dur = next_frame_time - time.perf_counter()
                        if sleep_dur > 0:
                            time.sleep(sleep_dur)
                        elif sleep_dur < -0.1:
                            next_frame_time = time.perf_counter()

                    elif isinstance(frame, av.AudioFrame) and resampler:
                        if skip_until is not None or self._paused:
                            continue
                        resampled = resampler.resample(frame)
                        for rf in resampled:
                            pcm_bytes = rf.to_ndarray().tobytes()
                            try:
                                self._audio_queue.put_nowait(pcm_bytes)
                            except queue.Full:
                                try:
                                    self._audio_queue.get_nowait()
                                    self._audio_queue.put_nowait(pcm_bytes)
                                except queue.Empty:
                                    pass

            container.close()

            if not self._running:
                break
            if not self._loop:
                # Hold the last frame; Play restarts from the top, a seek resumes there
                self._paused = True
                while self._paused and self._running and self._seek_target_sec is None:
                    time.sleep(0.02)
            self._current_frame_idx = 0
            self._current_pos_sec = 0.0

    def _playback_loop_cv(self):
        cap = cv2.VideoCapture(self._current_video_path)
        frame_interval = 1.0 / self._fps if self._fps > 0 else 1.0 / 30.0
        next_frame_time = time.perf_counter()
        show_one = False  # publish one frame even while paused (seek preview)

        while self._running:
            if self._seek_target_sec is not None:
                target_f = int(self._seek_target_sec * self._fps)
                self._seek_target_sec = None
                cap.set(cv2.CAP_PROP_POS_FRAMES, target_f)
                self._current_frame_idx = target_f
                next_frame_time = time.perf_counter()
                show_one = True

            if self._paused and not show_one:
                time.sleep(0.02)
                next_frame_time = time.perf_counter() + frame_interval
                continue

            ret, frame = cap.read()
            if not ret or frame is None:
                if self._loop:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    self._current_frame_idx = 0
                    self._current_pos_sec = 0.0
                    next_frame_time = time.perf_counter()
                    continue
                else:
                    # Hold the last frame; Play restarts from the top, a seek resumes there
                    self._paused = True
                    while self._paused and self._running and self._seek_target_sec is None:
                        time.sleep(0.02)
                    if self._seek_target_sec is None:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        self._current_frame_idx = 0
                        self._current_pos_sec = 0.0
                    next_frame_time = time.perf_counter()
                    continue

            self._current_frame_idx += 1
            ts = time.time()
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pos_s = self._current_frame_idx / self._fps

            with self._lock:
                self._latest_frame = np.ascontiguousarray(rgb)
                self._latest_ts = ts
                self._current_pos_sec = pos_s
                self._frame_count += 1

            if show_one:
                show_one = False
                next_frame_time = time.perf_counter()
                if self._paused:
                    continue

            next_frame_time += frame_interval
            sleep_dur = next_frame_time - time.perf_counter()
            if sleep_dur > 0:
                time.sleep(sleep_dur)
            elif sleep_dur < -0.1:
                next_frame_time = time.perf_counter()

        cap.release()

    def read_frame(self) -> Optional[Tuple[float, np.ndarray, float, float]]:
        with self._lock:
            if self._latest_frame is None:
                return None
            return (self._latest_ts, self._latest_frame, self._current_pos_sec, self._duration_sec)

    def _drain_audio(self) -> None:
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break

    def seek_seconds(self, target_seconds: float) -> None:
        self._seek_target_sec = max(0.0, min(target_seconds, self._duration_sec))

    def get_audio_chunk(self) -> Optional[bytes]:
        try:
            return self._audio_queue.get_nowait()
        except queue.Empty:
            return None

    def has_audio(self) -> bool:
        return self._has_audio

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def set_loop(self, loop: bool) -> None:
        self._loop = loop

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
            self._thread = None

        with self._lock:
            self._latest_frame = None

    def get_info(self) -> Dict[str, Any]:
        progress = (self._current_pos_sec / self._duration_sec) if self._duration_sec > 0 else 0.0
        return {
            "video_id": self._current_video_id,
            "width": self._width,
            "height": self._height,
            "fps": round(self._fps, 2),
            "frame_current": self._current_frame_idx,
            "frame_total": self._total_frames,
            "current_pos_sec": round(self._current_pos_sec, 2),
            "progress_percent": round(progress * 100, 1),
            "duration_sec": round(self._duration_sec, 1),
            "has_audio": self._has_audio,
            "running": self._running,
            "paused": self._paused,
            "loop": self._loop
        }
