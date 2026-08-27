from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .web import StartupError, run_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="file-hash-browser",
        description="Browse authorized directories and calculate file hashes.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("FILE_HASH_BROWSER_CONFIG", "config.json")),
        help="configuration file (default: config.json)",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate the configuration and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.check_config:
        print(
            "Configuration is valid "
            f"({len(config.roots)} root(s), {config.effective_parallel_tasks} worker(s))."
        )
        return 0

    try:
        run_server(config)
    except KeyboardInterrupt:
        return 0
    except StartupError as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
