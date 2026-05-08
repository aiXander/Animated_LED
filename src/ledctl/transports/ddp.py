import socket
import struct

import numpy as np

from .base import Transport

# Distributed Display Protocol — http://www.3waylabs.com/ddp/
# Header is 10 bytes: flags, sequence, type, id, offset (u32 BE), length (u16 BE).

DDP_VERSION = 1
DDP_FLAG_PUSH = 0x01
DDP_TYPE_RGB = 0x01  # 8-bit RGB
DDP_DEST_DEFAULT = 1  # WLED listens on id=1 ("default output device")
MAX_PIXELS_PER_PACKET = 480  # 1440 data + 10 header < 1500 MTU


class DDPTransport(Transport):
    """UDP DDP sender. Splits frames across packets; PUSH only on the final packet."""

    def __init__(self, host: str, port: int = 4048, dest_id: int = DDP_DEST_DEFAULT):
        self.host = host
        self.port = port
        self.dest_id = dest_id
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # When `paused` is True, send_frame becomes a no-op. WLED's realtime
        # override expires after ~2.5 s, after which the Gledopto resumes its
        # own preset/effect — i.e. Pi control mode → Gledopto control mode
        # without needing to stop the render loop.
        self.paused: bool = False
        self.packets_sent: int = 0
        self.frames_sent: int = 0

    async def send_frame(self, pixels: np.ndarray) -> None:
        if self.paused:
            return
        # Validate shape lazily — wrong shape = caller bug, hard fail is fine.
        assert pixels.dtype == np.uint8 and pixels.ndim == 2 and pixels.shape[1] == 3
        flat = pixels.tobytes()
        n_pixels = pixels.shape[0]
        offset_bytes = 0
        pixel_idx = 0
        while pixel_idx < n_pixels:
            chunk_pixels = min(MAX_PIXELS_PER_PACKET, n_pixels - pixel_idx)
            chunk_bytes = chunk_pixels * 3
            is_last = (pixel_idx + chunk_pixels) == n_pixels
            flags = (DDP_VERSION << 6) | (DDP_FLAG_PUSH if is_last else 0)
            header = struct.pack(
                ">BBBBLH",
                flags,
                0,  # sequence disabled (0)
                DDP_TYPE_RGB,
                self.dest_id,
                offset_bytes,
                chunk_bytes,
            )
            packet = header + flat[offset_bytes : offset_bytes + chunk_bytes]
            self._sock.sendto(packet, (self.host, self.port))
            self.packets_sent += 1
            offset_bytes += chunk_bytes
            pixel_idx += chunk_pixels
        self.frames_sent += 1

    async def close(self) -> None:
        self._sock.close()
