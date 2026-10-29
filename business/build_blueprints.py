#!/usr/bin/env python3
"""Build a Business X-Ray page from blueprints (kept for existing commands).

The compiler lives in ontology-extraction/scripts/business_xray.py, which the
installed skill carries; this file runs it from a checkout.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ontology-extraction" / "scripts"))

from business_xray import *  # noqa: E402,F401,F403
from business_xray import main  # noqa: E402

if __name__ == "__main__":
    main()
