"""Deprecated compatibility launcher; use ``python3 -m midware.app``."""

from pathlib import Path
import sys


if __package__ in {None, ""}:
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)

from midware import runtime as _runtime  # noqa: E402
from midware.app import app, main  # noqa: E402,F401


def __getattr__(name: str):
    """Preserve imports of legacy runtime attributes for one migration cycle."""
    return getattr(_runtime, name)


if __name__ == "__main__":
    print("DEPRECATED: use `python3 -m midware.app`; compatibility removal target: v2.")
    main()
