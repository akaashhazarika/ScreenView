import argparse
import io
import socket
import struct
import sys
import threading
import time

from PIL import Image
from PyQt6.QtWidgets import QApplication, QLabel, QMainWindow
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtCore import Qt, QTimer


# Must match sender
HEADER_FORMAT = "!IHHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

# How often the GUI pulls the newest frame (ms). Polling faster than the
# sender's FPS keeps latency low; if no new frame is ready the poll is a no-op.
DISPLAY_POLL_MS = 15

# Safety caps for the reassembly buffer.
MAX_PENDING_FRAMES = 120      # drop oldest partials beyond this (packet loss)
RESTART_GAP = 1000            # frame_id jumping this far back == sender restart


class FrameReceiver:
    """Receives UDP packets in a background thread and reassembles frames.

    Only the *latest* fully-assembled frame is retained. The GUI thread pulls
    it on a timer, so a burst of frames never floods the GUI event queue and
    the socket thread never starves waiting on the display.
    """

    def __init__(self, port):
        self.port = port
        self.running = True

        # Frame assembly buffer: {frame_id: {chunk_index: data}}
        self.frames = {}
        self.frame_chunk_counts = {}
        self.latest_complete_frame_id = -1

        # Latest assembled frame, handed to the GUI thread on demand.
        self._lock = threading.Lock()
        self._latest_frame = None
        self._latest_frame_id = -1

    def start_receiving(self):
        """Run in a background thread."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        sock.bind(("0.0.0.0", self.port))
        sock.settimeout(1.0)

        local_ip = self._get_local_ip()
        print(f"Viewer listening on {local_ip}:{self.port}")
        print("Waiting for sender...")

        fps_counter = 0
        fps_timer = time.time()

        while self.running:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            if len(data) < HEADER_SIZE:
                continue

            frame_id, chunk_index, total_chunks, payload_len = struct.unpack(
                HEADER_FORMAT, data[:HEADER_SIZE]
            )
            payload = data[HEADER_SIZE:HEADER_SIZE + payload_len]

            if total_chunks == 0 or chunk_index >= total_chunks:
                continue  # corrupt header

            # Decide whether this frame_id is stale, current, or a restart.
            if frame_id < self.latest_complete_frame_id:
                behind = self.latest_complete_frame_id - frame_id
                is_wrap = self.latest_complete_frame_id > 2**31 and frame_id < 2**31
                is_restart = behind > RESTART_GAP
                if not (is_wrap or is_restart):
                    continue  # genuinely old frame — ignore
                if is_restart:
                    # Sender restarted: throw away the old session's state.
                    self.frames.clear()
                    self.frame_chunk_counts.clear()
                    self.latest_complete_frame_id = -1

            # Store chunk
            if frame_id not in self.frames:
                self.frames[frame_id] = {}
                self.frame_chunk_counts[frame_id] = total_chunks
                self._evict_if_needed(frame_id)

            self.frames[frame_id][chunk_index] = payload

            # Check if frame is complete
            if len(self.frames[frame_id]) == total_chunks:
                try:
                    frame_data = b"".join(
                        self.frames[frame_id][i] for i in range(total_chunks)
                    )
                except KeyError:
                    # Duplicate/out-of-range indices masqueraded as complete.
                    self._drop_frame(frame_id)
                    continue

                self.latest_complete_frame_id = frame_id
                with self._lock:
                    self._latest_frame = frame_data
                    self._latest_frame_id = frame_id

                fps_counter += 1
                now = time.time()
                if now - fps_timer >= 1.0:
                    print(f"Receiving: {fps_counter} FPS | "
                          f"Frame: {len(frame_data) / 1024:.0f} KB")
                    fps_counter = 0
                    fps_timer = now

                # Clean up this frame and any older partials.
                old_ids = [fid for fid in self.frames if fid <= frame_id]
                for fid in old_ids:
                    self._drop_frame(fid)

        sock.close()

    def take_latest(self, since_id):
        """Return (frame_id, bytes) if a frame newer than `since_id` exists,
        else (None, None). Called from the GUI thread."""
        with self._lock:
            if self._latest_frame is not None and self._latest_frame_id != since_id:
                return self._latest_frame_id, self._latest_frame
        return None, None

    def _drop_frame(self, fid):
        self.frames.pop(fid, None)
        self.frame_chunk_counts.pop(fid, None)

    def _evict_if_needed(self, current_id):
        """Bound memory: if too many frames are half-received (lost chunks),
        discard the oldest so the buffer can't grow without limit."""
        if len(self.frames) <= MAX_PENDING_FRAMES:
            return
        for fid in sorted(self.frames)[:-MAX_PENDING_FRAMES]:
            if fid != current_id:
                self._drop_frame(fid)

    @staticmethod
    def _get_local_ip():
        """Best-effort local IP for display only. Never raises."""
        try:
            # Open a UDP socket to a public address; this picks the outbound
            # interface without actually sending any packets.
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("8.8.8.8", 80))
                return probe.getsockname()[0]
            finally:
                probe.close()
        except OSError:
            return "0.0.0.0"

    def stop(self):
        self.running = False


class ViewerWindow(QMainWindow):
    """Main window that displays the received screen."""

    def __init__(self, receiver):
        super().__init__()
        self.receiver = receiver
        self._last_shown_id = -1

        self.setWindowTitle("Screen Share Viewer")
        self.setStyleSheet("background-color: black;")

        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet("color: white; font-size: 24px;")
        self.label.setText("Waiting for sender...")
        self.setCentralWidget(self.label)

        # Start fairly large
        self.resize(1280, 720)

        # Keep a reference to the latest pixmap so we can rescale on resize
        self._current_pixmap = None

        # Pull the freshest frame at a steady cadence instead of reacting to
        # every packet. Stale frames are skipped, so the GUI never backlogs.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_frame)
        self.timer.start(DISPLAY_POLL_MS)

    def _poll_frame(self):
        frame_id, frame_data = self.receiver.take_latest(self._last_shown_id)
        if frame_data is None:
            return
        self._last_shown_id = frame_id
        self._render(frame_data)

    def _render(self, frame_data: bytes):
        try:
            image = Image.open(io.BytesIO(frame_data))
            image = image.convert("RGB")

            # Convert PIL Image to QPixmap
            data = image.tobytes()
            qimage = QImage(data, image.width, image.height,
                            3 * image.width, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(qimage)
            self._current_pixmap = pixmap
            self._rescale()
        except Exception as e:
            # Ignore an occasional corrupt/partial frame; keep the last image.
            print(f"Failed to decode frame: {e}", file=sys.stderr)

    def _rescale(self):
        if self._current_pixmap is None:
            return
        scaled = self._current_pixmap.scaled(
            self.label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.label.setPixmap(scaled)

    def resizeEvent(self, event):
        """Rescale the current frame when the window is resized."""
        super().resizeEvent(event)
        self._rescale()


def main():
    parser = argparse.ArgumentParser(description="UDP screen-share viewer")
    parser.add_argument("--port", type=int, default=9999,
                        help="UDP port to listen on (must match sender)")
    args = parser.parse_args()

    app = QApplication(sys.argv)

    receiver = FrameReceiver(args.port)
    window = ViewerWindow(receiver)

    # Run the blocking socket loop off the GUI thread so the UI stays responsive.
    thread = threading.Thread(target=receiver.start_receiving, daemon=True)
    thread.start()

    window.show()

    exit_code = app.exec()

    # Clean shutdown of the background thread.
    receiver.stop()
    thread.join(timeout=2.0)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
