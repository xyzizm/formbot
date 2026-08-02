#!/usr/bin/env python3
"""
FormBot entry point.

    python3 run.py                     # uses config.json
    python3 run.py --config other.json
"""

import argparse

from formbot.bot import Bot, load_config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Telegram bot that collects applications into a spreadsheet"
    )
    parser.add_argument("--config", default="config.json", help="path to config")
    args = parser.parse_args()

    Bot(load_config(args.config)).run()


if __name__ == "__main__":
    main()
