"""The one helper every `report_slice` shares.

A weekly digest is assembled from a dozen calls against five systems, and any
of them can fail. Failing the whole report because one datastore listing timed
out would throw away the other eleven answers, so a call that raises becomes
`{"error": ...}` in place and the digest carries on.

That is not swallowing the failure: the report prompt is told that a section
showing `{"error": ...}` means the system could not be reached during
collection, and that this is itself a finding. An unreachable system is
reported *as* unreachable rather than as absent.

Lives in its own module so both sides can use it — the connectors, and
`reports.py` for the pieces that are not any connector's to produce — without
importing through `connectors/__init__.py`, which imports the connectors and
would make the cycle.
"""


def soft(fn, *args):
    """Call `fn(*args)`, turning any failure into a reportable finding."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 — a dead system is itself a finding
        return {"error": str(e)}
