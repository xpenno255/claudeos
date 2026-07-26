"""What a triage verdict is, and how it is written down.

Split out of `labissues` because it changes for a different reason: this module
moves when the *verdict shape* moves, `labissues` when GitHub's API or the sweep
does. It touches no credential, no socket and no lock — it is the closed
vocabulary, the schema the model must answer in, the prompt tail that states
that contract, and the serialise/parse pair for the machine block.

The block rides inside an HTML comment, which GitHub renders invisibly, so one
comment serves a human reading prose and the app reading fields. `BLOCK_VERSION`
is this module's own compatibility clock, independent of anything in `labissues`.

Three properties are load-bearing, each forced by a real run against real lab
data during the #20 prototype:

  * **Four verdict values.** A refuted hypothesis is a useful result, not a
    failure, and "looked, found nothing" is not "could not tell".
  * **Status per finding, not per comment.** One run mixed `success`, `no_data`
    and `truncated`; a single confidence figure would have hidden that a key
    field came back empty.
  * **Remediation carries a kind.** The honest output of that run was a
    diagnostic, not a fix. A schema assuming a fix will manufacture one.
"""

import json
import re

from . import toolloop

MARKER = "claudeos-triage"
BLOCK_VERSION = 1
VERDICTS = ("diagnosed", "refuted", "inconclusive", "no_fault_found")
CONFIDENCES = ("low", "medium", "high")
SEVERITIES = ("critical", "serious", "warning", "info")
EVIDENCE_STATUSES = ("success", "no_data", "truncated", "excluded")
REMEDIATION_KINDS = ("fix", "diagnostic", "none")


TRIAGE_TAIL = """## Your job

You are triaging one issue from this homelab's issue tracker, raised by the person who owns the lab. You are working unattended: there is nobody to ask a question of, and nothing you say reaches them until you finish.

Work out what is actually wrong. Form hypotheses, then use the tools to test them against the lab's real state. Name the hypotheses you rule out — a hypothesis you can eliminate is a useful result, not a failure, and it saves the owner from checking it themselves.

Your tools are read-only. You cannot change anything, restart anything or apply a fix, and you must not claim to have done so. Nor can you ask for permission: if the only way forward is a change, the honest answer is a diagnostic — the exact next step for a human to take.

Stop when the evidence stops paying for itself. An issue you cannot settle is a real outcome; say what you checked, what you found, and what would settle it.

## Your answer

Your final answer is a structured record. The fields:

- `summary` — the comment a human reads on the issue. Markdown prose, addressed to the lab's owner. Lead with the answer, then the evidence for it, then what to do next. Quote what you actually saw. Hedge anything you did not directly confirm, and say plainly which areas went unverified and why.
- `verdict` — `diagnosed` when you have identified the cause. `refuted` when the issue's own stated cause is wrong, whether or not you found the real one. `no_fault_found` when you looked where the issue points and everything there is healthy. `inconclusive` when you could not tell.
- `confidence` — in the verdict, not in the lab.
- `severity` — the impact on the home if this is left alone, not how interesting it is.
- `refuted` — each hypothesis you ruled out, named in a short phrase.
- `evidence` — one entry per finding that carries weight, with the tool it came from and that result's own status: `success`, `no_data` (the query worked and found nothing — never report this as healthy), `truncated` (you saw part of it), or `excluded` for something you looked at and deliberately did not rely on, with the reason in the note.
- `remediation` — `kind` is `fix` for a change that resolves it, `diagnostic` for the next step that would narrow it down, `none` when there is nothing to do. `text` is what a human should actually do, specific enough to follow. ClaudeOS never runs it; a person reads it and decides."""

TRIAGE_PROMPT = f"{toolloop.BASE_PROMPT}\n\n{TRIAGE_TAIL}"

# The shape asked of the model. Deliberately not the same shape as the machine
# block: `summary` is prose for the comment and never goes in the block, and
# `executable` is absent because ClaudeOS stamps it — the model does not get to
# say whether this app will run something.
VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "verdict", "confidence", "severity", "refuted",
                 "evidence", "remediation"],
    "properties": {
        "summary": {"type": "string",
                    "description": "Markdown prose for the lab's owner, posted as the "
                                   "issue comment. Answer first, then the evidence."},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "confidence": {"type": "string", "enum": list(CONFIDENCES)},
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "refuted": {"type": "array", "items": {"type": "string"},
                    "description": "Each hypothesis ruled out, as a short named phrase."},
        "evidence": {
            "type": "array",
            "description": "One entry per finding that carries weight.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["tool", "status", "note"],
                "properties": {
                    "tool": {"type": "string",
                             "description": "The tool this finding came from."},
                    "status": {"type": "string", "enum": list(EVIDENCE_STATUSES)},
                    "note": {"type": "string",
                             "description": "What it showed. For `excluded`, why it "
                                            "was not relied on."},
                },
            },
        },
        "remediation": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "text"],
            "properties": {
                "kind": {"type": "string", "enum": list(REMEDIATION_KINDS)},
                "text": {"type": "string",
                         "description": "What a human should do. Empty when kind is "
                                        "`none`."},
            },
        },
    },
}

_BLOCK_RE = re.compile(r"<!--\s*" + re.escape(MARKER) + r"\s*(.*?)-->", re.DOTALL)



def normalised_cost(cost) -> dict:
    """Coerce whatever a caller has into the block's cost shape.

    Public because the triage run needs it for a run that died before it
    produced any figures — zero spend is still a fact worth recording."""
    c = cost if isinstance(cost, dict) else {}
    return {"input": int(c.get("input") or 0),
            "output": int(c.get("output") or 0),
            "usd": round(float(c.get("usd") or 0.0), 6)}


def _one_of(value, allowed: tuple, fallback: str) -> str:
    return value if value in allowed else fallback


def machine_block(result: dict | None, *, cost=None, error: str | None = None) -> dict:
    """A verdict reduced to exactly the fields the hidden block carries.

    Built field by field rather than passed through, because the vocabularies
    are closed and the source is a language model: an invented verdict value or
    an extra key would reach the UI that parses this. Anything unrecognised
    lands on the cautious end — an unknown verdict is `inconclusive`, not a
    diagnosis.

    `executable` is stamped here, always false. The remediation is text and
    ClaudeOS does not run it, so that is not the model's claim to make.
    """
    r = result if isinstance(result, dict) else {}
    rem = r.get("remediation") if isinstance(r.get("remediation"), dict) else {}
    block = {
        "v": BLOCK_VERSION,
        "verdict": _one_of(r.get("verdict"), VERDICTS, "inconclusive"),
        "confidence": _one_of(r.get("confidence"), CONFIDENCES, "low"),
        "severity": _one_of(r.get("severity"), SEVERITIES, "info"),
        "refuted": [str(h).strip() for h in (r.get("refuted") or [])
                    if str(h).strip()],
        "evidence": [{"tool": str(e.get("tool") or "?"),
                      "status": _one_of(e.get("status"), EVIDENCE_STATUSES, "excluded"),
                      "note": str(e.get("note") or "")}
                     for e in (r.get("evidence") or []) if isinstance(e, dict)],
        "remediation": {"kind": _one_of(rem.get("kind"), REMEDIATION_KINDS, "none"),
                        "text": str(rem.get("text") or ""),
                        "executable": False},
        "cost": normalised_cost(cost),
    }
    if error:
        # Only on the failure path. A block with an error and an `inconclusive`
        # verdict is how a failed run explains itself to whatever reads it back.
        block["error"] = str(error)
    return block


def comment_body(block: dict, prose: str) -> str:
    """The comment as posted: prose a human reads, then a block they never see.

    GitHub renders an HTML comment invisibly, so one comment serves both
    readers. The escape matters — a note containing `-->` would otherwise close
    the comment early and spill JSON onto the issue; as `\\u003e` it still
    parses back to the same string.
    """
    payload = json.dumps(block, indent=1, sort_keys=True).replace("-->", "--\\u003e")
    return f"{prose.strip()}\n\n<!-- {MARKER}\n{payload}\n-->\n"


def prose_of(body: str | None) -> str:
    """The human half of a triage comment — everything but the block.

    The pair with `comment_body`: that joins prose to block, this takes them
    apart again. Needed because a verdict read back from GitHub has to render
    the reasoning as well as the fields, and the reasoning is not in the block.
    """
    return _BLOCK_RE.sub("", body or "").strip()


def parse_verdict(body: str | None) -> dict | None:
    """The machine block in a comment, or None when there is not a usable one.

    Degrades instead of raising. A comment with no block is the normal case —
    humans write most of them — and a block that will not parse is a comment
    from an older ClaudeOS or a truncated write. Either is a fact the caller can
    render; neither should take down a sweep reading a whole issue's history.
    """
    m = _BLOCK_RE.search(body or "")
    if not m:
        return None
    try:
        block = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(block, dict) or block.get("v") != BLOCK_VERSION:
        return None
    return block
