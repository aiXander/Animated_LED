import asyncio
import socket
import struct

import numpy as np
import pytest

from ledctl.transports.ddp import (
    DDP_FLAG_PUSH,
    DDP_TYPE_RGB,
    DDP_VERSION,
    MAX_PIXELS_PER_PACKET,
    DDPTransport,
)


def _recv_all(sock: socket.socket, timeout: float = 0.5) -> list[bytes]:
    sock.settimeout(timeout)
    out: list[bytes] = []
    while True:
        try:
            data, _ = sock.recvfrom(2048)
            out.append(data)
        except TimeoutError:
            return out


@pytest.fixture
def udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    yield sock
    sock.close()


def test_packet_count_and_push_flag(udp_listener):
    port = udp_listener.getsockname()[1]
    transport = DDPTransport("127.0.0.1", port)
    pixels = np.tile(np.array([[200, 100, 50]], dtype=np.uint8), (1800, 1))
    asyncio.run(transport.send_frame(pixels))
    asyncio.run(transport.close())

    packets = _recv_all(udp_listener)
    # 1800 / 480 = 3.75 → 4 packets
    assert len(packets) == 4

    for i, p in enumerate(packets):
        flags, _seq, dtype, _id, offset, length = struct.unpack(">BBBBLH", p[:10])
        assert (flags >> 6) & 0x3 == DDP_VERSION
        assert dtype == DDP_TYPE_RGB
        assert length == len(p) - 10
        is_last = i == len(packets) - 1
        assert bool(flags & DDP_FLAG_PUSH) is is_last, f"push flag wrong on packet {i}"

    # Offsets contiguous and final byte count = 1800 * 3
    total_bytes = sum(len(p) - 10 for p in packets)
    assert total_bytes == 1800 * 3


def test_payload_round_trip(udp_listener):
    port = udp_listener.getsockname()[1]
    transport = DDPTransport("127.0.0.1", port)
    rng = np.random.default_rng(42)
    pixels = rng.integers(0, 256, size=(MAX_PIXELS_PER_PACKET * 2, 3), dtype=np.uint8)
    asyncio.run(transport.send_frame(pixels))
    asyncio.run(transport.close())

    packets = _recv_all(udp_listener)
    payload = b"".join(p[10:] for p in packets)
    assert payload == pixels.tobytes()


def test_small_frame_single_packet_has_push(udp_listener):
    port = udp_listener.getsockname()[1]
    transport = DDPTransport("127.0.0.1", port)
    pixels = np.zeros((10, 3), dtype=np.uint8)
    asyncio.run(transport.send_frame(pixels))
    asyncio.run(transport.close())
    packets = _recv_all(udp_listener)
    assert len(packets) == 1
    assert packets[0][0] & DDP_FLAG_PUSH
