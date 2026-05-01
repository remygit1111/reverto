# Marketing site

This directory contains the source files for the static
marketing site served at `https://reverto.bot`.

## Structure

- `index.html` — landing page (placeholder for now)
- `data/` — auto-generated JSON snapshots from the app
  (roadmap.json, changelog.json) — populated by PR 2

## Deploy

Use the `make deploy-marketing` target:

```
make deploy-marketing
```

This rsyncs the contents of this directory to
`/var/www/reverto-marketing/` on the production VPS, sets
ownership to `caddy:caddy`, and sets correct permissions.

The target is intended to run **on the VPS** (it uses local
`sudo chown` / `sudo chmod`). From Reverto-Dev (WSL2), SSH to
the VPS first and run `make deploy-marketing` there. A
WSL2-to-VPS deploy variant may be added in a later
operator-tooling iteration.

## Architecture

The marketing site is intentionally simple — pure HTML, CSS,
and minimal JS. No Python, no database, no authentication.
This minimizes attack surface and makes the site fast to
load and easy to maintain.

For the application itself (login required), visit
`https://app.reverto.bot`.
