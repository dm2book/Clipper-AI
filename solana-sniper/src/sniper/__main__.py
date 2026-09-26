"""Entry point: ``python -m sniper [--config path] [--check-config]``."""

from __future__ import annotations

import argparse
import asyncio
import os

from sniper.app import run, setup_logging
from sniper.config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="sniper")
    parser.add_argument("--config", default=os.environ.get("SNIPER_CONFIG", "config.yaml"))
    parser.add_argument("--check-config", action="store_true", help="validate the config and exit")
    args = parser.parse_args()

    settings = load_settings(args.config)
    setup_logging(settings.log_level)
    if args.check_config:
        print(f"config OK — mode={settings.trading.mode}")
        return
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
