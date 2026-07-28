"""No view may name a model. The server says which one ran, or nobody does.

Not a module's tests — a repo invariant, which is why it sits apart from the eight
files `CLAUDE.md` lists. The bar is the same one though, and this clears it on the
silent half more clearly than most: a hardcoded model name renders confidently
forever, and nothing in the app can ever contradict it, because there is nothing
to compare it against.

That is not hypothetical. `claude-opus-4-8` was copied into six places outside its
constant, and two of them were shown to the owner as fact — including the Setup
card's claim about which model their Home Assistant logs are sent to (#62). Every
one of those was correct when written. The expense is not a bill; it is that the
app told its owner something untrue about where their data went, in the one place
they would go to check.

Structural fixes usually make their own tests redundant, and mostly this one does:
a footer that renders `_usage.model` cannot go stale. This exists for the failure
that *reintroduces* the problem — the plausible `|| "claude-opus-4-8"` fallback
added later so a footer never looks empty, which is exactly how the sixth copy got
there in the first place. A fallback is the shape this comes back in, so the
invariant has to forbid a literal anywhere in `public/`, not merely require that
the server sends one.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"

# Anything that looks like an Anthropic model id: claude-<family>-<version>.
# Broad on the family so it catches a Sonnet or Haiku literal too, and a family
# nobody has thought of yet — but anchored on a **digit-leading version segment**,
# because every real id has one and ordinary prose does not. Without that anchor
# this flags `claude-os-panel`, which is a plausible CSS class in an app called
# ClaudeOS: a guard that cries wolf is one somebody eventually deletes.
MODEL_LITERAL = re.compile(r"claude-(?:[a-z]+-)*[0-9][0-9a-z.-]*")


class NoModelLiteralInTheFrontendTest(unittest.TestCase):

    def test_no_view_names_a_model(self):
        """The whole invariant. A view renders what the server reported or says it
        does not know; it never carries its own answer, including as a fallback."""
        offenders = []
        for path in sorted(PUBLIC.rglob("*")):
            if path.suffix not in (".js", ".html", ".css") or not path.is_file():
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for hit in MODEL_LITERAL.findall(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{n}: {hit}")

        self.assertEqual(offenders, [], "a view is naming a model itself:\n  "
                                       + "\n  ".join(offenders)
                                       + "\nRender the model the server reported, "
                                         "or say it is unknown — never a literal.")

    def test_the_scan_would_actually_catch_one(self):
        """Guards the guard. A regex that quietly stops matching turns the test
        above into a permanent pass, which is the failure mode of every
        grep-shaped invariant."""
        for literal in ('`claude-opus-4-8 · ${n} tokens`',
                        'c?.model || "claude-opus-5"',
                        "model: 'claude-sonnet-5'",
                        '"claude-haiku-4-5"',
                        '"claude-3-opus-20240229"'):  # the legacy id shape
            self.assertTrue(MODEL_LITERAL.search(literal), literal)

    def test_the_scan_does_not_cry_wolf(self):
        """The other half. This app is called ClaudeOS, so strings beginning
        `claude` are ordinary here — and a guard that fails on a CSS class is one
        somebody switches off, which costs more than it ever saved."""
        for innocent in ("`${r._usage.model} · ${n} tokens`",
                         'class="claude-os-panel"',
                         "claudeos-alerts-x7Q9tK2m",   # the ntfy topic example
                         "// claude-style headings"):
            self.assertIsNone(MODEL_LITERAL.search(innocent), innocent)

    def test_the_scan_reaches_the_files_that_matter(self):
        """A path typo would also make this pass forever. The views are where the
        copies were, so the scan has to be provably looking at them."""
        scanned = {p.name for p in PUBLIC.rglob("*.js") if p.is_file()}

        for expected in ("ops.js", "setup.js", "chat.js", "labissues.js"):
            self.assertIn(expected, scanned)


if __name__ == "__main__":
    unittest.main()
