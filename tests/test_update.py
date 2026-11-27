import base64
import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import unicodedata
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import update  # noqa: E402


def vmess(n=1, **extra):
    # 192.0.2.0/24 is reserved for documentation, so these never reach a real server.
    config = {"v": "2", "ps": f"old name {n}", "add": f"192.0.2.{n % 250 + 1}", "port": str(10000 + n),
              "id": "00000000-0000-4000-8000-000000000000", "aid": "0", "net": "tcp", "tls": ""}
    config.update(extra)
    return "vmess://" + base64.b64encode(json.dumps(config).encode()).decode()


def decode(link):
    return json.loads(base64.b64decode(link[len("vmess://"):]))


class FeedFormats(unittest.TestCase):
    def test_plain_text_keeps_order_and_stops_at_fifty(self):
        lines = ["# comment", "vless://not-wanted"] + [vmess(n) for n in range(80)]
        servers = update.build_servers(update.feed_entries("\n".join(lines)))
        self.assertEqual(50, len(servers))
        self.assertEqual([10000 + n for n in range(50)], [s["port"] for s in servers])

    def test_base64_subscription(self):
        body = base64.b64encode("\n".join(vmess(n) for n in range(6)).encode()).decode()
        self.assertEqual(6, len(update.build_servers(update.feed_entries(body))))

    def test_free_servers_api_shape_keeps_only_vmess(self):
        rows = [{"uri": "vless://x", "protocol": "vless"},
                {"uri": vmess(1), "protocol": "vmess", "country": "sg", "ping_ms": 6, "speed_mbps": None},
                {"uri": "ss://y", "protocol": "shadowsocks"}]
        servers = update.build_servers(update.feed_entries(json.dumps({"count": 3, "servers": rows})))
        self.assertEqual([("Singapore", 6, None)], [(s["country"], s["ping"], s["speed"]) for s in servers])

    def test_json_with_metadata(self):
        feed = json.dumps({"servers": [{"link": vmess(1), "country": "sg", "ping_ms": 6.2, "speed_mbps": 987}]})
        server = update.build_servers(update.feed_entries(feed))[0]
        line = update.summary_line(server)
        for expected in ("🇸🇬", "Singapore", "987 Mbps", "6 ms"):
            self.assertIn(expected, line)

    def test_country_comes_from_flag_in_old_name(self):
        server = update.build_servers([{"link": vmess(1, ps="🇯🇵 somebody else's channel")}])[0]
        self.assertEqual("Japan", server["country"])

    def test_unusable_feeds_raise(self):
        for text in ("", "   ", "{broken", '{"nope": 1}', "this is not a feed!"):
            with self.subTest(text=text), self.assertRaises(update.FeedError):
                update.feed_entries(text)


class Validation(unittest.TestCase):
    def test_only_the_display_name_changes(self):
        original = vmess(3, net="ws", path="/p", host="example.com", tls="tls")
        renamed = decode(update.build_servers([{"link": original}])[0]["link"])
        before = decode(original)
        self.assertEqual("🌐 VPNLK.online | Global #01", renamed.pop("ps"))
        before.pop("ps")
        self.assertEqual(before, renamed)

    def test_raw_transport_is_kept_and_written_as_tcp(self):
        server = update.build_servers([{"uri": vmess(1, net="raw")}])[0]
        self.assertEqual(("tcp", "tcp"), (server["network"], decode(server["link"])["net"]))

    def test_bad_links_and_duplicates_are_skipped(self):
        bad = ["vmess://%%%", "vmess://" + base64.b64encode(b"[1,2]").decode(), vmess(port="0"),
               vmess(port="abc"), vmess(id=""), vmess(add="a b"), vmess(net="<img>"), None, 42]
        entries = [{"link": b} for b in bad] + [{"link": vmess(1)}, {"link": vmess(1, ps="copy")}]
        self.assertEqual(1, len(update.build_servers(entries)))

    def test_feed_text_cannot_inject_markup(self):
        evil = "</summary></details><script>alert(1)</script> ```[ad](https://evil.example)"
        entry = {"link": vmess(1, ps=evil, tls=evil, sni=evil), "country": evil, "ping_ms": evil, "speed_mbps": True}
        servers = update.build_servers([entry])
        page = update.render("{{STATUS_BADGES}}\n{{CONFIGS}}", servers, "now")
        self.assertNotIn("<script", page)
        self.assertNotIn("evil.example", page)


class Requests(unittest.TestCase):
    def test_sync_identifies_itself_and_sends_no_credentials(self):
        request = update.build_request("https://example.com/feed")
        self.assertTrue(request.get_header("User-agent").startswith("VPNLK-GitHub-Sync/"))
        self.assertEqual({"User-agent", "Accept"}, set(request.headers))

    def test_plain_http_is_refused_without_echoing_the_url(self):
        with self.assertRaises(update.FeedError) as caught:
            update.read_feed("http://example.com/private-path")
        self.assertNotIn("private-path", str(caught.exception))


class Outputs(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        shutil.copytree(ROOT / "templates", self.root / "templates")

    def run_main(self, lines):
        feed = self.root / "feed.txt"
        feed.write_text("\n".join(lines), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return update.main(["--source", str(feed), "--root", str(self.root)])

    def test_writes_readme_and_subscription(self):
        self.assertEqual(0, self.run_main([vmess(n) for n in range(7)]))
        plain = (self.root / "configs" / "vmess.txt").read_text()
        sub = (self.root / "configs" / "vmess-sub.txt").read_text()
        self.assertEqual(plain, base64.b64decode(sub).decode())
        readme = (self.root / "README.md").read_text()
        self.assertIn("7 free VMess configs", readme)
        self.assertNotIn("{{", readme)

    def test_website_counts_are_shown_and_kept_when_the_page_fails(self):
        page = self.root / "vmess.html"
        page.write_text('<a href="/type/vless">VLESS <small>1065</small></a>'
                        '<a href="/type/vmess" class="on" aria-current="true">VMess <small>346</small></a>'
                        '<a href="/type/trojan">Trojan <small>171</small></a>'
                        '<a href="/type/shadowsocks">Shadowsocks <small>318</small></a>'
                        '<a href="/type/vless">Free VLESS configs</a>', encoding="utf-8")
        feed = self.root / "feed.txt"
        feed.write_text(vmess(1), encoding="utf-8")
        args = ["--source", str(feed), "--root", str(self.root), "--count-source"]
        with contextlib.redirect_stdout(io.StringIO()):
            update.main(args + [str(page)])
            first = (self.root / "README.md").read_text()
            update.main(args + [str(self.root / "missing.html")])
        for name, key, number in (("VLESS", "vless", 1065), ("VMess", "vmess", 346),
                                  ("Trojan", "trojan", 171), ("Shadowsocks", "shadowsocks", 318)):
            self.assertIn(f"[{name} {number}](https://vpnlk.online/type/{key})", first)
        self.assertEqual(first, (self.root / "README.md").read_text())

    def test_page_has_one_main_heading_and_working_section_links(self):
        self.run_main([vmess(n) for n in range(3)])
        readme = re.sub(r"```.*?```", "", (self.root / "README.md").read_text(), flags=re.S)
        self.assertEqual(1, len(re.findall(r"^# ", readme, re.M)))
        self.assertNotIn("{{", readme)
        self.assertNotIn("Working servers on vpnlk.online", readme)  # nothing was read, so nothing is claimed
        headings = re.findall(r"^#{2,3} (.+)$", readme, re.M)
        # GitHub drops punctuation and emoji but keeps letters and vowel marks of every script.
        slugs = {"".join(c for c in h.lower() if c in "- " or unicodedata.category(c)[0] in "LMN"
                         or c == "\u200d").replace(" ", "-") for h in headings}
        for anchor in re.findall(r"\]\(#([^)]+)\)", readme):
            self.assertIn(anchor, slugs)

    def test_same_list_is_not_rewritten(self):
        servers = update.build_servers([{"link": vmess(n)} for n in range(7)])
        first = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertTrue(update.write_outputs(self.root, servers, first))
        before = (self.root / "README.md").read_text()
        self.assertFalse(update.write_outputs(self.root, servers, datetime(2026, 1, 2, tzinfo=timezone.utc)))
        self.assertEqual(before, (self.root / "README.md").read_text())

    def test_broken_feed_keeps_the_old_list(self):
        self.run_main([vmess(n) for n in range(7)])
        before = (self.root / "README.md").read_text()
        self.assertEqual(1, self.run_main(["vless://no-vmess-here"]))
        self.assertEqual(before, (self.root / "README.md").read_text())


if __name__ == "__main__":
    unittest.main()
