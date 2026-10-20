# qPlay catalog schema

The canonical schema is initialized by
`/home/ceaser/Quark/Quark_TV/src/library/media_library.cpp` and documented in
the project at `docs/MEDIA_CATALOG.md`. The database is
`~/.local/share/qPlay/library.sqlite3`.

The helper reads `[webdav] url`, `username`, and `password`, plus `[tmdb]
apiToken` and `apiKey` from `~/.config/qPlay/user-config.ini`. It prefers the
API Read Access Token as `Authorization: Bearer ...`; if that is empty it uses
the v3 API key as the `api_key` query parameter. Never include credentials in
catalog URLs.

## Import fields

Each `media` record uses these fields (legacy imports may pass the records as a
top-level JSON array):

```json
[
  {
    "source": "http://alist-host/dav/Quark/TV/Series/S01E01.mkv",
    "title": "Series title",
    "tmdb_id": 12345,
    "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
    "backdrop_url": "https://image.tmdb.org/t/p/w1280/backdrop.jpg",
    "rating": 8.2,
    "overview": "Localized synopsis",
    "release_date": "2020-01-01",
    "media_type": "episode",
    "series_title": "Series title",
    "season_number": 1,
    "season_name": "Season name from TMDB when available",
    "episode_number": 1,
    "episode_title": "Episode title",
    "still_url": "https://image.tmdb.org/t/p/w300/episode-still.jpg"
  }
]
```

For a movie, set `media_type` to `movie`, `series_title` and `episode_title`
to empty strings, and season/episode numbers to zero. For unmatched records,
use zero for `tmdb_id` and `rating`, empty image/overview/date strings, and
keep the title/path-derived fields that can be identified confidently.

## Work genres

Genres are stored once per matched work in `work_genres`, not repeated across
each media file or episode. The table columns are:

| Column | Meaning |
| --- | --- |
| `work_type` | `movie` or `tv`. Episode media rows map to the parent `tv` work. |
| `tmdb_id` | Movie ID for a movie, or series ID for a TV show. |
| `genre_id` | TMDB genre ID. |
| `genre_name` | Localized genre name returned by TMDB (the helper supplies Chinese names for TV genre IDs 10765 and 10768 when TMDB returns English labels). |

Its primary key is `(work_type, tmdb_id, genre_id)`, so a work can have many
genres while movie and TV IDs remain distinct. Genre metadata is imported in
the same transaction as media metadata.

New imports should use this object format. Keep one media object per actual
video file, and one work object per distinct matched movie or TV series:

```json
{
  "media": [
    {
      "source": "http://alist-host/dav/Quark/TV/Series/S01E01.mkv",
      "title": "Series title",
      "tmdb_id": 12345,
      "media_type": "episode",
      "series_title": "Series title",
      "season_number": 1,
      "episode_number": 1
    }
  ],
  "works": [
    {
      "work_type": "tv",
      "tmdb_id": 12345,
      "genres": [
        {"id": 18, "name": "剧情"},
        {"id": 10765, "name": "科幻与奇幻"}
      ]
    }
  ]
}
```

The importer also accepts the legacy JSON array of media objects. Legacy
imports do not modify existing genre assignments. For a work explicitly
included in `works`, an empty `genres` array clears its genre assignments.
Unmatched works should be omitted from `works`; do not invent TMDB genres.

## Write rules

- Use `source` as the conflict key. It must be the exact URL returned by the
  WebDAV listing and must not contain a username or password.
- Only update scraper-owned metadata columns: `title`, `tmdb_id`,
  `poster_url`, `backdrop_url`, `rating`, `overview`, `release_date`,
  `media_type`, `series_title`, `season_number`, `season_name`, `episode_number`, and
  `episode_title`, and `still_url`.
- Preserve `position_ms`, `play_count`, `favorite`, `last_opened_ms`,
  `duration_ms`, `format_name`, `bitrate`, `width`, `height`, and `created_ms`.
- Update work genres through `work_genres`, keyed by movie/TV work type, TMDB
  work ID, and TMDB genre ID. Do not repeat TV genres as separate rows for each
  episode.
- Commit a complete batch in one SQLite transaction. qPlay uses WAL mode and
  reloads the catalog when restarted.

## Helper commands

```text
python3 scripts/qplay_catalog.py list [--limit N]
python3 scripts/qplay_catalog.py search movie|tv TITLE
python3 scripts/qplay_catalog.py details movie|tv TMDB_ID
python3 scripts/qplay_catalog.py episode TMDB_SERIES_ID SEASON EPISODE
python3 scripts/qplay_catalog.py sync-season-names TMDB_SERIES_ID
python3 scripts/qplay_catalog.py sync-episode-details TMDB_SERIES_ID
python3 scripts/qplay_catalog.py check-tmdb
python3 scripts/qplay_catalog.py migrate-genres
python3 scripts/qplay_catalog.py import METADATA.json
```

`list` emits credential-free media URLs and relative paths. `search` emits
candidate TMDB IDs and identification fields. `details` returns one matched
movie or TV work with its localized genres. `migrate-genres` creates the
`work_genres` table without scanning WebDAV or contacting TMDB. `episode` returns a TMDB
episode's title, overview, air date, rating, and still path.
`sync-season-names` obtains TMDB season names and updates only the matching
series' season names. `sync-episode-details` fetches TMDB season data and updates
existing episode titles, localized overviews, and still image URLs for that
series only.
`import` validates URLs
against the configured WebDAV root and transactionally upserts metadata.
