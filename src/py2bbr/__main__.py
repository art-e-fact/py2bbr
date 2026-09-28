"""
Make a callable script out of the principal library module.

Allows ``python -m py2bbr`` to invoke the CLI.
"""

from __future__ import annotations

import sys

from py2bbr.cli import main

if __name__ == "__main__":
    sys.exit(main())
