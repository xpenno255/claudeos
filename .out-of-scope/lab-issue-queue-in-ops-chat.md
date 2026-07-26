# Reading the Lab Issue Queue from Ops Chat

Ops Chat has no tool for listing, reading or searching lab issues. The queue is
read on the Lab Issues page; the assistant does not have tracker access.

## Why this is out of scope

**It sits past the destination the feature was scoped to.** Lab Issues covers
raising a problem, triaging it, and viewing the verdict. "Ask the assistant about
the tracker" is a different capability — tracker access from chat — and was a
mis-scope when it was listed as in-scope fog during charting.

**Adding it would arm the triage agent too, not just Ops Chat.** Tool exposure has
exactly one axis:

```python
def schemas(include_writes: bool = True) -> list:
    src = ALL_TOOLS if include_writes else READ_TOOLS
```

A `get_lab_issues` tool would be a read tool, so it would land in `READ_TOOLS` and
therefore in `tools.schemas(include_writes=False)` — the set a **triage run** is
given. A run investigating issue #7 would gain the ability to read every other
issue in the queue, including ClaudeOS's own prior verdicts on them. That is a
meaningful change to what a triage run is, arrived at as a side effect of a
convenience feature for chat. Doing it properly needs a per-caller axis on tool
exposure, which does not exist.

**The genuinely useful version is a different feature.** Nobody wants to ask chat
to list issues — they want *"this resembles issue #388, which turned out to be the
same flaky plug"*. That is **issue correlation**: comparing a new problem against
the verdict history to surface a likely duplicate or a recurring cause. Worth
building, and worth building deliberately — it needs a notion of similarity, a
decision about whether the comparison is a tool call or part of the triage prompt,
and a view to show the match. It is not a tool definition.

## If this is reconsidered

The correlation feature is the thing to build, not the list tool. Prerequisite:
`tools.schemas()` needs a way to expose a tool to one caller and not another,
otherwise every addition silently widens what a triage run can see.

## Prior requests

- #25 — "An Ops Chat `get_lab_issues` tool" (closed as out of scope during the
  Lab Issues wayfinder map, #14)
