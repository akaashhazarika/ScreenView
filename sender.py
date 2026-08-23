
#!/usr/bin/env python3
"""
Screen Share Sender
Captures the screen and streams it over UDP to the viewer.

Usage:
    python3 sender.py <viewer_ip> [--port 9999] [--fps 30] [--quality 50]
"""

import argparse
import io
import socket
import struct
import time
import threading

import mss
import pyautogui
from PIL import Image, ImageDraw


# Max UDP payload size (leave room for header)
MAX_CHUNK_SIZE = 60000
# Header: frame_id (4) + chunk_index (2) + total_chunks (2) + payload_len (4) = 12 bytes
HEADER_FORMAT = "!IHHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)


def capture_screen(sct, monitor):
    """Capture the screen and return raw RGB image with cursor overlay."""
    screenshot = sct.grab(monitor)
    image = Image.frombytes("RGB", (screenshot.width, screenshot.height),
                            screenshot.rgb)

    cursor_x, cursor_y = pyautogui.position()
    cx = cursor_x - monitor["left"]
    cy = cursor_y - monitor["top"]

    if 0 <= cx < image.width and 0 <= cy < image.height:
        draw = ImageDraw.Draw(image)
        r = 8
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill="red", outline="white", width=2)

    return image


def compress_frame(image, quality):
    """Compress PIL Image to JPEG bytes."""
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def send_frame(sock, viewer_addr, frame_data, frame_id):
    """Chunk and send a frame over UDP."""
    total_chunks = (len(frame_data) + MAX_CHUNK_SIZE - 1) // MAX_CHUNK_SIZE

    for i in range(total_chunks):
        start = i * MAX_CHUNK_SIZE
        end = min(start + MAX_CHUNK_SIZE, len(frame_data))
        chunk = frame_data[start:end]

        header = struct.pack(HEADER_FORMAT, frame_id, i, total_chunks, len(chunk))
        packet = header + chunk

        try:
            sock.sendto(packet, viewer_addr)
        except OSError:
            pass  # Skip on network errors, keep streaming


def main():
    parser = argparse.ArgumentParser(description="Screen Share Sender")
    parser.add_argument("viewer_ip", help="Viewer's IP address")
    parser.add_argument("--port", type=int, default=9999, help="UDP port (default: 9999)")
    parser.add_argument("--fps", type=int, default=30, help="Target FPS (default: 30)")
    parser.add_argument("--quality", type=int, default=50,
                        help="JPEG quality 1-95 (default: 50)")
    args = parser.parse_args()

    viewer_addr = (args.viewer_ip, args.port)
    frame_interval = 1.0 / args.fps

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Increase send buffer
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)

    sct = mss.mss()
    monitor = sct.monitors[1]  # Primary monitor

    print(f"Streaming {monitor['width']}x{monitor['height']} to {viewer_addr}")
    print(f"FPS: {args.fps}, JPEG quality: {args.quality}")
    print("Press Ctrl+C to stop")

    frame_id = 0
    fps_counter = 0
    fps_timer = time.time()

    try:
        while True:
            start = time.time()

            # Capture
            image = capture_screen(sct, monitor)

            # Compress
            frame_data = compress_frame(image, args.quality)

            # Send
            send_frame(sock, viewer_addr, frame_data, frame_id)

            frame_id = (frame_id + 1) % (2**32)
            fps_counter += 1

            # Print FPS every second
            now = time.time()
            if now - fps_timer >= 1.0:
                size_kb = len(frame_data) / 1024
                print(f"FPS: {fps_counter} | Frame: {size_kb:.0f} KB | "
                      f"Bandwidth: {fps_counter * size_kb / 1024:.1f} MB/s")
                fps_counter = 0
                fps_timer = now

            # Sleep to maintain target FPS
            elapsed = time.time() - start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
