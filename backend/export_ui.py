"""Export every collected snapshot for the console.

Kept at the checkout root so `python3 export_ui.py` keeps working; the code is
`rightsizer/output/console.py` (also installed as `rightsizer-export`).
"""
import sys

from rightsizer.output.console import main

if __name__ == "__main__":
    sys.exit(main())
