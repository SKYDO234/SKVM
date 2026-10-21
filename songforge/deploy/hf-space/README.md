---
title: songforge
emoji: 🎵
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Give it a vocal, get a finished song back
---

# songforge — vocal in, song out

Upload a vocal stem. It is analysed (tempo, key, melody, structure), a backing
track is composed against those constraints, and both are mixed and mastered
into a finished song.

This Space runs the CPU backend (`procedural`). To run MusicGen or your own
trained checkpoint, use a GPU host — see `DEPLOY.md` in the repository.

Definitions and limits are controlled by the Space variables:
`SONGFORGE_MAX_SECONDS`, `SONGFORGE_MAX_UPLOAD_MB`, `SONGFORGE_WORKERS`,
`SONGFORGE_TTL_HOURS`, `SONGFORGE_API_KEY`.
