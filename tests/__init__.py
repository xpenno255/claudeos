"""Suite-wide isolation of the data directory.

Individual suites already redirect `CLAUDEOS_DATA` at a temp directory, but that
is opt-in, and the classes that never opted in resolved to the owner's real
`data/` — which is how a test run came to append 368 imaginary entries to the
real ops log, including failures that `reports` then fed to the model through
`recent_warnings` as fact (#66).

Isolation belongs here rather than in each `setUp` because the failure mode is
*omission*: a suite that forgets is indistinguishable from one that had nothing
to isolate, and nothing fails when it forgets. Setting it once, before any `app`
module is imported, means a test cannot reach the real data directory by
default — `store` resolves `DATA_DIR` at import and everything downstream joins
onto it, so this has to happen before the first `from app import ...`.

An explicit `CLAUDEOS_DATA` in the environment still wins, so a deliberate run
against a chosen directory is unaffected.
"""

import os
import tempfile

SUITE_DATA_DIR = os.environ.get("CLAUDEOS_DATA") or tempfile.mkdtemp(
    prefix="claudeos-tests-")
os.environ["CLAUDEOS_DATA"] = SUITE_DATA_DIR


def restore_data_dir() -> None:
    """Hand the suite's directory back after a test borrowed its own.

    Teardowns must call this rather than deleting `CLAUDEOS_DATA`: unsetting it
    restores the *real* data directory for whatever runs next, which reopens the
    hole this module exists to close.
    """
    os.environ["CLAUDEOS_DATA"] = SUITE_DATA_DIR
