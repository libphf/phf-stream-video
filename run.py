#!/usr/bin/env python3
"""
PHF Video Stream Server
Ultra-low latency, 100% lossless video file streaming over WebSocket with WebAssembly client.
"""

import os
import sys
import argparse
import asyncio

# Ensure safe output on Windows console
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Add server directory to path
server_dir = os.path.join(os.path.dirname(__file__), "server")
sys.path.insert(0, server_dir)

from stream_server import VideoStreamServer

def main():
    parser = argparse.ArgumentParser(description="PHF Video Stream Server (File Streaming)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interface to listen on (default: 127.0.0.1). "
                             "Use 0.0.0.0 to expose streaming to the whole network.")
    parser.add_argument("--http-port", type=int, default=8080, help="HTTP dashboard port (default: 8080)")
    parser.add_argument("--ws-port", type=int, default=8765, help="WebSocket streaming port (default: 8765)")
    parser.add_argument("--videos-dir", default=None, help="Directory containing video files (default: videos/)")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    web_dir = os.path.join(base_dir, "web")
    videos_dir = args.videos_dir or os.path.join(base_dir, "videos")

    os.makedirs(videos_dir, exist_ok=True)

    server = VideoStreamServer(
        web_dir=web_dir,
        videos_dir=videos_dir,
        host=args.host,
        http_port=args.http_port,
        ws_port=args.ws_port
    )

    print("=" * 60)
    print("  🎬 PHF Video Stream Server")
    print("=" * 60)
    shown_host = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    print(f"  👉 Web Dashboard:    http://{shown_host}:{args.http_port}")
    print(f"  👉 WebSocket Stream:  ws://{shown_host}:{args.ws_port}")
    print(f"  📂 Videos Folder:     {videos_dir}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  ⚠️  Listening on {args.host}: anyone who can reach this machine can watch")
        print("      and control playback. There is no authentication.")
    print("=" * 60)
    print("Press Ctrl+C to terminate.")

    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\n[Server] Shutting down...")
    finally:
        server.stop()

if __name__ == "__main__":
    main()
