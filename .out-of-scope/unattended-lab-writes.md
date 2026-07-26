# Unattended Writes Against the Lab

ClaudeOS does not change the homelab without a human in the loop **at the moment
of action**. Triage investigates and writes down what it found; a person decides
whether to act on it.

This covers every variant of "let it fix the thing itself", including:

- **Auto-fix** — a triage run that applies its own remediation.
- **Comment-triggered fix** — `/fix` (or similar) on a lab issue re-running the
  investigation with write tools enabled.
- Anything else where ClaudeOS mutates lab state on a trigger that is not a
  person pressing a button with the specific action in front of them.

## Why this is out of scope

**It is a different safety class, and the app already has a boundary drawn
against it.** `app/chat.py` exists in its current shape because writes to the lab
are confirm-gated: the loop suspends on a write tool, surfaces the exact tool and
parameters, and waits. That approval hook is the whole point —

```python
def approval(name, params, tool_use_id, env, other_results):
    pid = secrets.token_hex(8)
    env["pending_id"] = pid
    ...
```

A triage run is deliberately the opposite: `tools.schemas(include_writes=False)`
and **no approval channel passed at all**, so a write cannot be approved even if
one somehow reached the loop. Unattended writes would mean building a second,
weaker path to the same actions — which is precisely what the first path was
built to prevent.

**Triage runs unattended.** Nobody is watching while one happens. Every decision
in the feature was judged against that, and it is the reason remediation is text:
a wrong fix applied at 3am to a lab whose state the run may have misread is not
recoverable by noticing quickly.

**The escape hatch already exists and is better.** The remediation a run proposes
is written into the issue comment, precise and copy-pasteable. A human reads it,
decides, and pastes it into Ops Chat — which has the confirm-gated write tools.
That path is one extra step and keeps the approval boundary intact.

**The comment-triggered variant is not a smaller version of this.** It needs its
own answers to questions the read-only design never has to ask: what exactly does
a comment authorise, how stale can the plan be by the time the trigger fires, is
approval per-action or blanket for the run, and what happens on partial failure
half way through a multi-step fix. None of those are hard *because* nothing is
authorised today.

## If this is reconsidered

It returns as a fresh effort with its own map, not as an extension to triage. The
questions above are the agenda.

## Prior requests

- Settled during the Lab Issues wayfinder map (#14) and recorded in the spec
  (#26) under **Out of scope**. No separate ticket was filed — both variants were
  ruled out before any code existed.
