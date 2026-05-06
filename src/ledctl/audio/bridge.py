"""External audio-feature bridge.

The LED controller no longer captures or analyses audio itself. Instead we run
the [Realtime_PyAudio_FFT](https://github.com/.../Realtime_PyAudio_FFT) server
as a subprocess and consume its `/audio/lmh` and `/audio/meta` OSC messages.
Two responsibilities live here:

  - `OscFeatureListener` — bind a UDP socket on the configured port, dispatch
    `/audio/lmh` and `/audio/meta` into a shared `AudioState`. A watchdog
    thread flips `connected → False` if packets stop arriving.
  - `AudioServerSupervisor` — start the external `audio-server` subprocess,
    pipe its stdout/stderr through the local logger, and stop it on shutdown.
    Resilient on purpose: any failure (binary missing, port collision, crash
    during start) just logs a warning and leaves the LED render loop running
    with the audio state at zeros.

`AudioBridge` glues both together: one `start()` / `stop()` pair the engine
calls, a single `state` attribute the render loop reads from. If the bridge
or the subprocess fails the visual stack keeps rendering — `audio_band` reads
just return 0.0 because the AudioState scalars never get written.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from ..config import AudioServerConfig

from .state import AudioState

log = logging.getLogger(__name__)

# Watchdog: if we haven't seen a packet in this many seconds the bridge is
# considered stale. The external server emits `/audio/lmh` every audio block
# (~187 Hz @ 48k/256), so 1 s is ~190 missed packets — clearly broken.
DEFAULT_STALE_AFTER_S: float = 1.5

# How long to wait for the subprocess to start emitting before we give up and
# log a "failed to start" warning. We don't actually block startup on this —
# the LED render loop is decoupled.
DEFAULT_BOOT_TIMEOUT_S: float = 5.0


def _audio_server_already_running(ui_url: str, timeout_s: float = 0.5) -> bool:
    """TCP-probe the audio-server's UI port to detect a pre-existing instance.

    The audio-server binds an HTTP/WS UI (default 8766). If something is
    already listening there we assume the server is up — spawning a second
    one would just race for the same audio device and UI port. Returns False
    on any error so detection failures fall through to the normal spawn path.
    """
    try:
        parsed = urlparse(ui_url)
    except Exception:  # noqa: BLE001
        return False
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if port is None:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


class OscFeatureListener:
    """Bind a UDP socket and dispatch `/audio/lmh` + `/audio/meta` into AudioState.

    Uses `python-osc`'s `BlockingOSCUDPServer` driven by a daemon thread so
    callers don't have to think about asyncio integration. A second thread
    watches for stale state — if the writer's `last_packet_at` falls behind
    by `stale_after_s`, we flip `connected = False` so consumers know the
    feed has dropped.
    """

    def __init__(
        self,
        state: AudioState,
        host: str = "127.0.0.1",
        port: int = 9000,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
    ):
        self.state = state
        self.host = host
        self.port = int(port)
        self.stale_after_s = float(stale_after_s)
        self._server: Any = None
        self._server_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Optional thread-safe callback fired after every /audio/lmh write.
        # The render loop sets this so it can wake immediately on a fresh
        # packet instead of polling at the fixed render cadence. The caller
        # owns thread-safety (e.g. asyncio loop.call_soon_threadsafe).
        self.kick_callback: Callable[[], None] | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        if self._server is not None:
            return
        try:
            from pythonosc import dispatcher  # noqa: PLC0415
            from pythonosc.osc_server import BlockingOSCUDPServer  # noqa: PLC0415
        except ImportError as e:
            log.warning("python-osc not installed: %s — audio features disabled", e)
            self.state.error = f"python-osc unavailable: {e}"
            return
        d = dispatcher.Dispatcher()
        d.map("/audio/lmh", self._on_lmh)
        d.map("/audio/meta", self._on_meta)
        # /audio/fft is not consumed (the server only sends it when explicitly
        # enabled); register a no-op so we don't log "no handler" each frame.
        d.map("/audio/fft", lambda *_: None)
        try:
            # Single-threaded server: avoids the per-packet thread spawn that
            # ThreadingOSCUDPServer does at ~187 Hz. Handlers are tiny (3 float
            # writes), so serial dispatch is plenty fast and the reduced
            # scheduling jitter matters more than parallelism.
            self._server = BlockingOSCUDPServer((self.host, self.port), d)
        except OSError as e:
            log.warning(
                "OSC listener could not bind %s:%d (%s) — audio features disabled",
                self.host, self.port, e,
            )
            self.state.error = f"osc bind failed: {e}"
            self._server = None
            return
        self._stop_event.clear()
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="ledctl-osc-listener",
            daemon=True,
        )
        self._server_thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="ledctl-osc-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()
        log.info("OSC listener bound %s:%d", self.host, self.port)

    def stop(self) -> None:
        self._stop_event.set()
        srv = self._server
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception as e:  # noqa: BLE001
                log.debug("OSC listener shutdown: %s", e)
        self._server = None
        self._server_thread = None
        self._watchdog_thread = None
        self.state.connected = False
        self.state.reset_levels()

    # ---- OSC handlers ----

    def _on_lmh(self, _addr: str, *args: Any) -> None:
        if len(args) < 3:
            return
        try:
            low = float(args[0])
            mid = float(args[1])
            high = float(args[2])
        except (TypeError, ValueError):
            return
        s = self.state
        s.low = low
        s.mid = mid
        s.high = high
        s.mark_packet()
        cb = self.kick_callback
        if cb is not None:
            cb()

    def _on_meta(self, _addr: str, *args: Any) -> None:
        # Per the audio-server README: sr:i blocksize:i n_fft_bins:i
        # low_lo:f low_hi:f mid_lo:f mid_hi:f high_lo:f high_hi:f. Newer
        # versions may append fields (e.g. device_name as a trailing string).
        # Be permissive — only require the first nine and treat the rest as
        # optional.
        s = self.state
        if len(args) >= 9:
            try:
                s.samplerate = int(args[0])
                s.blocksize = int(args[1])
                s.n_fft_bins = int(args[2])
                s.low_lo = float(args[3])
                s.low_hi = float(args[4])
                s.mid_lo = float(args[5])
                s.mid_hi = float(args[6])
                s.high_lo = float(args[7])
                s.high_hi = float(args[8])
            except (TypeError, ValueError):
                pass
        # Trailing string args are interpreted as device name when present.
        # The current audio-server publishes the device name via WS only;
        # leaving this here so a future /audio/meta extension lights up the
        # UI label automatically.
        for extra in args[9:]:
            if isinstance(extra, str) and extra:
                s.device_name = extra
                break
        s.mark_packet()

    def _watchdog_loop(self) -> None:
        # Poll twice per stale_after_s so the worst-case detection latency is
        # half the threshold.
        period = max(0.05, self.stale_after_s / 2.0)
        while not self._stop_event.wait(period):
            if not self.state.connected:
                continue
            if monotonic() - self.state.last_packet_at > self.stale_after_s:
                self.state.connected = False
                self.state.error = f"no /audio/lmh in {self.stale_after_s:.1f}s"
                log.warning("audio bridge stale: %s", self.state.error)


class AudioServerSupervisor:
    """Start, monitor, and stop the external audio-server subprocess.

    Failures during start (binary missing, exec failure, immediate exit) are
    logged but never raised — the LED server keeps running without audio
    reactivity. A daemon thread drains stdout/stderr into the logger so
    errors from the audio-server are visible without leaving zombie pipes.
    """

    def __init__(
        self,
        command: Sequence[str],
        working_dir: str | None = None,
        env: dict[str, str] | None = None,
        boot_timeout_s: float = DEFAULT_BOOT_TIMEOUT_S,
    ):
        self.command = list(command)
        self.working_dir = working_dir
        self.env = env
        self.boot_timeout_s = float(boot_timeout_s)
        self._proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._error: str = ""

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def error(self) -> str:
        return self._error

    def start(self) -> None:
        if self._proc is not None:
            return
        if not self.command:
            self._error = "no audio-server command configured"
            log.warning(self._error)
            return
        # Resolve the binary up front. Popen would also raise FileNotFoundError
        # but the log message is clearer here. We also look next to
        # sys.executable so a venv install (.venv/bin/audio-server) works when
        # the user ran ledctl as `.venv/bin/ledctl` without activating.
        resolved_command = self._resolve_command(self.command)
        if resolved_command is None:
            first = self.command[0]
            self._error = (
                f"audio-server executable {first!r} not found on PATH or "
                f"alongside {sys.executable}; install Realtime_PyAudio_FFT or "
                f"set audio_server.command in config"
            )
            log.warning(self._error)
            return
        try:
            self._proc = subprocess.Popen(
                resolved_command,
                cwd=self.working_dir,
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                # New process group so SIGINT to ledctl doesn't double-kill
                # the child before we can stop it cleanly.
                start_new_session=True,
            )
        except OSError as e:
            self._error = f"failed to spawn audio-server: {e}"
            log.warning(self._error)
            self._proc = None
            return
        log.info(
            "audio-server started (pid=%d): %s",
            self._proc.pid,
            " ".join(resolved_command),
        )
        self._stop_event.clear()
        self._reader_thread = threading.Thread(
            target=self._drain_pipe,
            name="ledctl-audio-server-pipe",
            daemon=True,
        )
        self._reader_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                log.warning("audio-server didn't exit on SIGTERM, killing")
                proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1.0)
            except OSError as e:
                log.warning("audio-server terminate error: %s", e)

    # ---- internal ----

    @staticmethod
    def _resolve_command(command: Sequence[str]) -> list[str] | None:
        """Find the binary; return the resolved argv or None if missing.

        Lookup order:
          1. `python` / `python3` resolve to `sys.executable` so a config like
             `["python", "-m", "server.main"]` always uses the same interpreter
             ledctl is running under (i.e. the same venv that has the
             Realtime_PyAudio_FFT deps installed).
          2. Absolute paths pass through.
          3. Bare names go through `shutil.which` (system PATH), then fall
             back to `Path(sys.executable).parent / first` so a venv-local
             install of `audio-server` works when ledctl was launched via
             the venv's binary without activating the venv shell-side.
        """
        if not command:
            return None
        first = command[0]
        if first in {"python", "python3"}:
            return [sys.executable, *list(command[1:])]
        if first.startswith("/"):
            return list(command)
        on_path = shutil.which(first)
        if on_path is not None:
            return [on_path, *list(command[1:])]
        venv_bin = Path(sys.executable).resolve().parent / first
        if venv_bin.is_file() and os.access(venv_bin, os.X_OK):
            return [str(venv_bin), *list(command[1:])]
        return None

    def _drain_pipe(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        # Forward each line to the logger at INFO. The audio-server prefixes
        # its own log lines with module + level, so using INFO preserves
        # the original level visible in the LED server's terminal.
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    log.info("[audio-server] %s", line)
                if self._stop_event.is_set():
                    break
        except Exception as e:  # noqa: BLE001
            log.debug("audio-server pipe drain ended: %s", e)
        rc = proc.poll()
        if rc is not None and rc != 0 and not self._stop_event.is_set():
            self._error = f"audio-server exited with code {rc}"
            log.warning(self._error)


class AudioBridge:
    """Owns the OSC listener + the optional audio-server subprocess.

    Single entry point the engine wires up; both pieces are independent so
    the server can be left disabled (manual launch elsewhere) or the
    listener can be disabled (running fully without audio reactivity).
    """

    def __init__(
        self,
        listener: OscFeatureListener,
        supervisor: AudioServerSupervisor | None = None,
        ui_url: str = "http://127.0.0.1:8766",
    ):
        self.listener = listener
        self.supervisor = supervisor
        self.ui_url = ui_url

    @property
    def state(self) -> AudioState:
        return self.listener.state

    @property
    def running(self) -> bool:
        return self.listener.running

    def start(self) -> None:
        # Listener first so we don't miss the audio-server's first /audio/meta.
        self.listener.start()
        if self.supervisor is not None:
            if _audio_server_already_running(self.ui_url):
                log.info(
                    "audio-server already running at %s — attaching to it "
                    "instead of spawning a new subprocess",
                    self.ui_url,
                )
            else:
                self.supervisor.start()

    def stop(self) -> None:
        if self.supervisor is not None:
            self.supervisor.stop()
        self.listener.stop()

    @classmethod
    def from_config(cls, cfg: AudioServerConfig) -> AudioBridge:
        """Build the bridge from an `AudioServerConfig` block."""
        state = AudioState()
        listener = OscFeatureListener(
            state=state,
            host=cfg.osc_listen_host,
            port=cfg.osc_listen_port,
            stale_after_s=cfg.stale_after_s,
        )
        supervisor: AudioServerSupervisor | None = None
        if cfg.autostart:
            env = dict(os.environ)
            supervisor = AudioServerSupervisor(
                command=list(cfg.command),
                working_dir=cfg.working_dir,
                env=env,
            )
        return cls(listener=listener, supervisor=supervisor, ui_url=cfg.ui_url)
