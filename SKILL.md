---
name: qplay-media-library
description: Build and refresh the qPlay media catalog from an AList WebDAV library, matching movies and TV episodes with TMDB and storing metadata in qPlay's SQLite database. Use when the user asks to scan, identify, scrape, or update their qPlay catalog; not for playback control.
---

# qPlay media library

Use this skill to maintain the catalog consumed by qPlay. Cataloging is a
separate agent workflow: qPlay itself only reads catalog rows, records watch
progress, displays posters/details, and plays saved WebDAV URLs.

Read `~/.config/qPlay/user-config.ini` for the `[webdav]` collection URL,
username and password, and `[tmdb] apiToken` / `apiKey`. Never print credentials,
place them in catalog URLs, or include them in user-facing output. Prefer the
TMDB API Read Access Token as a Bearer credential; the v3 API key is a supported
fallback. The default catalog
database is `~/.local/share/qPlay/library.sqlite3`.

## Workflow

1. Read [catalog-schema.md](references/catalog-schema.md) before importing.
2. Run `python3 scripts/qplay_catalog.py list` to enumerate video paths under the
   configured WebDAV root. This sends read-only `PROPFIND` requests and does
   not download media files.
3. Analyze directory and file names to distinguish movies, series, seasons,
   and episodes. Use the TMDB search and TV episode endpoints through the
   helper; compare localized and original titles, years, season/episode
   numbers, and directory context. Do not assign a weak or ambiguous match.
   Leave unmatched rows without TMDB metadata and report them for later review.
4. For TV episodes, use the TMDB episode response's title, localized overview,
   and `still_path`; store a `still_url` using the TMDB `w300` image size, or an
   empty value when TMDB has no still. Create a JSON metadata batch with one
   item per video file and pass it to
   `python3 scripts/qplay_catalog.py import <file>`. Import preserves qPlay's playback
   progress, favorites, and technical media fields. Use the credential-free
   WebDAV `source` value from the listing unchanged.
5. Summarize imported, matched, unmatched, and ambiguous counts without
   revealing credentials. qPlay loads catalog changes on startup; restart it
   after importing to refresh the poster wall.

Before a scrape, run `python3 scripts/qplay_catalog.py check-tmdb`. It performs
a read-only `/configuration` call and reports whether authentication succeeded
without showing either credential. Search each movie with `/search/movie` and each
series with `/search/tv`; retrieve episode details from
`/tv/{series_id}/season/{season_number}/episode/{episode_number}` when the path
identifies an episode. Use its `name`, `overview`, and `still_path` for the
episode fields. Retrieve the show's season list from `/tv/{series_id}`
and write each TMDB season's `name` into `season_name` for every episode in that
season. Use the season number parsed from the file path independently for every
episode; never infer one season for a whole series directory. Populate poster
and backdrop URLs using TMDB image paths (`w500` for posters and `w1280` for
backdrops).

To populate or refresh season names for an already-imported show without
changing its episode numbers or other metadata, run
`python3 scripts/qplay_catalog.py sync-season-names TMDB_SERIES_ID`.
To refresh episode titles, localized overviews, and still images for an
already-imported series, run
`python3 scripts/qplay_catalog.py sync-episode-details TMDB_SERIES_ID`.

Do not scan or import the whole library unless the user requested a library
refresh. Never alter or remove WebDAV files. If WebDAV access or TMDB lookup
fails, stop and report the failing stage without writing partial metadata.
