"""`GET /join/{code}`: what a join link shows when somebody opens it in a browser.

`cci team invite` hands out `<server>/join/<code>` rather than a bare code,
because the link carries the one thing a fresh machine is missing -- which
server to sign in to -- and `cci team join <link>` reads it from there. A
link also gets clicked, so the server answers the click with how to use it.

The page never looks the code up. It renders the same thing for a live code,
a spent one and one that never existed, for the reason `invites.redeem`
gives one 404 for all of them: telling a stranger that a code is real tells
them the team is. Only the shape is checked, so arbitrary text is never
echoed into the page.

The address holds a bearer secret, so the response is not cached, not
indexed, and sends no Referer anywhere.
"""

from __future__ import annotations

import html
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from cci_server.invites import INVITE_PREFIX

router = APIRouter()

INSTALL_SCRIPT = "https://raw.githubusercontent.com/lunowe/cc-insights/master/scripts/install.sh"

_CODE_SHAPE = re.compile(rf"^{re.escape(INVITE_PREFIX)}[A-Za-z0-9_-]{{20,100}}$")

_SECRET_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
}


def _public_url(request: Request) -> str:
    """The address the browser used. Behind a TLS-terminating proxy (Railway)
    the app itself sees plain http, so the forwarded scheme wins."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}{request.url.path}"


@router.get("/join/{code}", response_class=HTMLResponse, include_in_schema=False)
def join_page(code: str, request: Request) -> HTMLResponse:
    if not _CODE_SHAPE.match(code):
        return HTMLResponse("Not found.\n", status_code=404, headers=_SECRET_HEADERS)
    link = html.escape(_public_url(request))
    script = html.escape(INSTALL_SCRIPT)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<title>CC-Insights team invite</title>
<style>
 body {{ font: 16px/1.55 system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1.25rem; color: #1d1b18; background: #faf8f4; }}
 @media (prefers-color-scheme: dark) {{ body {{ color: #ece8e1; background: #151412; }} pre {{ background: #22201d; }} }}
 h1 {{ font-size: 1.4rem; }}
 pre {{ background: #efebe4; padding: .75rem 1rem; border-radius: 6px; overflow-x: auto; white-space: pre-wrap; word-break: break-all; }}
 small {{ opacity: .75; }}
</style></head><body>
<h1>You've been invited to a CC-Insights team</h1>
<p>Run this on the machine where you use your coding agents:</p>
<pre>cci team join {link}</pre>
<p>No <code>cci</code> there yet? This installs it and joins in one go:</p>
<pre>curl -fsSL {script} | bash -s -- --join {link}</pre>
<p>You will be asked to approve a sign-in in your browser. Joining shares
nothing of your own: you choose what to send with <code>cci publish</code>.</p>
<p><small>This link works like a password: anyone holding it can join the team.
It is single-use unless the person who sent it said otherwise.</small></p>
</body></html>
"""
    return HTMLResponse(page, headers=_SECRET_HEADERS)
