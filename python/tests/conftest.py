from __future__ import annotations

import os
from pathlib import Path


# The application intentionally fails closed when PROJECT_ROOT is missing.
# Give schema/import tests an explicit repository root before test modules load.
os.environ.setdefault("PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))
