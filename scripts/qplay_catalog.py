#!/usr/bin/env python3
"""Enumerate, query metadata for, and import a qPlay AList WebDAV catalog."""

from __future__ import annotations

import argparse
import base64
import configparser
import http.client
import json
import posixpath
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


CONFIG_PATH = Path.home() / ".config/qPlay/user-config.ini"
DATABASE_PATH = Path.home() / ".local/share/qPlay/library.sqlite3"
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".m2ts",
    ".mpg", ".mpeg", ".wmv", ".flv",
}
DAV = "{DAV:}"


def read_config() -> tuple[str, str, str, str, str]:
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(CONFIG_PATH, encoding="utf-8"):
        raise RuntimeError(f"User config file not found: {CONFIG_PATH}")
    root = config.get("webdav", "url", fallback="").strip()
    username = config.get("webdav", "username", fallback="").strip()
    password = config.get("webdav", "password", fallback="")
    tmdb_key = config.get("tmdb", "apiKey", fallback="").strip()
    tmdb_token = config.get("tmdb", "apiToken", fallback="").strip()
    parsed = urllib.parse.urlsplit(root)
    if not parsed.scheme or not parsed.netloc or not parsed.path:
        raise RuntimeError("Set a valid [webdav] url in the qPlay user config.")
    if parsed.username or parsed.password:
        raise RuntimeError("Keep WebDAV credentials in their config fields, not in the URL.")
    root = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/", "", ""))
    return root, username, password, tmdb_token, tmdb_key


def is_under_root(url: str, root: str) -> bool:
    candidate = urllib.parse.urlsplit(url)
    base = urllib.parse.urlsplit(root)
    if (candidate.scheme.lower(), candidate.hostname, candidate.port) != (
        base.scheme.lower(), base.hostname, base.port
    ):
        return False
    path = posixpath.normpath(urllib.parse.unquote(candidate.path))
    root_path = posixpath.normpath(urllib.parse.unquote(base.path))
    root_path = root_path.rstrip("/")
    return path == root_path or path.startswith(root_path + "/")


def propfind(url: str, username: str, password: str) -> bytes:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    body = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/>'
        b'<d:getcontentlength/></d:prop></d:propfind>'
    )
    request = urllib.request.Request(
        url,
        data=body,
        method="PROPFIND",
        headers={
            "Authorization": f"Basic {token}",
            "Depth": "1",
            "Content-Type": "application/xml; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status not in (200, 207):
                raise RuntimeError(f"WebDAV returned HTTP {response.status}.")
            return response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"WebDAV PROPFIND failed with HTTP {error.code}.") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"WebDAV connection failed: {error.reason}") from None


def list_media(limit: int) -> list[dict[str, str]]:
    root, username, password, _, _ = read_config()
    queue = [root]
    visited: set[str] = set()
    files: list[dict[str, str]] = []
    while queue and len(files) < limit:
        folder = queue.pop(0)
        if folder in visited:
            continue
        visited.add(folder)
        document = ET.fromstring(propfind(folder, username, password))
        for response in document.findall(f"{DAV}response"):
            href_node = response.find(f"{DAV}href")
            if href_node is None or not href_node.text:
                continue
            item_url = urllib.parse.urljoin(folder, href_node.text)
            if not is_under_root(item_url, root):
                continue
            if item_url.rstrip("/") == folder.rstrip("/"):
                continue
            propstat = response.find(f"{DAV}propstat/{DAV}prop")
            is_collection = propstat is not None and propstat.find(f"{DAV}resourcetype/{DAV}collection") is not None
            if is_collection:
                queue.append(item_url.rstrip("/") + "/")
                continue
            path = urllib.parse.unquote(urllib.parse.urlsplit(item_url).path)
            if Path(path).suffix.lower() in VIDEO_EXTENSIONS:
                files.append({
                    "source": item_url,
                    "path": path,
                    "relative_path": path[len(urllib.parse.unquote(urllib.parse.urlsplit(root).path)):].lstrip("/"),
                    "filename": Path(path).name,
                })
                if len(files) >= limit:
                    break
    return files


def tmdb_request(path: str, params: dict[str, str] | None = None) -> dict:
    _, _, _, token, key = read_config()
    if not token and not key:
        raise RuntimeError("Set [tmdb] apiToken or apiKey in the qPlay user config before TMDB lookup.")
    query = {"language": "zh-CN"}
    if params:
        query.update(params)
    headers = {"Accept": "application/json", "User-Agent": "qPlay-catalog-agent/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        query["api_key"] = key
    request = urllib.request.Request(
        "https://api.themoviedb.org/3/" + path.lstrip("/") + "?" + urllib.parse.urlencode(query),
        headers=headers,
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"TMDB request failed with HTTP {error.code}.") from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError):
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError("TMDB connection failed after retries.") from None


def search_tmdb(kind: str, title: str) -> dict:
    endpoint = "search/movie" if kind == "movie" else "search/tv"
    result = tmdb_request(endpoint, {"query": title, "include_adult": "false"})
    candidates = []
    for item in result.get("results", [])[:12]:
        candidates.append({
            "id": item.get("id"),
            "title": item.get("title") or item.get("name"),
            "original_title": item.get("original_title") or item.get("original_name"),
            "release_date": item.get("release_date") or item.get("first_air_date"),
            "overview": item.get("overview", ""),
            "rating": item.get("vote_average", 0),
            "vote_count": item.get("vote_count", 0),
            "poster_path": item.get("poster_path"),
            "backdrop_path": item.get("backdrop_path"),
        })
    return {"results": candidates}


def title_details(kind: str, tmdb_id: int) -> dict:
    if kind not in ("movie", "tv"):
        raise RuntimeError("Title kind must be 'movie' or 'tv'.")
    endpoint = f"movie/{tmdb_id}" if kind == "movie" else f"tv/{tmdb_id}"
    data = tmdb_request(endpoint)
    genres = []
    for genre in data.get("genres", []):
        genre_id = int(genre.get("id") or 0)
        genre_name = str(genre.get("name") or "").strip()
        if kind == "tv":
            genre_name = {
                10765: "科幻与奇幻",
                10768: "战争与政治",
            }.get(genre_id, genre_name)
        if genre_id > 0 and genre_name:
            genres.append({"id": genre_id, "name": genre_name})
    return {
        "work_type": kind,
        "tmdb_id": int(data.get("id") or tmdb_id),
        "title": data.get("title") or data.get("name") or "",
        "genres": genres,
    }


def episode_details(series_id: int, season: int, episode: int) -> dict:
    return tmdb_request(f"tv/{series_id}/season/{season}/episode/{episode}")


def sync_season_names(series_id: int) -> dict:
    show = tmdb_request(f"tv/{series_id}")
    seasons = [
        {
            "season_number": int(season.get("season_number") or 0),
            "season_name": str(season.get("name") or "").strip(),
        }
        for season in show.get("seasons", [])
    ]
    connection = open_database()
    updated = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        for season in seasons:
            cursor = connection.execute(
                "UPDATE media SET season_name=? WHERE tmdb_id=? AND media_type='episode' AND season_number=?",
                (season["season_name"], series_id, season["season_number"]),
            )
            updated += cursor.rowcount
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"series_id": series_id, "episodes_updated": updated, "seasons": seasons}


def sync_episode_details(series_id: int) -> dict:
    connection = open_database()
    updated = 0
    images = 0
    try:
        catalog_seasons = [
            row[0] for row in connection.execute(
                "SELECT DISTINCT season_number FROM media "
                "WHERE tmdb_id=? AND media_type='episode' ORDER BY season_number",
                (series_id,),
            )
        ]
        image_base = "https://image.tmdb.org/t/p/w300"
        episodes = []
        for season_number in catalog_seasons:
            details = tmdb_request(f"tv/{series_id}/season/{season_number}")
            for episode in details.get("episodes", []):
                still_path = str(episode.get("still_path") or "").strip()
                episodes.append({
                    "season_number": season_number,
                    "episode_number": int(episode.get("episode_number") or 0),
                    "episode_title": str(episode.get("name") or "").strip(),
                    "overview": str(episode.get("overview") or "").strip(),
                    "still_url": image_base + still_path if still_path else "",
                })

        connection.execute("BEGIN IMMEDIATE")
        for episode in episodes:
            cursor = connection.execute(
                "UPDATE media SET "
                "episode_title=CASE WHEN ?<>'' THEN ? ELSE episode_title END, "
                "overview=CASE WHEN ?<>'' THEN ? ELSE overview END, "
                "still_url=CASE WHEN ?<>'' THEN ? ELSE still_url END "
                "WHERE tmdb_id=? AND media_type='episode' AND season_number=? AND episode_number=?",
                (
                    episode["episode_title"], episode["episode_title"],
                    episode["overview"], episode["overview"],
                    episode["still_url"], episode["still_url"],
                    series_id, episode["season_number"], episode["episode_number"],
                ),
            )
            updated += cursor.rowcount
            if episode["still_url"]:
                images += cursor.rowcount
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "series_id": series_id,
        "episodes_from_tmdb": len(episodes),
        "catalog_rows_updated": updated,
        "rows_with_stills": images,
    }


def open_database() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("""CREATE TABLE IF NOT EXISTS media (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL DEFAULT '',
        format_name TEXT NOT NULL DEFAULT '',
        duration_ms INTEGER NOT NULL DEFAULT 0,
        bitrate INTEGER NOT NULL DEFAULT 0,
        width INTEGER NOT NULL DEFAULT 0,
        height INTEGER NOT NULL DEFAULT 0,
        position_ms INTEGER NOT NULL DEFAULT 0,
        play_count INTEGER NOT NULL DEFAULT 0,
        favorite INTEGER NOT NULL DEFAULT 0,
        tmdb_id INTEGER NOT NULL DEFAULT 0,
        poster_url TEXT NOT NULL DEFAULT '',
        backdrop_url TEXT NOT NULL DEFAULT '',
        rating REAL NOT NULL DEFAULT 0,
        overview TEXT NOT NULL DEFAULT '',
        release_date TEXT NOT NULL DEFAULT '',
        media_type TEXT NOT NULL DEFAULT 'movie',
        series_title TEXT NOT NULL DEFAULT '',
        season_number INTEGER NOT NULL DEFAULT 0,
        season_name TEXT NOT NULL DEFAULT '',
        episode_number INTEGER NOT NULL DEFAULT 0,
        episode_title TEXT NOT NULL DEFAULT '',
        still_url TEXT NOT NULL DEFAULT '',
        last_opened_ms INTEGER NOT NULL DEFAULT 0,
        created_ms INTEGER NOT NULL DEFAULT 0
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS work_genres (
        work_type TEXT NOT NULL CHECK (work_type IN ('movie', 'tv')),
        tmdb_id INTEGER NOT NULL CHECK (tmdb_id > 0),
        genre_id INTEGER NOT NULL CHECK (genre_id > 0),
        genre_name TEXT NOT NULL,
        PRIMARY KEY (work_type, tmdb_id, genre_id)
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS work_genres_genre_idx "
        "ON work_genres(genre_id, work_type, tmdb_id)"
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(media)")}
    migrations = {
        "tmdb_id": "INTEGER NOT NULL DEFAULT 0",
        "poster_url": "TEXT NOT NULL DEFAULT ''",
        "backdrop_url": "TEXT NOT NULL DEFAULT ''",
        "rating": "REAL NOT NULL DEFAULT 0",
        "overview": "TEXT NOT NULL DEFAULT ''",
        "release_date": "TEXT NOT NULL DEFAULT ''",
        "media_type": "TEXT NOT NULL DEFAULT 'movie'",
        "series_title": "TEXT NOT NULL DEFAULT ''",
        "season_number": "INTEGER NOT NULL DEFAULT 0",
        "season_name": "TEXT NOT NULL DEFAULT ''",
        "episode_number": "INTEGER NOT NULL DEFAULT 0",
        "episode_title": "TEXT NOT NULL DEFAULT ''",
        "still_url": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in migrations.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE media ADD COLUMN {name} {definition}")
    return connection


def import_metadata(path: Path) -> dict[str, int]:
    root, _, _, _, _ = read_config()
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cannot read metadata JSON: {error}") from None
    if isinstance(records, list):
        media_records = records
        works = []
    elif isinstance(records, dict):
        media_records = records.get("media", [])
        works = records.get("works", [])
    else:
        raise RuntimeError("Metadata input must be a JSON array or an object with media and works arrays.")
    if not isinstance(media_records, list) or not isinstance(works, list):
        raise RuntimeError("Metadata media and works values must be JSON arrays.")

    normalized_works = []
    seen_works = set()
    for work in works:
        if not isinstance(work, dict):
            raise RuntimeError("Each work item must be a JSON object.")
        work_type = str(work.get("work_type", ""))
        tmdb_id = int(work.get("tmdb_id") or 0)
        genres = work.get("genres", [])
        if work_type not in ("movie", "tv") or tmdb_id <= 0 or not isinstance(genres, list):
            raise RuntimeError("Each work requires work_type movie|tv, a positive tmdb_id, and a genres array.")
        work_key = (work_type, tmdb_id)
        if work_key in seen_works:
            raise RuntimeError("Each TMDB work may appear only once in the works array.")
        seen_works.add(work_key)
        normalized_genres = []
        seen_genres = set()
        for genre in genres:
            if not isinstance(genre, dict):
                raise RuntimeError("Each genre must be a JSON object.")
            genre_id = int(genre.get("id") or 0)
            genre_name = str(genre.get("name") or "").strip()
            if genre_id <= 0 or not genre_name:
                raise RuntimeError("Each genre requires a positive id and a non-empty name.")
            if genre_id not in seen_genres:
                seen_genres.add(genre_id)
                normalized_genres.append((genre_id, genre_name))
        normalized_works.append((work_type, tmdb_id, normalized_genres))

    fields = (
        "source", "title", "tmdb_id", "poster_url", "backdrop_url", "rating",
        "overview", "release_date", "media_type", "series_title",
        "season_number", "season_name", "episode_number", "episode_title", "still_url",
    )
    connection = open_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = int(time.time() * 1000)
        for record in media_records:
            if not isinstance(record, dict):
                raise RuntimeError("Each metadata item must be a JSON object.")
            source = str(record.get("source", ""))
            parsed_source = urllib.parse.urlsplit(source)
            if not is_under_root(source, root) or parsed_source.username or parsed_source.password:
                raise RuntimeError("A media source is outside the configured WebDAV root.")
            values = []
            for field in fields:
                value = record.get(field)
                if field in ("tmdb_id", "season_number", "episode_number"):
                    value = int(value or 0)
                elif field == "rating":
                    value = float(value or 0)
                elif value is None:
                    value = ""
                values.append(value)
            if values[8] not in ("movie", "episode"):
                raise RuntimeError("media_type must be 'movie' or 'episode'.")
            connection.execute(
                "INSERT INTO media (source,title,tmdb_id,poster_url,backdrop_url,rating,overview,release_date,"
                "media_type,series_title,season_number,season_name,episode_number,episode_title,still_url,created_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
                "title=excluded.title,tmdb_id=excluded.tmdb_id,poster_url=excluded.poster_url,"
                "backdrop_url=excluded.backdrop_url,rating=excluded.rating,overview=excluded.overview,"
                "release_date=excluded.release_date,media_type=excluded.media_type,"
                "series_title=excluded.series_title,season_number=excluded.season_number,"
                "season_name=excluded.season_name,"
                "episode_number=excluded.episode_number,episode_title=excluded.episode_title,"
                "still_url=excluded.still_url",
                (*values, now),
            )
        for work_type, tmdb_id, genres in normalized_works:
            connection.execute(
                "DELETE FROM work_genres WHERE work_type=? AND tmdb_id=?",
                (work_type, tmdb_id),
            )
            connection.executemany(
                "INSERT INTO work_genres(work_type,tmdb_id,genre_id,genre_name) VALUES(?,?,?,?)",
                [(work_type, tmdb_id, genre_id, genre_name)
                 for genre_id, genre_name in genres],
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "imported": len(media_records),
        "works_updated": len(normalized_works),
        "genres_written": sum(len(genres) for _, _, genres in normalized_works),
    }


def migrate_genres() -> dict[str, int | str]:
    connection = open_database()
    try:
        count = connection.execute("SELECT COUNT(*) FROM work_genres").fetchone()[0]
        return {"table": "work_genres", "genre_assignments": int(count)}
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    listing = subparsers.add_parser("list", help="List video files under the configured WebDAV root")
    listing.add_argument("--limit", type=int, default=5000)
    search = subparsers.add_parser("search", help="Search TMDB candidates")
    search.add_argument("kind", choices=("movie", "tv"))
    search.add_argument("title")
    details = subparsers.add_parser("details", help="Get one TMDB movie or TV work with its genres")
    details.add_argument("kind", choices=("movie", "tv"))
    details.add_argument("tmdb_id", type=int)
    episode = subparsers.add_parser("episode", help="Get TMDB TV episode details")
    episode.add_argument("series_id", type=int)
    episode.add_argument("season", type=int)
    episode.add_argument("episode", type=int)
    season_names = subparsers.add_parser(
        "sync-season-names", help="Fetch TMDB season names and update only that series in the catalog"
    )
    season_names.add_argument("series_id", type=int)
    episode_art = subparsers.add_parser(
        "sync-episode-details", help="Fetch episode titles, synopses and still images for one series"
    )
    episode_art.add_argument("series_id", type=int)
    subparsers.add_parser("check-tmdb", help="Verify TMDB credentials with a read-only API request")
    subparsers.add_parser("migrate-genres", help="Create the work_genres table in the qPlay catalog")
    importer = subparsers.add_parser("import", help="Upsert media metadata and work genres into qPlay SQLite")
    importer.add_argument("metadata", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "list":
            if args.limit < 1:
                raise RuntimeError("--limit must be positive.")
            result = list_media(args.limit)
        elif args.command == "search":
            result = search_tmdb(args.kind, args.title)
        elif args.command == "details":
            result = title_details(args.kind, args.tmdb_id)
        elif args.command == "episode":
            data = episode_details(args.series_id, args.season, args.episode)
            result = {key: data.get(key) for key in (
                "id", "name", "overview", "air_date", "vote_average", "still_path"
            )}
        elif args.command == "sync-season-names":
            result = sync_season_names(args.series_id)
        elif args.command == "sync-episode-details":
            result = sync_episode_details(args.series_id)
        elif args.command == "check-tmdb":
            data = tmdb_request("configuration")
            images = data.get("images", {})
            result = {
                "authenticated": True,
                "secure_image_base_url": images.get("secure_base_url", ""),
            }
        elif args.command == "migrate-genres":
            result = migrate_genres()
        else:
            result = import_metadata(args.metadata)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, sqlite3.Error, ET.ParseError, urllib.error.URLError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
