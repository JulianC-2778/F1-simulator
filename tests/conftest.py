"""Shared pytest bootstrap.

`midware/commentary_engine.py` and a few sibling modules import their local
neighbours with bare names (e.g. `from event_payload_config import
EVENT_FIELDS`) rather than package-qualified ones, matching how
`midware/runtime.py` runs in production (it inserts `midware/` onto
`sys.path` itself). Tests that import `midware.commentary_engine` /
`midware.context_manager` directly -- without first importing
`midware.runtime` or `midware.app`, which have that same side effect --
need the same path entry, so it's set up once here for the whole session.
"""

import sys
from pathlib import Path

_MIDWARE_DIR = Path(__file__).resolve().parent.parent / "midware"
if str(_MIDWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_MIDWARE_DIR))
