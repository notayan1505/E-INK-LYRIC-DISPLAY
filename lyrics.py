
import re
import time
import requests

BASE_URL = "https://lrclib.net/api"
HEADERS = {
    "User-Agent": "PaperLyrics/1.0 (Raspberry Pi lyrics display)"
}


def request_json(url, params=None, attempts=3):
    """Request JSON with retries for temporary service errors."""

    for attempt in range(attempts):
        try:
            response = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=12
            )

            if response.status_code == 404:
                return None

            if response.status_code == 429 or response.status_code >= 500:
                print(
                    f"LRCLIB returned HTTP {response.status_code}; "
                    f"attempt {attempt + 1}/{attempts}"
                )

                if attempt < attempts - 1:
                    time.sleep(attempt + 1)
                    continue

                return None

            response.raise_for_status()
            return response.json()

        except (requests.RequestException, ValueError) as exc:
            print(f"LRCLIB request failed: {exc}")

            if attempt < attempts - 1:
                time.sleep(attempt + 1)

    return None


def parse_synced_lyrics(synced):
    """Convert LRC timestamps into (seconds, lyric) pairs."""

    if not synced:
        return []

    lines = []

    for line in synced.splitlines():
        timestamps = re.findall(
            r"\[(\d{1,2}):(\d{2}(?:\.\d{1,3})?)\]",
            line
        )

        text = re.sub(
            r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]",
            "",
            line
        ).strip()

        for minutes, seconds in timestamps:
            timestamp = int(minutes) * 60 + float(seconds)
            lines.append((timestamp, text))

    lines.sort(key=lambda item: item[0])
    return lines


def has_synced_lyrics(item):
    return (
        isinstance(item, dict)
        and bool(item.get("syncedLyrics"))
    )


def clean_artist(artist):
    """Remove common extra text from media-session artist metadata."""

    if not artist:
        return ""

    # Apple Music sometimes combines artist and release information.
    separators = [" — ", " – ", " - "]

    for separator in separators:
        if separator in artist:
            artist = artist.split(separator, 1)[0]
            break

    return artist.strip()


def fetch_lyrics(title, artist, album="", duration=0):
    """Find synchronized lyrics, trying progressively broader searches."""

    title = (title or "").strip()
    artist = (artist or "").strip()
    album = (album or "").strip()

    if not title:
        print("Cannot search lyrics: track title is empty.")
        return []

    artists_to_try = []

    cleaned = clean_artist(artist)

    if cleaned:
        artists_to_try.append(cleaned)

    if artist and artist not in artists_to_try:
        artists_to_try.append(artist)

    if not artists_to_try:
        artists_to_try.append("")

    # First try the exact endpoint with minimal metadata.
    for artist_name in artists_to_try:
        if not artist_name:
            continue

        params = {
            "track_name": title,
            "artist_name": artist_name,
        }

        if album and album != "Album unavailable":
            params["album_name"] = album

        if duration and duration > 0:
            params["duration"] = int(duration)

        print(
            f"Trying exact lookup: {title!r} / {artist_name!r}"
        )

        result = request_json(f"{BASE_URL}/get", params=params)

        if has_synced_lyrics(result):
            lines = parse_synced_lyrics(result["syncedLyrics"])

            if lines:
                print(f"Found {len(lines)} synchronized lyric lines.")
                return lines

    # Search with different title and artist combinations.
    searches = []

    for artist_name in artists_to_try:
        if artist_name:
            searches.append({
                "track_name": title,
                "artist_name": artist_name,
            })

    # Also try the title alone in case artist metadata is incorrect.
    searches.append({"track_name": title})

    # Try without common version labels.
    shorter_title = re.sub(
        r"\s*[\(\[](?:acoustic|live|remaster(?:ed)?|radio edit|"
        r"bonus track|deluxe|instrumental|remix)[^\)\]]*[\)\]]",
        "",
        title,
        flags=re.IGNORECASE
    ).strip()

    if shorter_title and shorter_title.casefold() != title.casefold():
        for artist_name in artists_to_try:
            if artist_name:
                searches.append({
                    "track_name": shorter_title,
                    "artist_name": artist_name,
                })

        searches.append({"track_name": shorter_title})

    # Avoid repeating identical queries.
    seen = set()

    for params in searches:
        key = tuple(sorted(params.items()))

        if key in seen:
            continue

        seen.add(key)

        print(f"Trying search: {params}")

        results = request_json(
            f"{BASE_URL}/search",
            params=params
        )

        if not isinstance(results, list):
            continue

        # Prefer a result that has synchronized lyrics.
        for item in results:
            if not has_synced_lyrics(item):
                continue

            lines = parse_synced_lyrics(item["syncedLyrics"])

            if lines:
                print(
                    "Found synchronized lyrics for:",
                    item.get("trackName", title),
                    "/",
                    item.get("artistName", artist)
                )
                print(f"Found {len(lines)} lyric lines.")
                return lines

    print("No synchronized lyrics found after all search attempts.")
    return []


def lyrics_at(lines, position):
    """Return the previous, current, and next lyric for playback time."""

    if not lines:
        return "", "No synced lyrics found", ""

    current_index = 0

    for index, (timestamp, _) in enumerate(lines):
        if timestamp <= position:
            current_index = index
        else:
            break

    previous = (
        lines[current_index - 1][1]
        if current_index > 0
        else ""
    )

    current = lines[current_index][1]

    next_line = (
        lines[current_index + 1][1]
        if current_index + 1 < len(lines)
        else ""
    )

    return previous, current, next_line
