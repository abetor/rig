"""rig CLI / `python3 -m rig` entry point. A thin shim over entry.cli.main."""
import sys

from .entry.cli import main

if __name__ == "__main__":
    sys.exit(main())
