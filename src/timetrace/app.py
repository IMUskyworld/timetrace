"""Frozen, tray-first Windows application entry point."""

from __future__ import annotations

import sys


def main() -> None:
    from timetrace.cli import main as cli_main

    raise SystemExit(cli_main(["track", "--tray", "--quiet", "--log", *sys.argv[1:]]))


if __name__ == "__main__":
    main()
