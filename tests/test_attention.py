"""One property of the attention strip: a check that breaks is reported, never omitted.

`CLAUDE.md` sets the bar at failure modes that are **silent and expensive**. The
strip (#75) exists because the dashboard read green over failing backups, lost
Telegram alerts and an open `serious` lab issue, and its quiet state is a line
saying "Nothing needs you". The one way `app/attention.py` can say that falsely
is by reaching it because a source raised and was skipped — and that failure is
silent by construction, since an empty list is exactly what a healthy lab looks
like. It is expensive in the way #41 was: the owner stops looking because the
page told them not to.

So what is tested is the wrapper in `collect`, with substituted sources, and
nothing about any individual source. Each source reads app state that is already
covered where it lives, or is a formatting choice that is not this bar. No
network, no data directory: the sources are lambdas.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Imported before `app`: this sets CLAUDEOS_DATA, and `store` binds
# DATA_DIR at import — after it, the redirect is too late (#66).
import tests  # noqa: F401,E402

from app import attention  # noqa: E402

NOW = 1_800_000_000.0


def source(name, read, *, system=None, tab="#/ops/x"):
    return attention.Source(name, f"the {name} check", system or name, tab, read)


def fine(*items):
    return lambda now: list(items)


def boom(now):
    raise RuntimeError("cache file is corrupt")


class FailingSourceTest(unittest.TestCase):

    def test_a_raising_source_becomes_an_item_and_the_rest_still_report(self):
        crit = attention.item("monitors", "critical", "Monitor nas is down", "#/ops/uptime")
        out = attention.collect([source("backups", boom), source("monitors", fine(crit))], now=NOW)

        self.assertEqual(out["unchecked"], ["backups"])
        self.assertIn(crit, out["items"])
        broken = [i for i in out["items"] if i.get("unchecked")]
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]["system"], "backups")
        self.assertEqual(broken[0]["severity"], "warn")
        self.assertIn("Could not check the backups check", broken[0]["text"])
        self.assertIn("cache file is corrupt", broken[0]["text"])
        # it must still link somewhere a person can look for themselves
        self.assertEqual(broken[0]["tab"], "#/ops/x")

    def test_every_source_failing_is_never_an_empty_list(self):
        names = ["connectors", "backups", "notify", "reports"]
        out = attention.collect([source(n, boom) for n in names], now=NOW)

        self.assertEqual(len(out["items"]), len(names))
        self.assertTrue(all(i["severity"] == "warn" for i in out["items"]))
        self.assertEqual(out["unchecked"], names)

    def test_a_malformed_answer_counts_as_not_checked(self):
        # Each of these would otherwise be dropped or crash the renderer, and a
        # dropped answer is indistinguishable from a clean one.
        good = attention.item("x", "warn", "real", None)
        shapes = {
            "none": lambda now: None,
            "dict": lambda now: {"items": []},
            "bad-severity": fine(attention.item("x", "urgent", "disk", None)),
            "blank-text": fine(attention.item("x", "critical", "  ", None)),
            # all or nothing: half an answer from a broken check is not used
            "partly-good": fine(good, "not an item"),
        }
        out = attention.collect([source(n, f) for n, f in shapes.items()], now=NOW)

        self.assertEqual(sorted(out["unchecked"]), sorted(shapes))
        self.assertNotIn(good, out["items"])
        self.assertEqual(len(out["items"]), len(shapes))

    def test_the_quiet_state_is_only_reached_when_every_source_answered(self):
        out = attention.collect([source("a", fine()), source("b", fine())], now=NOW)
        self.assertEqual(out["items"], [])
        self.assertEqual(out["unchecked"], [])
        # the strip prints when it looked, so a quiet line is never timeless
        self.assertEqual(out["checked"], NOW)

    def test_worst_first(self):
        out = attention.collect([
            source("a", fine(attention.item("a", "info", "image update", None))),
            source("b", boom),
            source("c", fine(attention.item("c", "critical", "backup failed", None))),
        ], now=NOW)
        self.assertEqual([i["severity"] for i in out["items"]], ["critical", "warn", "info"])


if __name__ == "__main__":
    unittest.main()
