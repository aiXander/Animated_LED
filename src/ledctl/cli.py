import argparse
import logging
import sys
from pathlib import Path

from .api.server import create_app
from .config import load_config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledctl")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Start the controller and HTTP/WS server")
    run.add_argument("--config", required=True, type=Path, help="Path to config YAML")
    run.add_argument("--host", default=None, help="Override server.host")
    run.add_argument("--port", default=None, type=int, help="Override server.port")
    run.add_argument("--log-level", default="info")

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
        cfg = load_config(args.config)
        host = args.host or cfg.server.host
        port = args.port or cfg.server.port
        # Presets live alongside the config file by convention.
        presets_dir = args.config.parent / "presets"
        app = create_app(cfg, presets_dir=presets_dir, config_path=args.config.resolve())
        # Imported lazily so `show-config` doesn't pay the uvicorn import cost.
        import uvicorn
        uvicorn.run(app, host=host, port=port, log_level=args.log_level)
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
