"""Screenshots of the trace viewer, and the secret check on its API, against a running dev server.

    uv run --with playwright python scripts/trace_shots.py --mode mock   # NEXT_PUBLIC_DATA=mock
    uv run --with playwright python scripts/trace_shots.py --mode real   # .env.local, read-only
    uv run --with playwright python scripts/trace_shots.py --mode real --check-only

Headless Chromium, desktop viewport 1440x900, nothing played. Real mode mints the studio cookie
with the app's own signAccess (lib/server/accessCookie.ts, run by node) from ACCESS_COOKIE_SECRET
in apps/web/.env.local; the secret and the cookie are never printed. The check fetches the job
list, the Bronco trace and every raw manifest of it, and prints how many responses match a secret
pattern (the count only).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "apps" / "web"
BRONCO = "1785a480-84b8-41f9-8f97-1005b6d7d0a6"
SECRET_RE = re.compile(r"Signature=|Expires=|token=|eyJ[A-Za-z0-9_-]{10,}|msy_|sk-ant|sb_secret")

MINT = """
import { readFileSync } from "node:fs";
import { signAccess } from "%s";
const env = Object.fromEntries(readFileSync("%s", "utf8").split(/\\r?\\n/)
  .filter((l) => /^\\s*[A-Z0-9_]+\\s*=/.test(l))
  .map((l) => { const i = l.indexOf("="); return [l.slice(0, i).trim(),
    l.slice(i + 1).trim().replace(/^["']|["']$/g, "")]; }));
process.stdout.write(await signAccess(env.ACCESS_COOKIE_SECRET, Date.now(), 3600));
"""


def mint_cookie() -> str:
    src = MINT % ((WEB / "lib/server/accessCookie.ts").as_posix(), (WEB / ".env.local").as_posix())
    out = subprocess.run(["node", "--input-type=module", "-e", src], cwd=WEB, capture_output=True,
                         text=True, check=False)
    if out.returncode != 0 or not out.stdout.startswith("v1."):
        sys.exit("could not mint the access cookie (node signAccess failed)")
    return out.stdout.strip()


def get(base: str, path: str, cookie: str | None) -> tuple[int, str]:
    req = urllib.request.Request(base + path)
    if cookie:
        req.add_header("cookie", f"p2p_access={cookie}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def check(base: str, cookie: str | None) -> int:
    paths = ["/api/admin/traces", f"/api/admin/traces/{BRONCO}", "/api/admin/traces/calls"]
    status, text = get(base, f"/api/admin/traces/{BRONCO}", cookie)
    if status == 200:
        for a in json.loads(text)["attempts"]:
            paths.append(f"/api/admin/traces/{BRONCO}/raw?stage={a['stage']}&attempt={a['attempt']}")
            for c in a["calls"]:
                if c.get("path"):
                    paths.append(f"/api/admin/traces/{BRONCO}/body?path={c['path']}")
    total, fetched = 0, 0
    for p in paths:
        s, body = get(base, p, cookie)
        n = len(SECRET_RE.findall(body))
        total += n
        fetched += 1
        print(f"{s} {len(body):>7} bytes {n} matches {p.split('?')[0]}"
              f"{'?' + p.split('?')[1].split('&')[0] if '?' in p else ''}")
    print(f"secret pattern matches across {fetched} responses: {total}")
    return total


def shots(base: str, mode: str, out: Path, cookie: str | None) -> None:
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--mute-audio", "--autoplay-policy=user-gesture-required"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1,
                                  color_scheme="light")
        if cookie:
            ctx.add_cookies([{"name": "p2p_access", "value": cookie, "url": base}])
        page = ctx.new_page()

        def settle():
            page.wait_for_load_state("networkidle")
            # screenshots only: no dev-mode badge, and the sticky nav stays at the top of a
            # full-page capture instead of repeating mid-page
            page.add_style_tag(content="nextjs-portal{display:none!important}"
                                       "header.sticky{position:static!important}")
            page.evaluate("() => Promise.all([...document.images].map(i => i.complete ? 0 :"
                          " new Promise(r => { i.onload = i.onerror = r; })))")
            page.wait_for_timeout(400)

        page.goto(f"{base}/admin/traces")
        page.get_by_test_id(f"trace-job-{BRONCO}").wait_for(timeout=60_000)
        settle()
        page.screenshot(path=str(out / f"{mode}_jobs.png"))

        page.goto(f"{base}/admin/traces/{BRONCO}")
        page.get_by_test_id("trace-totals").wait_for(timeout=60_000)
        calls = page.get_by_test_id("trace-call")
        calls.nth(0).click()  # intake
        page.get_by_test_id("trace-call-detail").first.wait_for()
        settle()
        page.screenshot(path=str(out / f"{mode}_bronco.png"))
        if calls.count() > 1:
            calls.nth(1).click()  # concept r1c1
            settle()
        page.screenshot(path=str(out / f"{mode}_bronco_full.png"), full_page=True)

        page.goto(f"{base}/admin/traces/calls")
        page.get_by_test_id("call-row").first.wait_for(timeout=60_000)
        settle()
        page.screenshot(path=str(out / f"{mode}_calls.png"))
        browser.close()
    for f in sorted(out.glob(f"{mode}_*.png")):
        print(f.relative_to(REPO))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["mock", "real"], required=True)
    ap.add_argument("--base", default="http://localhost:3217")
    ap.add_argument("--out", default=str(REPO / "docs/plans/telemetry_shots"))
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args()
    cookie = mint_cookie() if args.mode == "real" else None
    matches = check(args.base, cookie)
    if not args.check_only:
        shots(args.base, args.mode, Path(args.out), cookie)
    return 1 if matches else 0


if __name__ == "__main__":
    sys.exit(main())
