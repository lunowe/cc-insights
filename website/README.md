# CC-Insights landing page

A static marketing page for CC-Insights. No build step, no backend, no
framework: `index.html`, `styles.css`, `main.js` and `favicon.svg`. Deploy the
directory as-is to any static host (GitHub Pages, Netlify, Cloudflare Pages,
an S3 bucket, `python -m http.server`).

## Preview

From the repository root:

```bash
python3 -m http.server 8080 --directory website
# then open http://localhost:8080
```

Opening `index.html` straight from disk also works, with one caveat: the
copy-to-clipboard buttons need a secure context, so on `file://` they fall back
to the older `execCommand` path, which most browsers still honour. Over
`http://localhost` everything works as it does in production.

Fonts are loaded from Google Fonts (Newsreader, IBM Plex Sans, IBM Plex Mono).
Offline, the page falls back to the system serif, sans and mono stacks.

Light and dark follow the operating system's `prefers-color-scheme`. There is
no toggle on the page by design.

## Deploy

The site runs as the `website` service in the `cc-insights` Railway project,
at https://website-production-779a.up.railway.app. Railpack recognises a
static site from `index.html` and serves it with Caddy, so the directory
needs no build configuration. Deploy from the repository root:

```bash
railway up website --path-as-root --service website
```

Leave out `--path-as-root` and Railway uploads the whole repository to that
service. Leave out `--service` and the upload goes to whichever service the
checkout is linked to, which may be the account server.

## The agent setup prompt

The "Set it up with your coding agent" block embeds the text of
`docs/agent-setup-prompt.md`. It is held in exactly one place: the
`<pre id="agent-prompt">` element in `index.html`, marked with an HTML comment
above it. After editing the prompt, run `python3 website/sync_prompt.py`,
which copies the file in verbatim and escapes it. The copy button reads the
element's text, so nothing else needs to change.

## Screenshots

Every figure on the page is hand-drawn SVG/CSS with synthetic or published
example numbers from the README. Nothing is rendered from a real database.
To take verification screenshots, any headless browser works, for example:

```bash
npx playwright screenshot --viewport-size=1440,900 --full-page http://localhost:8080 desktop-light.png
npx playwright screenshot --viewport-size=390,844  --full-page --color-scheme=dark http://localhost:8080 mobile-dark.png
```
