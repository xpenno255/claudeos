"""Who a security event is about: one device must not arrive as two, or as three.

`CLAUDE.md` sets the bar at failure modes that are **silent and expensive**, and
this is the backups argument in a different place. Every other thing the report
says about the network is checkable against something — a datastore is full or it
isn't, a monitor answers or it doesn't. An *attribution* is not. The report named
`XpennoNas` as a Docker host at `192.168.1.102` and graded it `serious`, and
nothing anywhere contradicted it, because the sentence was fluent and the numbers
were real. It was wrong about which machine, which is the one thing a security
finding exists to say (#59).

Expensive in the sense the backups tests use the word: acting on that finding
means investigating a host that is not doing anything, while the host that *is*
carries on. A confidently wrong name is worse than a vague one — vagueness gets
checked.

What is tested is the join and the places it gives up, because the failure was
never in the reasoning. UniFi phrases one device two ways, `{SRC_CLIENT}` keyed
by MAC and `{SRC_IP}` keyed by address, and the model was asked to reconcile
them with nothing to reconcile against. So:

  * the two phrasings collapse to one string, or the double count comes back
  * an unresolvable host says so, or the adjacency guess comes back
  * an external address stays bare, or the report starts naming the internet

`_render_msg` and `events()` take the identity map as an argument, so the render
tests substitute nothing at all. `identities()` and `report_slice()` reach the
network through `_call`, which is stubbed with recorded payload shapes — verified
against a UDM-SE on fw 5.1.27 — so nothing here touches the controller.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors import unifi  # noqa: E402


# The two templates UniFi actually uses for the same kind of event, recorded
# live. The asymmetry is the bug: SRC_CLIENT has a MAC and no address, SRC_IP has
# an address and no name, and neither says which device it is.
BLOCK = "A network intrusion attempt from {} to {} has been detected and blocked."

NAMED_SOURCE = {
    "SRC_CLIENT": {"id": "90:09:d0:0e:05:2f", "name": "XpennoNas",
                   "device_fingerprint_id": 2900, "fingerprint_source": 1},
    "DST_IP": {"id": "185.183.32.210", "name": "185.183.32.210", "not_actionable": True},
}
BARE_SOURCE = {
    "SRC_IP": {"id": "192.168.1.53", "name": "192.168.1.53", "not_actionable": True},
    "DST_IP": {"id": "172.249.83.28", "name": "172.249.83.28", "not_actionable": True},
}
INBOUND = {
    "SRC_IP": {"id": "198.235.24.173", "name": "198.235.24.173", "not_actionable": True},
    "DST_IP": {"id": "192.168.1.102", "name": "192.168.1.102", "not_actionable": True},
}
# The gateway's own block, which carries its address inline and so needs no lookup.
GATEWAY = {
    "DEVICE": {"id": "f4:e2:c6:f2:04:c9", "name": "UDM SE", "ip": "192.168.50.1",
               "model": "UniFi Dream Machine PRO SE"},
}

CLIENTS = [
    {"name": "XpennoNas", "ip": "192.168.1.53", "mac": "90:09:d0:0e:05:2f", "wired": True},
    {"name": "plex", "ip": "192.168.1.102", "mac": "bc:24:11:f5:87:6e", "wired": True},
]

SRC = "{SRC_CLIENT}"
IPSRC = "{SRC_IP}"


def render(template, params, idents):
    return unifi._render_msg(template, params, idents)


class RenderTest(unittest.TestCase):
    """The map is an argument, so these need no network and no substitution."""

    def setUp(self):
        with mock.patch.object(unifi, "clients", return_value=CLIENTS):
            self.idents = unifi.identities({})

    # ------------------------------------------------- one device, one identity

    def test_the_two_phrasings_of_one_device_render_identically(self):
        """The double count, in one assertion. `XpennoNas` and `192.168.1.53` are
        the same NAS; the report counted them as two offending hosts, and "several
        internal hosts are doing this" reads very differently from "one is"."""
        by_name = render(BLOCK.format(SRC, "{DST_IP}"), NAMED_SOURCE, self.idents)
        by_ip = render(BLOCK.format(IPSRC, "{DST_IP}"), BARE_SOURCE, self.idents)

        self.assertIn("XpennoNas (192.168.1.53)", by_name)
        self.assertIn("XpennoNas (192.168.1.53)", by_ip)

    def test_the_destination_that_was_misread_as_the_source_is_named(self):
        """`192.168.1.102` appeared only as an inbound *destination*, and `plex`
        appeared nowhere in the snapshot at all — so there was nothing to resolve
        it against and the model attached the nearest name it could see."""
        msg = render(BLOCK.format(IPSRC, "{DST_IP}"), INBOUND, self.idents)

        self.assertIn("plex (192.168.1.102)", msg)
        self.assertNotIn("XpennoNas", msg)

    # ----------------------------------------------------- where it gives up

    def test_an_unresolvable_named_host_says_the_address_is_unknown(self):
        """A client that has left the associated list has no address here. Saying
        so is the only rendering that cannot be mistaken for an identification —
        a bare `XpennoNas` next to an unrelated IP is how this started."""
        msg = render(BLOCK.format(SRC, "{DST_IP}"), NAMED_SOURCE, {})

        self.assertIn("XpennoNas (ip unknown)", msg)

    def test_a_client_with_no_lease_is_marked_rather_than_named_bare(self):
        """Being in the map is what stops a device reading as unplaced, so a
        client the controller knows but cannot address has to carry the marker
        into the map — otherwise it resolves to a confident bare name."""
        leaseless = [{"name": "borrowed-laptop", "ip": None, "mac": "aa:bb:cc:dd:ee:ff"}]
        with mock.patch.object(unifi, "clients", return_value=leaseless):
            idents = unifi.identities({})

        self.assertEqual(idents["aa:bb:cc:dd:ee:ff"], "borrowed-laptop (ip unknown)")

    def test_an_external_address_stays_bare(self):
        """It has no name to be missing. Appending "(ip unknown)" to an internet
        address would be false, and inventing a name for one would be worse."""
        msg = render(BLOCK.format(IPSRC, "{DST_IP}"), INBOUND, self.idents)

        self.assertIn("from 198.235.24.173 ", msg)
        self.assertNotIn("198.235.24.173 (", msg)

    def test_a_parameter_carrying_its_own_address_needs_no_lookup(self):
        """The gateway is a device, not a client, so it is never in the map — but
        its block already holds the address, and marking it unknown would report
        the one machine we are talking *to* as unidentified."""
        msg = render("{DEVICE} reported this", GATEWAY, self.idents)

        self.assertEqual(msg, "UDM SE (192.168.50.1) reported this")

    # ----------------------------------------------------- the map's two keys

    def test_the_map_is_keyed_by_mac_and_by_address(self):
        """Both keys, one string. The MAC is the load-bearing half: `{SRC_CLIENT}`
        carries no address, so an `ip -> name` map could not resolve it at all."""
        self.assertEqual(self.idents["90:09:d0:0e:05:2f"], "XpennoNas (192.168.1.53)")
        self.assertEqual(self.idents["192.168.1.53"], "XpennoNas (192.168.1.53)")

    def test_the_ops_page_feed_is_left_alone(self):
        """Without a map the wording is UniFi's own. The Ops page shows events as
        the controller phrases them and ships the raw row beside them; rewriting
        that was never the ask, and a fix that silently did would be a regression
        in a second place."""
        msg = render(BLOCK.format(SRC, "{DST_IP}"), NAMED_SOURCE, None)

        self.assertIn("from XpennoNas to 185.183.32.210", msg)


class ReportSliceTest(unittest.TestCase):
    """That the wiring actually reaches the report — and what it does when the
    client list is the thing that fails."""

    def _api(self, clients_ok=True):
        """Stand in for `_call`, keyed on path. Recorded shapes, no controller."""
        def call(_settings, _method, path, json_body=None):
            if path.endswith("/stat/sta"):
                if not clients_ok:
                    raise ConnectionError("cannot reach the controller")
                return {"data": [{"name": c["name"], "ip": c["ip"], "mac": c["mac"],
                                  "is_wired": True} for c in CLIENTS]}
            if path.endswith("/system-log/all"):
                return {"data": [{"id": "e1", "timestamp": 1_784_000_000_000,
                                  "category": "SECURITY",
                                  "message_raw": BLOCK.format(SRC, "{DST_IP}"),
                                  "title_raw": "Intrusion blocked",
                                  "parameters": NAMED_SOURCE}],
                        "total_element_count": 1319}
            if path.endswith("/stat/health"):
                return {"data": [{"subsystem": "wan", "status": "ok"}]}
            if path.endswith("/stat/device"):
                return {"data": []}
            if path.endswith("/stat/anomalies"):
                return {"data": []}
            raise AssertionError(f"unexpected path {path}")
        return call

    def test_the_report_gets_resolved_identities(self):
        """The wiring, end to end: `report_slice` is the caller that has to
        *count* offending hosts, so it is the one that must not be handed two
        names for one machine."""
        with mock.patch.object(unifi, "_call", self._api()):
            sl = unifi.report_slice({"host": "udm"})

        self.assertIn("XpennoNas (192.168.1.53)", sl["recent_security_events"][0])
        self.assertIn("resolved against", sl["event_identities"])
        self.assertEqual(sl["security_events_total"], 1319)

    def test_a_dead_client_list_costs_the_names_and_not_the_events(self):
        """Failing soft: the security events are the point of the section, and an
        unreachable client list must not take them with it. Every host degrades
        to unresolved, which is true."""
        with mock.patch.object(unifi, "_call", self._api(clients_ok=False)):
            sl = unifi.report_slice({"host": "udm"})

        self.assertIn("XpennoNas (ip unknown)", sl["recent_security_events"][0])
        self.assertEqual(sl["security_events_total"], 1319)

    def test_a_failed_resolution_is_reported_rather_than_implied(self):
        """The report is told the join never ran, instead of being handed a set of
        unresolved hosts that look the same as resolvable ones. Same reason the
        alerting state is stated rather than inferred (#53)."""
        with mock.patch.object(unifi, "_call", self._api(clients_ok=False)):
            sl = unifi.report_slice({"host": "udm"})

        self.assertIn("error", sl["event_identities"])


if __name__ == "__main__":
    unittest.main()
