# Media manager

A small microservices stack for downloading and organizing video content,
with a Jellyfin-shaped library layout for playlists.

## Stack

- **gateway** (Traefik) - single entrypoint, routes `/` to the web UI and
  `/downloads/*` to the API
- **web** - static page: paste a URL, watch it download, browse playlists
- **download-api** - FastAPI service, accepts jobs and reports status
- **download-worker** - RQ worker (SimpleWorker, no forking) that actually
  runs yt-dlp
- **redis** - job queue
- **postgres** - job/playlist tracking

## Run it

```bash
cp .env.example .env   # or .env.shurikloud.example on ShurIKloud
# edit .env: DOWNLOAD_PATH, SHOWS_PATH, PUID/PGID, ports
docker compose up -d --build
```

Open `http://localhost:<GATEWAY_PORT>/`.

## Playlists -> Jellyfin shows

A playlist URL gets its own folder tree:
```
SHOWS_PATH/<Playlist Name>/Season 01/<Playlist Name> S01E01 - <Title>.mp4
SHOWS_PATH/<Playlist Name>/Season 01/<Playlist Name> S01E01 - <Title>.nfo
```
Once a playlist's last video finishes, a `.posters-pending` trigger file is
written to `SHOWS_PATH`. Poster generation itself runs natively on the NAS
host (not in this container - see the ShurIKloud env template for why),
via a scheduled Unraid User Script that watches for that trigger.

## Self-test

The "Test pipeline" button (top right of the page) runs one real standalone
download and one real 2-video playlist through the actual pipeline, using
a short, stable public video ("Me at the zoo"). It polls until both finish,
shows pass/fail (with the actual error if something failed), then calls
`/self-test/cleanup` to delete the downloaded file(s) and remove the test
entries from history. Nothing from a passing OR failing test run is left
behind, on disk or in the job list.

## Known environment quirks (ShurIKloud/Unraid)

- Unraid shares often default to `root`-owned folders even when parent
  directories look fine - if you hit `PermissionError`/`Access denied`,
  check ownership on the *specific* folder being written to, recursively:
  `chown -R nobody:users <path> && chmod -R 775 <path>`
- Traefik needs a fairly recent version (`v3.6+`) to talk to modern Docker
  Engine's API - older tags fail with a `client version too old` error.
- `.env` changes require `docker compose up -d --force-recreate <service>`
  to actually take effect - editing the file alone doesn't touch a
  container that's already running.
