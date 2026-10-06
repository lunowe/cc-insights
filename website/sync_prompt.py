"""Copy docs/agent-setup-prompt.md into the <pre id="agent-prompt"> block of index.html.

Run after editing the prompt: python3 website/sync_prompt.py
"""
import html
import re
from pathlib import Path

root = Path(__file__).resolve().parent.parent
page = root / "website" / "index.html"
prompt = (root / "docs" / "agent-setup-prompt.md").read_text().rstrip("\n")

src = page.read_text()
pattern = re.compile(r'(<pre class="prompt" id="agent-prompt"[^>]*>)(.*?)(</pre>)', re.S)
if len(pattern.findall(src)) != 1:
    raise SystemExit("expected exactly one <pre id=\"agent-prompt\"> block in index.html")
out = pattern.sub(lambda m: m.group(1) + html.escape(prompt, quote=False) + m.group(3), src)
page.write_text(out)
print("in sync" if out == src else "updated website/index.html")
