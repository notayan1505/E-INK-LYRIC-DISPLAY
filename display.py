
import asyncio
import io
import threading
import tkinter as tk

from PIL import Image, ImageOps, ImageTk

from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager
)
from winsdk.windows.storage.streams import DataReader

from lyrics import fetch_lyrics, lyrics_at


# ---------- Visual design ----------

BG = "#F3F0E7"
INK = "#171713"
MUTED = "#77746B"
RULE = "#C9C5BA"

WIDTH = 600
HEIGHT = 440
ART_SIZE = 110
LYRIC_OFFSET = 1.3  # seconds; positive values show lyrics earlier

root = tk.Tk()
root.title("Paper Lyrics")
root.geometry(f"{WIDTH}x{HEIGHT}")
root.configure(bg=BG)
root.resizable(False, False)


# ---------- Header ----------

tk.Label(
    root,
    text="NOW PLAYING",
    font=("Segoe UI", 9, "bold"),
    fg=MUTED,
    bg=BG
).pack(pady=(12, 0))


# ---------- Lyrics ----------

lyrics_area = tk.Frame(root, bg=BG)
lyrics_area.pack(
    fill="both",
    expand=True,
    padx=30,
    pady=(5, 3)
)

previous_label = tk.Label(
    lyrics_area,
    text="",
    font=("Georgia", 18),
    fg=MUTED,
    bg=BG,
    wraplength=520,
    justify="center"
)
previous_label.pack(expand=True, pady=2)

current_label = tk.Label(
    lyrics_area,
    text="Waiting for lyrics...",
    font=("Georgia", 25, "bold"),
    fg=INK,
    bg=BG,
    wraplength=530,
    justify="center"
)
current_label.pack(expand=True, pady=5)

next_label = tk.Label(
    lyrics_area,
    text="",
    font=("Georgia", 18),
    fg=MUTED,
    bg=BG,
    wraplength=520,
    justify="center"
)
next_label.pack(expand=True, pady=2)


# ---------- Divider ----------

tk.Frame(
    root,
    bg=RULE,
    height=1
).pack(
    fill="x",
    padx=22,
    pady=(3, 8)
)


# ---------- Footer: album art and metadata ----------

footer = tk.Frame(root, bg=BG)
footer.pack(
    fill="x",
    padx=22,
    pady=(0, 7)
)

art_label = tk.Label(
    footer,
    text="♪",
    font=("Georgia", 40),
    bg="#DDD9CF",
    fg=INK,
    width=5,
    height=3
)
art_label.pack(side="left", padx=(0, 12))

info = tk.Frame(footer, bg=BG)
info.pack(side="left", fill="both", expand=True)

song_label = tk.Label(
    info,
    text="Waiting for Apple Music",
    font=("Segoe UI", 15, "bold"),
    fg=INK,
    bg=BG,
    anchor="w",
    wraplength=300,
    justify="left"
)
song_label.pack(fill="x")

artist_label = tk.Label(
    info,
    text="",
    font=("Segoe UI", 13),
    fg=INK,
    bg=BG,
    anchor="w",
    wraplength=300,
    justify="left"
)
artist_label.pack(fill="x")

album_label = tk.Label(
    info,
    text="",
    font=("Segoe UI", 11),
    fg=MUTED,
    bg=BG,
    anchor="w",
    wraplength=300,
    justify="left"
)
album_label.pack(fill="x")


# ---------- Playback time ----------

time_box = tk.Frame(footer, bg=BG)
time_box.pack(side="right", padx=(7, 0))

time_label = tk.Label(
    time_box,
    text="--:--",
    font=("Segoe UI", 15, "bold"),
    fg=INK,
    bg=BG
)
time_label.pack(anchor="e")

duration_label = tk.Label(
    time_box,
    text="of --:--",
    font=("Segoe UI", 11),
    fg=MUTED,
    bg=BG
)
duration_label.pack(anchor="e")


# ---------- Progress bar ----------

progress = tk.Canvas(
    root,
    height=4,
    bg="#D9D5CB",
    highlightthickness=0
)
progress.pack(
    fill="x",
    padx=22,
    pady=(0, 3)
)


# ---------- Status ----------

status_label = tk.Label(
    root,
    text="Connecting to Apple Music...",
    font=("Segoe UI", 8),
    fg=MUTED,
    bg=BG
)
status_label.pack(pady=(0, 5))


# ---------- State ----------

last_track = None
art_photo = None


# ---------- Helper functions ----------

def fmt(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02}"


def set_lyrics(previous, current, next_line):
    """Update the lyrics on the Tkinter UI thread."""
    previous_label.config(text=previous)
    current_label.config(text=current)
    next_label.config(text=next_line)


def set_artwork(data):
    """Display album art as a monochrome thumbnail."""
    global art_photo

    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
        image = ImageOps.fit(image, (ART_SIZE, ART_SIZE))

        image = image.convert("L").point(
            lambda pixel: 255 if pixel > 155 else 0
        ).convert("1")

        art_photo = ImageTk.PhotoImage(image)

        art_label.config(
            image=art_photo,
            text="",
            width=ART_SIZE,
            height=ART_SIZE
        )

    except Exception as exc:
        print("Album art conversion failed:", repr(exc))


def update_progress(position, duration):
    """Update playback time and the progress bar."""
    progress.delete("all")
    width = progress.winfo_width()

    if duration > 0:
        fraction = max(0, min(1, position / duration))

        progress.create_rectangle(
            0,
            0,
            width * fraction,
            4,
            fill=INK,
            outline=INK
        )

    time_label.config(text=fmt(position))
    duration_label.config(text=f"of {fmt(duration)}")


async def get_art_bytes(info):
    """Read the album thumbnail provided by Windows."""
    thumbnail = getattr(info, "thumbnail", None)

    if thumbnail is None:
        return None

    try:
        stream = await thumbnail.open_read_async()
        size = stream.size

        if not size or size > 15_000_000:
            return None

        reader = DataReader(stream)
        await reader.load_async(size)

        data = bytearray(size)
        reader.read_bytes(data)
        reader.close()

        return bytes(data)

    except Exception as exc:
        print("Album art unavailable:", repr(exc))
        return None


# ---------- Apple Music and lyrics loop ----------

async def media_loop():
    global last_track

    current_lyrics = []
    lyrics_track = None

    print("Connecting to Windows media sessions...")

    manager = (
        await GlobalSystemMediaTransportControlsSessionManager
        .request_async()
    )

    print("Connected. Waiting for music...")

    while True:
        try:
            session = manager.get_current_session()

            if session is None:
                root.after(
                    0,
                    song_label.config,
                    {"text": "Nothing playing"}
                )

                root.after(
                    0,
                    set_lyrics,
                    "",
                    "Play a song to begin",
                    ""
                )

                root.after(
                    0,
                    status_label.config,
                    {"text": "Waiting for a media session"}
                )

                last_track = None
                lyrics_track = None
                current_lyrics = []

                await asyncio.sleep(1)
                continue

            info = await session.try_get_media_properties_async()
            playback = session.get_playback_info()
            timeline = session.get_timeline_properties()

            title = info.title or "Unknown song"
            artist = info.artist or "Unknown artist"
            album = info.album_title or "Album unavailable"

            position = timeline.position.total_seconds()
            duration = timeline.end_time.total_seconds()

            status_name = str(
                playback.playback_status
            ).split(".")[-1]

            is_playing = "Playing" in status_name

            track_key = (
                session.source_app_user_model_id,
                title,
                artist,
                album
            )

            # New track: update metadata, art, and lyrics.
            if track_key != last_track:
                last_track = track_key

                print("\n" + "=" * 45)
                print("New track detected")
                print(f"Title:    {title!r}")
                print(f"Artist:   {artist!r}")
                print(f"Album:    {album!r}")
                print(f"Duration: {duration:.1f} seconds")

                root.after(
                    0,
                    song_label.config,
                    {"text": title}
                )

                root.after(
                    0,
                    artist_label.config,
                    {"text": artist}
                )

                root.after(
                    0,
                    album_label.config,
                    {"text": album}
                )

                root.after(
                    0,
                    set_lyrics,
                    "",
                    "Finding lyrics...",
                    ""
                )

                # Update album artwork.
                art_data = await get_art_bytes(info)

                if art_data:
                    root.after(0, set_artwork, art_data)

                # Fetch lyrics without blocking the UI event loop.
                print("Searching LRCLIB for synchronized lyrics...")

                try:
                    current_lyrics = await asyncio.to_thread(
                        fetch_lyrics,
                        title,
                        artist,
                        album,
                        duration
                    )

                    print(
                        f"Lyrics returned: "
                        f"{len(current_lyrics)} lines"
                    )

                    if current_lyrics:
                        print(
                            "First lyric:",
                            current_lyrics[0]
                        )
                    else:
                        print(
                           "No synchronized lyrics returned. "
                            "Check lyrics.py and the track metadata."
                        )

                except Exception as exc:
                    current_lyrics = []
                    print("Lyrics lookup failed:", repr(exc))

                lyrics_track = track_key

                if not current_lyrics:
                    root.after(
                        0,
                        set_lyrics,
                        "",
                        "No synced lyrics found",
                        ""
                    )

            # Select the lines matching playback position.
            if lyrics_track == track_key and current_lyrics:
                adjusted_position = position + LYRIC_OFFSET

                previous, current, next_line = lyrics_at(
                    current_lyrics,
                    adjusted_position
                )

                root.after(
                    0,
                    set_lyrics,
                    previous,
                    current,
                    next_line
                )

            # Update progress and status.
            root.after(
                0,
                update_progress,
                position,
                duration
            )

            root.after(
                0,
                status_label.config,
                {
                    "text": "Apple Music  ·  " + (
                        "PLAYING"
                        if is_playing
                        else status_name.upper()
                    )
                }
            )

        except Exception as exc:
            print("Media session error:", repr(exc))

            root.after(
                0,
                status_label.config,
                {"text": "Reading playback information..."}
            )

        await asyncio.sleep(0.5)


# ---------- Start background reader ----------

def start_reader():
    try:
        asyncio.run(media_loop())
    except Exception as exc:
        print("Media reader stopped:", repr(exc))


threading.Thread(
    target=start_reader,
    daemon=True
).start()


# ---------- Start window ----------

root.mainloop()