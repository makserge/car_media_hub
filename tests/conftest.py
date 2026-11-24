"""Make the add-on package importable also with ``python -I -m pytest`` (isolated mode ignores PYTHONPATH)."""
import sys
from pathlib import Path

APP = str(Path(__file__).resolve().parents[1] / "app")
if APP not in sys.path:
    sys.path.insert(0, APP)
