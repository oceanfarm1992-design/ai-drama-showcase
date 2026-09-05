# AI Drama — Public Showcase

A static site (GitHub Pages) that lists generated episodes/series with embedded
video players. It contains **no pipeline code, prompts, or credentials** — it
only fetches `manifest.json` from Cloudflare R2 at page load and renders
whatever is in it.

## How it connects to the private pipeline

The private repo's Modal pipeline (`modal_drama_generator.py`) writes/updates
`manifest.json` in the same R2 bucket as the rendered videos after every
episode (see `update_episode_manifest` in that file). This site never talks to
the private repo, Modal, or any secret — R2 is the only shared surface between
the two, and it only shares public, read-only data.

## Setup

1. Edit `config.js` and set `MANIFEST_URL` to your R2 bucket's public URL +
   `/manifest.json` (the same `R2_PUBLIC_BASE_URL` used in the private repo's
   `r2-credentials` secret).
2. **Enable CORS on the R2 bucket** so a browser on your Pages domain can fetch
   `manifest.json` and play the videos. In the Cloudflare dashboard: R2 →
   your bucket → Settings → CORS Policy, allow `GET` from your Pages origin
   (e.g. `https://<you>.github.io`).
3. Enable GitHub Pages for this repo (Settings → Pages → deploy from `main`,
   root folder). No build step needed — it's plain HTML/CSS/JS.

## Why a separate public repo

Keeps series premises, character-continuity prompts, and pipeline internals
private, while giving viewers/collaborators a public link to browse episodes,
without either repo needing to know about the other beyond the shared R2
manifest.
