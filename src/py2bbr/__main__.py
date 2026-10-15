"""
Allows ``python -m py2bbr`` to invoke the CLI.
"""

import sys

from py2bbr.cli import main

if __name__ == "__main__":
    sys.exit(main())
