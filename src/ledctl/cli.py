import argparse
import logging
import os
import sys
from pathlib import Path

# Before numpy loads: keep its BLAS on one thread. Every array here is tiny
# (1800 LEDs); a BLAS thread pool only adds wake-ups and jitter on the Pi.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

from .api.server import create_app  # noqa: E402
from .config import load_config  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledctl")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Start the controller and HTTP/WS server")
    run.add_argument("--config", required=True, type=Path, help="Path to config YAML")
    run.add_argument("--host", default=None, help="Override server.host")
    run.add_argument("--port", default=None, type=int, help="Override server.port")
    run.add_argument("--log-level", default="info")
    run.add_argument(
        "--access-log",
        action="store_true",
        help="Log every HTTP request (off by default: a knob drag is ~20 requests/s, "
             "each a formatted line through journald on the render thread)",
    )
    run.add_argument(
        "--open",
        action="store_true",
        help="Open the operator UI in the default browser once the server is up",
    )

    show = sub.add_parser("show-config", help="Parse the config and print it")
    show.add_argument("--config", required=True, type=Path)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "show-config":
        cfg = load_config(args.config)
        print(cfg.model_dump_json(indent=2))
        return 0

    if args.command == "run":
        logging.basicConfig(
            level=args.log_level.upper(),
            format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        )
        # Load .env (if present) before anything else so OPENROUTER_API_KEY
        # — and any other secrets the agent reads — are visible. Load the
        # repo-root .env first so its keys win, then ~/.env to fill in any
        # missing keys (load_dotenv defaults to override=False).
        try:
            from dotenv import load_dotenv

            repo_env = Path(__file__).resolve().parents[2] / ".env"
            home_env = Path.home() / ".env"
            if repo_env.is_file():
                load_dotenv(repo_env)
            if home_env.is_file():
                load_dotenv(home_env)
        except ImportError:  # pragma: no cover — defensive; dep is required
            pass
        cfg = load_config(args.config)
        host = args.host or cfg.server.host
        port = args.port or cfg.server.port
        # Presets live alongside the config file by convention.
        presets_dir = args.config.parent / "presets"
        app = create_app(cfg, presets_dir=presets_dir, config_path=args.config.resolve())
        if args.open:
            # uvicorn.run blocks, so schedule the browser open on a delay —
            # by the time the timer fires the server has bound. 0.0.0.0 isn't
            # routable from a browser, so swap in loopback for the URL.
            import threading
            import webbrowser
            ui_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
            url = f"http://{ui_host}:{port}/"
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        # Worker threads (OSC listeners, effect compiles, disk writes) share the
        # interpreter lock with the render loop. The default hand-over interval
        # is 5 ms, a third of a frame at 60 fps; 1 ms keeps a busy worker from
        # holding a frame back that long.
        sys.setswitchinterval(0.001)
        # Imported lazily so `show-config` doesn't pay the uvicorn import cost.
        import uvicorn
        uvicorn.run(
            app, host=host, port=port, log_level=args.log_level,
            access_log=args.access_log,
        )
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
