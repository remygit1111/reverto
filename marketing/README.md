# Marketing site

Source files for the static marketing site served at
`https://reverto.bot`. Pure HTML/CSS/JS; no Python, no database,
no authentication.

## Structure

- `index.html` — landing page with hero + About section
- `roadmap.html` — roadmap timeline (renders `/data/roadmap.json`)
- `changelog.html` — changelog list (renders `/data/changelog.json`)
- `maintenance.html` — 5xx fallback served by Caddy `handle_errors`
- `css/marketing.css` — site styling (self-contained; ships its
  own design tokens — does not depend on the app's stylesheet)
- `js/render.js` — JSON-to-DOM render logic for roadmap +
  changelog (mirrors `web/static/app.js::_rmRenderPhase` and
  `_clRenderEntry`)

## Deploy

Use the `make deploy-marketing` target:

```
make deploy-marketing
```

This rsyncs the contents of this directory to
`/var/www/reverto-marketing/` on the production VPS, sets
ownership to `caddy:caddy`, and applies 755/644 permissions.

The target is intended to run **on the VPS** (it uses local
`sudo chown` / `sudo chmod`). From Reverto-Dev (WSL2), SSH to
the VPS first and run `make deploy-marketing` there.

The `data/` subdirectory on the VPS at
`/var/www/reverto-marketing/data/` is **not** part of this
repo. It is created during PR 2 deploy with `bot:bot 755`
ownership so the FastAPI process can write JSON snapshots
there while Caddy (running as `caddy:caddy`) reads them.

## Snapshots

Roadmap and changelog content is auto-exported by the app to
`/var/www/reverto-marketing/data/`:

- Whenever an admin publishes, unpublishes, edits, deletes, or
  reorders a roadmap phase, `data/roadmap.json` is regenerated.
- Whenever an admin publishes, unpublishes, edits, or deletes a
  changelog entry, `data/changelog.json` is regenerated.

The writes are best-effort: a failure logs `WARNING` /
`ERROR` to portal.log but does NOT block the DB mutation —
the database is the source of truth.

If the snapshots drift from the database (e.g. after a
transient permissions issue), an admin can hit the
**Regenerate marketing snapshots** button on the admin Roadmap
page, which calls `POST /api/admin/marketing/regenerate` and
force-rewrites both files.

## Snapshot shape

Both JSON files mirror what the in-app SPA's public API
endpoints (`/api/roadmap` and `/api/changelog`) return — same
fields, same `body_html` / `description_html` pre-rendered
through bleach. The marketing site's `render.js` and the app
SPA share the same render conventions for that reason.

## Architecture

The marketing site is intentionally simple. No Python, no
database, no authentication — it minimises attack surface and
keeps the public-facing reverto.bot fast and easy to maintain.

For the application itself (login required), visit
`https://app.reverto.bot`.
