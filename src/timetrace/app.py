"""Frozen, tray-first Windows application entry point."""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--control":
        from timetrace.desktop import main as control_main

        control_main(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        from timetrace.cli import main as cli_main

        raise SystemExit(cli_main(sys.argv[2:]))
    from timetrace.cli import main as cli_main

    raise SystemExit(cli_main(["track", "--tray", "--quiet", "--log", *sys.argv[1:]]))


if __name__ == "__main__":
    main()
