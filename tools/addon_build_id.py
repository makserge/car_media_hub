"""Print the build id of an add-on code folder: python addon_build_id.py <path-to>/app/car_media_hub

Same algorithm as car_media_hub/build_info.py (kept in step by a test): first 10 hex characters of the SHA-256 over, for every
*.py file below the folder (sorted by relative path, "/" separators, __pycache__ ignored), the relative path as UTF-8, a zero
byte, the file bytes and a zero byte. Compare with the footer of the Regions page ("build <id>").
"""
import hashlib
import sys
from pathlib import Path


def compute_build_id(folder):
    folder = Path(folder)
    digest = hashlib.sha256()
    files = sorted((p for p in folder.rglob("*.py") if "__pycache__" not in p.parts), key=lambda p: p.relative_to(folder).as_posix())
    for path in files:
        digest.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:10]


if __name__ == "__main__":
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_dir():
        sys.exit("usage: addon_build_id.py <path-to>/app/car_media_hub")
    print(compute_build_id(sys.argv[1]))
