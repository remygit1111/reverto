# Vendored third-party static assets

Self-hosted copies of JS/CSS libraries that used to load from
`https://unpkg.com`. Closing PT-v4-NW-009 dropped the unpkg CDN
allowance from the app-side CSP entirely; every library that
the SPA needs now lives here and is served from
`/static/vendor/<library>/`.

## Bundles

| Path | Source | Version | Licence | sha384 (matches the old SRI tag) |
| --- | --- | --- | --- | --- |
| `lightweight-charts/lightweight-charts.standalone.production.js` | `unpkg.com/lightweight-charts@5.1.0/dist/` | 5.1.0 | Apache 2.0 | `ExNwjbclSLY2LS3S2c6aDJDjpBHGZLct23WX65fLzj0ob1bRrCh9H90WThES5wQA` |
| `gridstack/gridstack-all.js`                                     | `unpkg.com/gridstack@11.5.1/dist/`        | 11.5.1 | MIT        | `C6xDcgmIkJjuPEwOp1k2ZZPrQLhSlaD1+c5W0smmrrAnBNLbwzPA4ZCKmV3oN3TU` |
| `gridstack/gridstack.min.css`                                    | `unpkg.com/gridstack@11.5.1/dist/`        | 11.5.1 | MIT        | `fRUp/mDL0+m5arebhwfhn579teBw1KChegdnq0Ga3+JYL20f5wS2vtROixpQBno3` |

The sha384 column is the exact value that used to sit in the
`integrity=` attribute on the unpkg `<script>` / `<link>` tags
in `web/static/index.html` before vendoring. Operators can
regenerate either column with:

```
curl -sL https://unpkg.com/<pkg>@<ver>/dist/<file> \
  | openssl dgst -sha384 -binary | openssl base64 -A
```

Both licences are compatible with Reverto's BSL 1.1 source
licence (vendoring a permissive third-party bundle without
modification is the safe path).

## Upgrading a library

1. Download the new file from `unpkg.com` once (the same
   ergonomic that motivated the original CDN dependency — but
   only for the operator, not for every browser).
2. Replace the file in this directory, drop the old one.
3. Update the version + sha384 row in this README.
4. Bump the version string in `web/static/index.html`'s
   `<script>` tag if you keep a version suffix; otherwise the
   path itself unchanged is enough.
5. Smoke-test the affected pages (charts / workspace
   gridstack) before pushing.

No CSP change is needed when upgrading — the libraries are
served from `'self'` regardless of version. Compare with the
pre-NW-009 world where every CDN URL had to be explicitly
allow-listed and SRI hashes regenerated on every bump.

## Why not a package manager?

We picked `curl + vendor` over `npm install + bundle` because:

* The SPA has no build step today — `index.html` ships a few
  hand-written script tags and that's the entire pipeline.
  Introducing a Node toolchain just to vendor two libraries
  is a bigger change than the finding (NW-009) called for.
* The two libraries we vendor are already shipped pre-built
  and pre-minified from upstream. Re-bundling buys nothing.
* The provenance trail (this README) is the same one a
  package-lock would give us, in a form an operator can read
  in 30 seconds.

If the SPA ever grows a build step (Vite, esbuild, etc.) this
directory disappears and the libraries become regular npm
dependencies.
