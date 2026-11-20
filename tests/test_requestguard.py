"""Which requests the server refuses before routing (#70).

This clears CLAUDE.md's bar on both counts. It's **silent**: a cross-site
request or a rebinding page leaves no trace, because the server doesn't log
requests (ADR-0002 depends on that). And it's **expensive**: a page the owner
merely opens could restart VMs, unlock a door through Home Assistant, approve
its own Ops Chat write, or re-point `proxmox.host` and receive the stored token.

The guard is pure functions of header values, so nothing here opens a socket.
Half the tests pin what must keep working, because a guard that locks out the
owner, the cron pings (ADR-0002) or the container healthcheck fails loudly
in a way that gets it reverted.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import requestguard as g  # noqa: E402


class HostTest(unittest.TestCase):
    """DNS rebinding: the attacker controls the name, so the Host header shows it."""

    def test_an_attacker_domain_is_refused(self):
        self.assertFalse(g.host_allowed("attacker.example"))
        self.assertFalse(g.host_allowed("rebind.attacker.example:8321"))
        self.assertIsNotNone(g.refusal("GET", "attacker.example:8321", None))

    def test_names_an_outsider_cannot_register_are_allowed(self):
        for h in ("192.168.1.102:8321", "127.0.0.1:8321", "[::1]:8321", "::1",
                  "localhost:8321", "docker:8321", "docker.lan:8321",
                  "claudeos.local", "claudeos.home.arpa", "box.internal",
                  "docker.home", "docker.localdomain"):
            self.assertTrue(g.host_allowed(h), h)

    def test_a_suffix_is_not_matched_by_a_registered_lookalike(self):
        self.assertFalse(g.host_allowed("evil-lan.com"))
        self.assertFalse(g.host_allowed("local.attacker.example"))

    def test_a_configured_domain_is_allowed(self):
        """A reverse proxy in front of ClaudeOS uses a real domain."""
        old = os.environ.get("CLAUDEOS_ALLOWED_HOSTS")
        os.environ["CLAUDEOS_ALLOWED_HOSTS"] = "claudeos.example.org, other.example.org"
        try:
            self.assertTrue(g.host_allowed("claudeos.example.org"))
            self.assertTrue(g.host_allowed("CLAUDEOS.example.org:443"))
            self.assertFalse(g.host_allowed("attacker.example"))
        finally:
            if old is None:
                os.environ.pop("CLAUDEOS_ALLOWED_HOSTS", None)
            else:
                os.environ["CLAUDEOS_ALLOWED_HOSTS"] = old

    def test_a_malformed_host_is_refused(self):
        self.assertFalse(g.host_allowed("[::1"))


class OriginTest(unittest.TestCase):
    """Cross-site requests: a browser always says where a write came from."""

    HOST = "192.168.1.102:8321"

    def test_a_write_from_another_site_is_refused(self):
        self.assertIsNotNone(g.refusal("POST", self.HOST, "https://attacker.example"))
        self.assertIsNotNone(g.refusal("DELETE", self.HOST, "https://attacker.example"))

    def test_another_service_on_the_same_box_is_another_origin(self):
        """The docker host runs dozens of web apps. A port is part of the origin."""
        self.assertIsNotNone(g.refusal("POST", self.HOST, "http://192.168.1.102:8080"))

    def test_a_sandboxed_or_file_page_is_refused(self):
        self.assertIsNotNone(g.refusal("POST", self.HOST, "null"))

    def test_fetch_metadata_cross_site_is_refused_even_without_origin(self):
        self.assertIsNotNone(g.refusal("POST", self.HOST, None, "cross-site"))

    def test_the_ui_itself_can_write(self):
        self.assertIsNone(g.refusal("POST", self.HOST, "http://192.168.1.102:8321", "same-origin"))

    def test_a_proxy_on_the_default_port(self):
        """Behind TLS on 443, Host often carries no port while Origin implies one."""
        old = os.environ.get("CLAUDEOS_ALLOWED_HOSTS")
        os.environ["CLAUDEOS_ALLOWED_HOSTS"] = "claudeos.example.org"
        try:
            self.assertIsNone(g.refusal("POST", "claudeos.example.org", "https://claudeos.example.org"))
        finally:
            if old is None:
                os.environ.pop("CLAUDEOS_ALLOWED_HOSTS", None)
            else:
                os.environ["CLAUDEOS_ALLOWED_HOSTS"] = old

    def test_a_cron_ping_with_no_origin_still_works(self):
        """ADR-0002: `curl -X POST <claudeos>/api/backups/<token>/ping` sends no Origin."""
        self.assertIsNone(g.refusal("POST", self.HOST, None))

    def test_reads_are_not_origin_checked(self):
        """A cross-site GET can't read the response (no CORS headers). The rebinding
        case, where it could, is caught by Host."""
        self.assertIsNone(g.refusal("GET", self.HOST, "https://attacker.example"))

    def test_the_container_healthcheck_passes(self):
        self.assertIsNone(g.refusal("GET", "127.0.0.1:8321", None))


if __name__ == "__main__":
    unittest.main()
