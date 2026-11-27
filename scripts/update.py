#!/usr/bin/env python3
"""Sync the top VMess configs from the VPNLK.online feed into README.md and configs/.

The default feed is the public API the Serendib Ray app uses for free servers.
It needs no account or token. The FEED_URL variable can point somewhere else.
Any of these formats work, best server first:
  * JSON: {"servers": [{"uri": "vmess://...", "country": "sg",
                        "ping_ms": 6, "speed_mbps": 987}]}   ("link" works too)
  * plain text, one vmess:// link per line
  * a base64 subscription (the same text, base64 encoded)

The script always identifies itself honestly (see USER_AGENT). If the website's
bot guard ever refuses it, allow that name on the website.
"""

import argparse
import base64
import hashlib
import http.client
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAND = "VPNLK.online"
REPO = "VPNLK-online/VPNLK.online"
SUB_URL = f"https://raw.githubusercontent.com/{REPO}/main/configs/vmess-sub.txt"
USER_AGENT = f"VPNLK-GitHub-Sync/1.0 (+https://github.com/{REPO})"
DEFAULT_FEED = "https://vpnlk.online/api/v1/free-servers"
VMESS_PAGE = "https://vpnlk.online/type/vmess"  # public page whose tabs show every protocol's count
PROTOCOLS = {"vless": "VLESS", "vmess": "VMess", "trojan": "Trojan", "shadowsocks": "Shadowsocks"}

LIMIT = 50
MAX_BYTES = 2_000_000

NETWORKS = {"tcp", "ws", "grpc", "h2", "http", "kcp", "quic", "httpupgrade", "xhttp", "splithttp"}

COUNTRIES = {
    "AE": "United Arab Emirates", "AL": "Albania", "AT": "Austria", "AU": "Australia",
    "BE": "Belgium", "BG": "Bulgaria", "BR": "Brazil", "BY": "Belarus", "CA": "Canada",
    "CH": "Switzerland", "CN": "China", "CY": "Cyprus", "CZ": "Czechia", "DE": "Germany",
    "DK": "Denmark", "EE": "Estonia", "ES": "Spain", "FI": "Finland", "FR": "France",
    "GB": "United Kingdom", "GR": "Greece", "HK": "Hong Kong", "HU": "Hungary",
    "ID": "Indonesia", "IE": "Ireland", "IL": "Israel", "IN": "India", "IR": "Iran",
    "IS": "Iceland", "IT": "Italy", "JP": "Japan", "KR": "South Korea", "KZ": "Kazakhstan",
    "LK": "Sri Lanka", "LT": "Lithuania", "LV": "Latvia", "MY": "Malaysia", "NG": "Nigeria",
    "NL": "Netherlands", "NO": "Norway", "PL": "Poland", "RO": "Romania", "RU": "Russia",
    "SA": "Saudi Arabia", "SE": "Sweden", "SG": "Singapore", "TH": "Thailand",
    "TR": "Türkiye", "TW": "Taiwan", "US": "United States", "VN": "Vietnam",
    "ZA": "South Africa",
}


class FeedError(Exception):
    """The feed could not be used. Messages never include the feed URL."""


def b64decode_text(value):
    value = "".join(value.split()).replace("-", "+").replace("_", "/")
    value += "=" * (-len(value) % 4)
    return base64.b64decode(value, validate=True).decode("utf-8-sig")


def build_request(url):
    if not url.startswith("https://"):
        raise FeedError("the feed URL must start with https://")
    return urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain"}
    )


def read_feed(source):
    if "://" not in source:
        try:
            data = Path(source).read_bytes()
        except OSError:
            raise FeedError("could not read the feed file") from None
    else:
        try:
            with urllib.request.urlopen(build_request(source), timeout=30) as response:
                data = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise FeedError(f"the feed answered HTTP {error.code}") from None
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise FeedError(f"could not reach the feed ({type(error).__name__})") from None
    if len(data) > MAX_BYTES:
        raise FeedError("the feed is larger than 2 MB")
    return data.decode("utf-8-sig", errors="replace")


def feed_entries(text):
    text = text.strip()
    if not text:
        raise FeedError("the feed is empty")
    if text.startswith(("[", "{")):
        try:
            document = json.loads(text)
        except (ValueError, RecursionError):
            raise FeedError("the feed looks like JSON but is not valid JSON") from None
        items = document.get("servers") if isinstance(document, dict) else document
        if not isinstance(items, list):
            raise FeedError('a JSON feed must be a list, or have a "servers" list')
        return [{"link": i} if isinstance(i, str) else i for i in items if isinstance(i, (str, dict))]
    if "://" not in text:
        try:
            text = b64decode_text(text)
        except ValueError:
            raise FeedError("the feed is not JSON, config links or a base64 subscription") from None
    return [{"link": line.strip()} for line in text.splitlines()]


def parse_vmess(link):
    """Return the decoded config, or None if the link is not a usable VMess link."""
    if not isinstance(link, str) or not link.startswith("vmess://"):
        return None
    try:
        config = json.loads(b64decode_text(link[len("vmess://"):].split("#")[0]))
        if not isinstance(config, dict):
            return None
        port = int(str(config.get("port")).strip())
    except (ValueError, RecursionError):
        return None
    address, user_id = config.get("add"), config.get("id")
    if not isinstance(address, str) or not address or any(c.isspace() for c in address):
        return None
    if not isinstance(user_id, str) or not user_id.strip():
        return None
    if not 1 <= port <= 65535:
        return None
    network = str(config.get("net") or "tcp").lower()
    if network == "raw":  # Xray's newer name for tcp; "tcp" also works in older apps
        config["net"] = network = "tcp"
    if network not in NETWORKS:
        return None
    return config


def country_code(declared, remark):
    if isinstance(declared, str) and re.fullmatch(r"[A-Za-z]{2}", declared):
        return declared.upper()
    flag = re.search("[\U0001F1E6-\U0001F1FF]{2}", str(remark or ""))
    if flag:
        return "".join(chr(ord(c) - 0x1F1E6 + ord("A")) for c in flag.group())
    return None


def flag_emoji(code):
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code) if code else "🌐"


def positive_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value > 0 else None


def build_servers(entries, limit=LIMIT):
    """Validate, de-duplicate and rename the configs, keeping the feed's order."""
    servers, seen = [], set()
    for entry in entries:
        config = parse_vmess(entry.get("uri") or entry.get("link"))
        if config is None:
            continue
        key = tuple(str(config.get(k) or "") for k in ("add", "port", "id", "net", "path", "host", "tls", "sni"))
        if key in seen:
            continue
        seen.add(key)
        code = country_code(entry.get("country"), config.get("ps"))
        country = COUNTRIES.get(code, code) if code else "Global"
        rank = len(servers) + 1
        # The display name changes. It is what users see inside their V2Ray app.
        renamed = dict(config, ps=f"{flag_emoji(code)} {BRAND} | {country} #{rank:02d}")
        encoded = json.dumps(renamed, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
        security = str(config.get("tls") or "").lower()
        servers.append({
            "rank": rank,
            "link": "vmess://" + base64.b64encode(encoded).decode("ascii"),
            "flag": flag_emoji(code),
            "country": country,
            "network": str(config.get("net") or "tcp").lower(),
            "security": {"tls": "TLS", "reality": "REALITY"}.get(security, "no TLS"),
            "port": int(str(config["port"]).strip()),
            "ping": positive_number(entry.get("ping_ms")),
            "speed": positive_number(entry.get("speed_mbps")),
        })
        if len(servers) == limit:
            break
    return servers


def parse_site_counts(page):
    """Counts from the website's protocol tabs, such as {"vless": 1065, "vmess": 346}."""
    counts = {}
    for key in PROTOCOLS:
        tab = re.search(rf'href="/type/{key}"[^>]*>[^<]*<small>\s*(\d[\d,]{{0,8}})\s*</small>', page)
        if tab and (number := int(tab.group(1).replace(",", ""))):
            counts[key] = number
    return counts


def site_counts(root, source):
    """Read the website's counts. If one can't be read, keep the last one we saw."""
    saved = saved_site_counts(root)
    try:
        found = parse_site_counts(read_feed(source))
    except FeedError as error:
        print(f"::warning::website counts unavailable: {error}")
        return saved
    if len(found) < len(PROTOCOLS):
        print("::warning::the website no longer shows every protocol count in the expected form")
    return {**saved, **found}


def saved_site_counts(root):
    try:
        data = json.loads((root / "configs" / "site-stats.json").read_text(encoding="utf-8"))
        return {k: data[k] for k in PROTOCOLS if type(data.get(k)) is int and data[k] > 0}
    except (OSError, ValueError, AttributeError):
        return {}


def code_block(lines):
    return "```text\n" + "\n".join(lines) + "\n```"


def summary_line(server):
    parts = [
        f"<b>#{server['rank']:02d}</b>&nbsp; {server['flag']} <b>{server['country']}</b>",
        f"<code>{server['network']}</code> <code>{server['security']}</code>",
        f"port <code>{server['port']}</code>",
    ]
    if server["speed"]:
        speed = server["speed"]
        parts.append("⚡ 1 Gbps+" if speed >= 1000 else f"⚡ {max(1, round(speed))} Mbps")
    if server["ping"]:
        parts.append(f"📶 {max(1, round(server['ping']))} ms")
    return " &nbsp;·&nbsp; ".join(parts)


def site_headline(counts):
    if not counts:
        return "### 🌐 [Get every tested server on **vpnlk.online** →](https://vpnlk.online/)"
    links = " &nbsp;|&nbsp; ".join(
        f"[{name} {counts[key]}](https://vpnlk.online/type/{key})"
        for key, name in PROTOCOLS.items() if key in counts
    )
    return f"**🌐 Working servers on vpnlk.online**\n\n### {links}"


def configs_section(servers, updated, counts=None):
    total = (counts or {}).get("vmess")
    if not servers:
        return (
            "## 🚀 Latest free VMess configs\n\n"
            "> [!NOTE]\n"
            "> The automatic list is being set up. Until then, copy fresh VMess servers from "
            f"**[vpnlk.online/type/vmess]({VMESS_PAGE})**."
        )
    count = len(servers)
    countries = Counter(f"{s['flag']} {s['country']}" for s in servers)
    breakdown = " &nbsp;·&nbsp; ".join(f"{name} <b>{n}</b>" for name, n in countries.most_common())
    rows = "\n\n".join(
        f"<details>\n<summary>{summary_line(s)}</summary>\n\n{code_block([s['link']])}\n\n</details>"
        for s in servers
    )
    website = []
    if total:
        website = [
            f"> 🌐 This page shows **{count}**. The website lists **{total:,} working VMess servers**: "
            f"**[see them all on vpnlk.online/type/vmess]({VMESS_PAGE})**"
        ]
    return "\n\n".join([
        "## 🔗 Free VMess subscription link",
        "Add this URL as a subscription in **v2rayNG**, **v2rayN** or **V2Box**. "
        "Update the subscription in the app to pull the newest list.",
        code_block([SUB_URL]),
        "## 🚀 Latest free VMess configs",
        f"**{count} free VMess configs** on this page &nbsp;·&nbsp; last changed **{updated}**",
        *website,
        f"<sub>{breakdown}</sub>",
        f"<details>\n<summary><b>📋 Copy all {count} configs at once</b></summary>\n\n"
        f"{code_block([s['link'] for s in servers])}\n\n</details>",
        "### Copy a single V2Ray VMess server",
        "Open a row, then press the copy button at the top right of the box.",
        rows,
    ])


def status_badges(servers, updated):
    if not servers:
        return ""

    def badge(label, message, color):
        text = "-".join(
            urllib.parse.quote(part.replace("-", "--").replace("_", "__"), safe="") for part in (label, message)
        )
        return f"![{label}](https://img.shields.io/badge/{text}-{color}?style=flat-square)"

    return " ".join([badge("VMess on this page", str(len(servers)), "0f766e"), badge("Last changed", updated, "2563eb")])


def render(template, servers, updated, counts=None, year=""):
    values = {
        "{{YEAR}}": str(year),
        "{{SITE_HEADLINE}}": site_headline(counts),
        "{{STATUS_BADGES}}": status_badges(servers, updated),
        "{{CONFIGS}}": configs_section(servers, updated, counts),
    }
    for placeholder, value in values.items():
        template = template.replace(placeholder, value)
    return template


def write_outputs(root, servers, now=None, counts=None):
    """Write README.md and configs/. Return False when nothing but the time would change."""
    template = (root / "templates" / "README.template.md").read_text(encoding="utf-8")
    now = now or datetime.now(timezone.utc)
    digest = hashlib.sha256(render(template, servers, "", counts, now.year).encode("utf-8")).hexdigest()
    marker = f"<!-- content-digest: {digest} -->"
    readme = root / "README.md"
    plain_path = root / "configs" / "vmess.txt"
    sub_path = root / "configs" / "vmess-sub.txt"
    if readme.exists() and marker in readme.read_text(encoding="utf-8"):
        if not servers or (plain_path.exists() and sub_path.exists()):
            return False
    updated = now.strftime("%d %b %Y, %H:%M UTC")
    page = render(template, servers, updated, counts, now.year)
    readme.write_text(page.rstrip() + f"\n\n{marker}\n", encoding="utf-8")
    if servers:
        plain = "".join(s["link"] + "\n" for s in servers)
        plain_path.parent.mkdir(exist_ok=True)
        plain_path.write_text(plain, encoding="utf-8")
        sub_path.write_text(base64.b64encode(plain.encode("ascii")).decode("ascii") + "\n", encoding="ascii")
    if counts:
        stats_path = root / "configs" / "site-stats.json"
        stats_path.parent.mkdir(exist_ok=True)
        stats_path.write_text(json.dumps(counts) + "\n", encoding="utf-8")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", help="feed URL or file path (default: the FEED_URL variable)")
    parser.add_argument("--count-source", help="page to read the website protocol counts from")
    parser.add_argument("--placeholder", action="store_true", help="write the README with no configs")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.placeholder:
        write_outputs(args.root, [])
        return 0

    source = args.source or os.environ.get("FEED_URL", "").strip() or DEFAULT_FEED
    try:
        servers = build_servers(feed_entries(read_feed(source)))
        if not servers:
            raise FeedError("the feed has no usable VMess configs; kept the old list")
    except FeedError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    # A local --source is a test run, so it never reaches out to the website by itself.
    count_source = args.count_source or (None if args.source else VMESS_PAGE)
    counts = site_counts(args.root, count_source) if count_source else saved_site_counts(args.root)
    changed = write_outputs(args.root, servers, counts=counts)
    print(f"{len(servers)} configs; website counts {counts or 'unknown'}; "
          f"{'page updated' if changed else 'no change'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
