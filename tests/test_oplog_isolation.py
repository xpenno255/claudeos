"""The ops log must follow the configured data directory.

Neither test here is about what the ops log records. Both are about *where*.

`app/oplog.py` used to compute its file path once, at import, from a `DATA_DIR`
value copied out of the store. Every test suite that isolates its state does so
by pointing `CLAUDEOS_DATA` at a temp directory and reloading the store — which
rebinds the store's value and leaves the copy untouched. The stores stayed
isolated and the ops log did not, so running the suite appended entries to the
owner's real `data/opslog.jsonl`: 368 in one day, including sixty-six "scheduled
report failed 3x — giving up" errors and twenty "lab repo unreachable: the
stored token was rejected" (#66).

That is expensive rather than merely untidy, because the ops log is an input to
the weekly report: `reports` builds `recent_warnings` from it and sends it to the
model, which ranks and explains what it is given. A report generated after a test
run would have reported failures that never happened — fluently, with real
numbers, and with nothing able to contradict them. It is #59's failure mode
reached from a different direction.

The invariant is therefore phrased the way `test_model_naming` phrases its own:
it forbids the mistake rather than exercising the happy path. Redirecting the
data directory must be *sufficient* to move the ops log, so that a suite which
isolates its state cannot write to the real log by omission — no test has to
remember to reload this module.
"""

import importlib
import os
import tempfile
import unittest

# Imported before `app`: this sets CLAUDEOS_DATA, and `store` binds
# DATA_DIR at import — after it, the redirect is too late (#66).
from tests import restore_data_dir

from app import oplog as _oplog
from app import store as _store


class OpsLogFollowsTheDataDirTest(unittest.TestCase):

    def setUp(self):
        # Where the log would go with the environment as the suite found it.
        # Captured before any redirection so the assertion below is about the
        # directory being *left*, whatever it happens to be.
        self.previous_path = _oplog._log_path()
        self.previous_bytes = (os.path.getsize(self.previous_path)
                               if os.path.exists(self.previous_path) else None)
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        restore_data_dir()
        importlib.reload(_store)
        importlib.reload(_oplog)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_redirecting_the_data_dir_takes_the_ops_log_with_it(self):
        os.environ["CLAUDEOS_DATA"] = self.tmp
        importlib.reload(_store)

        # Deliberately *not* reloading oplog. The point is that a suite which
        # only redirects its data must not leak here, because that omission is
        # exactly what wrote imaginary failures into the real log.
        _oplog.add("error", "test", "this line must never reach the real ops log")

        moved = os.path.join(self.tmp, "opslog.jsonl")
        self.assertTrue(os.path.exists(moved),
                        "the entry did not follow the redirected data directory")
        with open(moved, "r", encoding="utf-8") as f:
            self.assertIn("must never reach the real ops log", f.read())

        now_bytes = (os.path.getsize(self.previous_path)
                     if os.path.exists(self.previous_path) else None)
        self.assertEqual(
            self.previous_bytes, now_bytes,
            f"writing to the ops log grew {self.previous_path}, the log belonging "
            f"to the data directory the test had already redirected away from")

    def test_the_suite_is_not_pointed_at_the_real_data_directory(self):
        """Fails loudly if the run ever reaches the real data directory.

        `tests/__init__.py` moves the whole suite onto a temp directory, and two
        separate things currently deliver it: discovery from the repo root
        imports the package before any submodule, and each isolating suite
        imports it explicitly ahead of `app`. Either alone is sufficient, which
        is why both invocations pass today.

        Asserted anyway, because the ways it can quietly stop being true are not
        exotic — a new suite that imports `app` before this package, an import
        block sorted by a formatter, a module that never redirects at all. The
        original defect was invisible until someone read a polluted log, so the
        replacement for it is a test that says so on the spot.
        """
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        real = os.path.abspath(os.path.join(repo_root, "data"))
        self.assertNotEqual(
            os.path.abspath(_store.DATA_DIR), real,
            "the suite is running against the real data directory — invoke it as "
            "`python3 -m unittest discover -s tests -t .` so tests/__init__.py "
            "redirects it")


if __name__ == "__main__":
    unittest.main()
