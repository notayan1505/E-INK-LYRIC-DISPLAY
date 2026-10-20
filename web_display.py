import asyncio
import base64
import io
import queue
import threading
import time

from flask import Flask, jsonify, make_response, render_template_string
from PIL import Image, ImageOps

from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager,
)
from winsdk.windows.storage.streams import DataReader

from lyrics import fetch_lyrics

app = Flask(__name__)

LYRIC_OFFSET = 1.3
PORT = 5000

state = {
    "title": "Paper Lyrics",
    "artist": "Waiting for Apple Music",
    "album": "",
    "artwork": "",
    "position": 0.0,
    "duration": 0.0,
    "lyrics": [],
    "current_line": -1,
    "playing": False,
    "status": "Waiting for Apple Music",
    "updated": 0,
    "offset": LYRIC_OFFSET,
}
state_lock = threading.Lock()
commands = queue.Queue()


def current_index(lines, position):
    active = -1
    for i, (timestamp, _) in enumerate(lines):
        if timestamp <= position:
            active = i
        else:
            break
    return active


async def get_artwork(info):
    try:
        thumbnail = info.thumbnail
        if thumbnail is None:
            return ""

        stream = await thumbnail.open_read_async()
        size = stream.size
        if not size:
            return ""

        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(size)
        raw = bytearray(size)
        reader.read_bytes(raw)
        reader.close()

        image = Image.open(io.BytesIO(raw)).convert("L")
        image.thumbnail((360, 360))
        image = ImageOps.autocontrast(image)
        # Floyd-Steinberg Dithering for high contrast E-Ink print style
        image = image.convert("1", dither=Image.Dither.FLOYDSTEINBERG)

        output = io.BytesIO()
        image.save(output, format="PNG")
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return "data:image/png;base64," + encoded

    except Exception as exc:
        print("Artwork error:", repr(exc))
        return ""


async def media_loop():
    print("Connecting to Windows media sessions...")
    manager = None
    last_track_key = None
    lines = []
    artwork = ""
    last_line = -999

    while True:
        try:
            if manager is None:
                manager = (
                    await GlobalSystemMediaTransportControlsSessionManager
                    .request_async()
                )
                print("Connected. Play a song in Apple Music.")

            session = manager.get_current_session()

            if session is None:
                while not commands.empty():
                    commands.get_nowait()
                with state_lock:
                    state["status"] = "Waiting for Apple Music"
                    state["playing"] = False
                    state["updated"] = time.time()
                await asyncio.sleep(1)
                continue

            while True:
                try:
                    cmd = commands.get_nowait()
                except queue.Empty:
                    break
                try:
                    if cmd == "toggle":
                        await session.try_toggle_play_pause_async()
                    elif cmd == "next":
                        await session.try_skip_next_async()
                    elif cmd == "prev":
                        await session.try_skip_previous_async()
                except Exception as exc:
                    print("Control error:", repr(exc))

            info = await session.try_get_media_properties_async()
            playback = session.get_playback_info()
            timeline = session.get_timeline_properties()

            title = str(info.title or "Unknown title")
            artist = str(info.artist or "Unknown artist")
            album = str(info.album_title or "")

            duration = 0.0
            position = 0.0
            if timeline:
                try:
                    start = timeline.start_time.total_seconds()
                    end = timeline.end_time.total_seconds()
                    duration = max(0.0, end - start)
                    position = max(0.0, timeline.position.total_seconds() - start)
                except Exception:
                    pass

            raw_status = playback.playback_status
            status_name = getattr(raw_status, "name", None)
            if not status_name:
                names = {0: "CLOSED", 1: "OPENED", 2: "CHANGING",
                         3: "STOPPED", 4: "PLAYING", 5: "PAUSED"}
                try:
                    status_name = names.get(int(raw_status), str(raw_status))
                except Exception:
                    status_name = str(raw_status)
            playing = status_name.upper() == "PLAYING"
            track_key = (title, artist, album)

            if track_key != last_track_key:
                last_track_key = track_key
                last_line = -999
                print(f"New track: {title} — {artist} — {album}")

                artwork = await get_artwork(info)

                try:
                    lines = await asyncio.to_thread(
                        fetch_lyrics, title, artist, album, duration
                    )
                    lines = lines or []
                    print(f"Found {len(lines)} lyric lines.")
                except Exception as exc:
                    print("Lyrics error:", repr(exc))
                    lines = []

            if duration <= 0 and lines:
                duration = float(lines[-1][0]) + 12.0

            line_index = current_index(lines, position + LYRIC_OFFSET)

            lyric_data = [
                {"time": float(timestamp), "text": text}
                for timestamp, text in lines
            ]

            with state_lock:
                state.update({
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "artwork": artwork,
                    "position": position,
                    "duration": duration,
                    "lyrics": lyric_data,
                    "current_line": line_index,
                    "playing": playing,
                    "status": status_name,
                    "updated": time.time(),
                    "offset": LYRIC_OFFSET,
                })

            if line_index != last_line:
                last_line = line_index
                if 0 <= line_index < len(lines):
                    print(
                        f"Lyric {line_index + 1}/{len(lines)}: "
                        f"{lines[line_index][1]}"
                    )

        except Exception as exc:
            print("Media reader error:", repr(exc))
            manager = None
            with state_lock:
                state["status"] = "Reconnecting to Apple Music"
                state["updated"] = time.time()

        await asyncio.sleep(0.25)


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="color-scheme" content="only light">
<title>Paper Lyrics</title>
<style>
* { -webkit-box-sizing: border-box; box-sizing: border-box; }

html, body {
    margin: 0; padding: 0; width: 100%; height: 100%;
    background: #000000; color: #ffffff; color-scheme: only light;
    font-family: Georgia, Baskerville, "Times New Roman", serif;
    overflow: hidden;
}
#app { position: fixed; top: 0; left: 0; width: 758px; height: 1024px;
       -webkit-transform-origin: 0 0; transform-origin: 0 0; }
a { color: #fff; text-decoration: none; }

/* ---------- header ---------- */
.top {
    position: absolute; top: 26px; left: 36px; right: 36px; height: 34px;
    border-bottom: 3px double #fff;
    font-family: "Courier New", Courier, monospace;
    font-size: 14px; font-weight: bold; letter-spacing: 4px;
}
.top .right { float: right; }

/* ---------- now playing (portrait default) ---------- */
.meta {
    position: absolute; top: 78px; left: 36px; right: 36px; height: 400px;
    overflow: hidden; text-align: center;
}
.meta-in { display: table; width: 100%; height: 100%; }
.meta-cell { display: table-cell; vertical-align: top; text-align: center; }
.art {
    display: inline-block; border: 3px solid #fff; padding: 4px;
    background: #fff; margin-bottom: 14px;
}
.art img { display: block; width: 250px; height: 250px; }
.art.none { display: none; }
.kicker {
    font-family: "Courier New", Courier, monospace;
    font-size: 13px; font-weight: bold; letter-spacing: 4px;
    color: #fff; margin-bottom: 8px;
}
.title { font-size: 36px; font-weight: bold; line-height: 1.1; margin-bottom: 8px; word-wrap: break-word; }
.title.l1 { font-size: 30px; }
.title.l2 { font-size: 24px; }
.artist { font-size: 26px; font-style: italic; line-height: 1.2; word-wrap: break-word; }
.artist.l1 { font-size: 21px; }
.album {
    display: inline-block; margin-top: 12px; padding-top: 10px;
    border-top: 2px solid #fff;
    font-family: "Courier New", Courier, monospace;
    font-size: 14px; color: #fff; word-wrap: break-word;
}

/* ---------- lyrics ---------- */
.stage { position: absolute; top: 490px; bottom: 205px; left: 36px; right: 36px; overflow: hidden; }
.stage-in { display: table; width: 100%; height: 100%; }
.stage-cell { display: table-cell; vertical-align: middle; text-align: center; }
.ornament { font-size: 16px; letter-spacing: 12px; margin: 0 0 14px 0; line-height: 1; }
.lyric { margin: 0; padding: 0; line-height: 1.25; word-wrap: break-word; }
.lyric.past    { font-style: italic; font-size: 21px; color: #fff; margin-bottom: 16px; }
.lyric.current { font-size: 40px; font-weight: bold; color: #fff; margin-bottom: 16px; line-height: 1.18; }
.lyric.future  { font-size: 23px; color: #fff; margin-bottom: 12px; }
.lyric.empty   { font-size: 22px; font-style: italic; color: #fff; }

/* ---------- footer ---------- */
.footer { position: absolute; left: 36px; right: 36px; bottom: 22px; }
.bar { height: 34px; border: 4px solid #fff; background: #000000; }
#progress { height: 100%; width: 0%; background: #ffffff; }
.times {
    margin-top: 8px; height: 22px;
    font-family: "Courier New", Courier, monospace;
    font-size: 15px; font-weight: bold;
}
#remaining { float: right; }
.controls { text-align: center; margin-top: 10px; white-space: nowrap; }
.btn {
    display: inline-block; position: relative; vertical-align: middle;
    width: 120px; height: 78px; margin: 0 12px;
    border: 3px solid #fff; background: #000;
}
.btn.main { width: 170px; background: #fff; }
.btn i { position: absolute; display: block; }
/* icon pieces */
.bar-v { width: 5px; height: 32px; top: 20px; background: #fff; }
.tri-r { width: 0; height: 0; top: 20px;
    border-left: 24px solid #fff; border-top: 16px solid transparent; border-bottom: 16px solid transparent; }
.tri-l { width: 0; height: 0; top: 20px;
    border-right: 24px solid #fff; border-top: 16px solid transparent; border-bottom: 16px solid transparent; }
.btn.main .tri-r { border-left-color: #000; }
.btn.main .bar-v { background: #000; }
/* prev: bar + left triangle */
#b-prev .bar-v { left: 41px; } #b-prev .tri-l { left: 49px; }
/* next: right triangle + bar */
#b-next .tri-r { left: 41px; } #b-next .bar-v { left: 68px; }
/* play / pause (center) */
#icon { position: absolute; left: 50%; top: 50%; width: 0; height: 0; display: block; }
#icon .tri-r { left: -12px; top: -18px; border-left-width: 28px; border-top-width: 18px; border-bottom-width: 18px; }
#icon .bar-v { width: 10px; height: 36px; top: -18px; }
#icon .bar-v.a { left: -16px; } #icon .bar-v.b { left: 6px; }

/* ---------- landscape (JS sets body.land; sizes/positions come from layout()) ---------- */
body.land .meta-cell { vertical-align: middle; }
body.land .title { font-size: 34px; }
body.land .title.l1 { font-size: 30px; }
body.land .title.l2 { font-size: 24px; }
body.land .artist { font-size: 25px; }
body.land .artist.l1 { font-size: 21px; }
body.land .stage { border-left: 2px solid #fff; padding-left: 30px; }
body.land .lyric.current { font-size: 38px; }
</style>
</head>
<body>
<div id="app">
    <span id="sync" style="display:none;"></span>

    <div class="meta" id="meta-box"><div class="meta-in"><div class="meta-cell">
        <div class="art none" id="art"><img id="artwork" alt=""></div>
        <div class="kicker" id="kicker">NOW PLAYING</div>
        <div class="title" id="title">Paper Lyrics</div>
        <div class="artist" id="artist">Waiting for Apple Music</div>
        <div class="album" id="album" style="display:none;"></div>
    </div></div></div>

    <div class="stage" id="stage-box">
        <div class="stage-in"><div class="stage-cell">
            <div class="ornament">&#9830; &#9830; &#9830;</div>
            <div id="lyrics"><p class="lyric empty">Play a song to begin.</p></div>
        </div></div>
    </div>

    <div class="footer">
        <div class="bar"><div id="progress"></div></div>
        <div class="times">
            <span id="elapsed">0:00</span>
            <span id="remaining"></span>
        </div>
        <div class="controls">
            <a href="#" class="btn" id="b-prev" onclick="ctl('prev'); return false;"><i class="bar-v"></i><i class="tri-l"></i></a>
            <a href="#" class="btn main" id="icon-btn" onclick="ctl('toggle'); return false;"><span id="icon"></span></a>
            <a href="#" class="btn" id="b-next" onclick="ctl('next'); return false;"><i class="tri-r"></i><i class="bar-v"></i></a>
        </div>
    </div>
</div>

<div id="flash" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;z-index:50;"></div>
<div id="dbg" style="position:fixed;top:0;left:0;z-index:99;background:#fff;color:#000;font:14px monospace;padding:4px;display:none;"></div>

<script>
// ES5 only: the 2013 Kindle browser is old WebKit.
var prevKey = "", prevLine = -999, prevCount = -1, prevMode = -1;
var lyricOffset = 1.3, lastData = null, curKey = "", synced = false;
var W = 758, H = 1024, land = false, lyricBefore = 1, lyricAfter = 2;
var prevIcon = "", prevSecond = -1, prevArt = "";
var basePos = 0, baseAt = 0, duration = 0, isPlaying = false;

function $(id) { return document.getElementById(id); }
function now() { return new Date().getTime(); }
function setText(id, v) { var el = $(id); if (el.textContent !== v) el.textContent = v; }
function fmt(v) {
    var s = Math.max(0, Math.floor(Number(v) || 0));
    var r = s % 60;
    return Math.floor(s / 60) + ":" + (r < 10 ? "0" : "") + r;
}
function curPos() {
    var p = basePos;
    if (isPlaying) p += (now() - baseAt) / 1000;
    if (duration > 0 && p > duration) p = duration;
    return p;
}

function param(name, def) {
    var m = new RegExp("[?&]" + name + "=([-0-9.]+)").exec(window.location.search);
    return m ? parseFloat(m[1]) : def;
}

function layout() {
    var pw0 = window.innerWidth || document.documentElement.clientWidth;
    var ph0 = window.innerHeight || document.documentElement.clientHeight;
    // ?rotate=cw / ?rotate=ccw force a sideways turn; ?rotate=off disables it
    var q = window.location.search;
    var isKindle = (navigator.userAgent || "").indexOf("Kindle") >= 0;
    var dir = q.indexOf("rotate=off") >= 0 ? 0
            : q.indexOf("rotate=ccw") >= 0 ? -1
            : (q.indexOf("rotate") >= 0 || isKindle) ? 1 : 0;   // Kindles rotate automatically
    var rotated = dir !== 0 && ph0 > pw0;
    var outH = window.outerHeight || 0;           // some browsers report a window shorter than the page area
    var autoBar = (outH > 0 && outH < ph0 && ph0 - outH < 250) ? ph0 - outH : (isKindle ? 36 : 0);
    var bar = param("bar", autoBar);              // ?bar=NN overrides
    var vw = rotated ? ph0 - bar : pw0;          // effective (post-rotation) screen size
    var vh = rotated ? pw0 : ph0 - bar;
    land = vw > vh;
    W = land ? 860 : 640;             // design width; smaller = bigger on screen
    var k = vw / W;
    H = Math.round(vh / k);           // design height follows the screen's shape

    var app = $("app").style;
    app.width = W + "px"; app.height = H + "px";
    var tf = "scale(" + k + ")";
    if (rotated) {
        tf = (dir > 0 ? "translate(" + pw0 + "px, 0px) rotate(90deg) "
                      : "translate(0px, " + (ph0 - bar) + "px) rotate(-90deg) ") + tf;
    }
    app.webkitTransform = tf; app.transform = tf;
    document.body.className = land ? "land" : "port";

    var meta = $("meta-box").style, stage = $("stage-box").style;
    var TOP = 30, mid, size;
    if (land) {
        mid = H - TOP - 205;
        var pw = Math.round(W * param("panel", 30) / 100);   // info panel width, % of screen
        meta.top = TOP + "px"; meta.left = "36px"; meta.right = "auto";
        meta.width = pw + "px"; meta.height = mid + "px";
        stage.top = TOP + "px"; stage.bottom = "205px";
        stage.left = (36 + pw + 34) + "px"; stage.right = "36px";
        size = Math.min(pw * 0.72, mid - 170);
        lyricBefore = 1;
        lyricAfter = 1;
    } else {
        mid = H - TOP - 205;
        var mh = Math.round(mid * 0.5);           // info = 50% of the middle band
        meta.top = TOP + "px"; meta.left = "36px"; meta.right = "36px";
        meta.width = "auto"; meta.height = mh + "px";
        stage.top = (TOP + mh + 12) + "px"; stage.bottom = "205px";
        stage.left = "36px"; stage.right = "36px";
        size = Math.min(W * 0.45, mh - 130);
        lyricBefore = 1;
        lyricAfter = 1;
    }
    lyricBefore = param("past", lyricBefore);
    lyricAfter = param("lines", lyricAfter);
    size = Math.max(100, Math.round(size));
    var img = $("artwork").style;
    img.width = size + "px"; img.height = size + "px";
    prevMode = -1;                                // force lyrics to redraw
    window.scrollTo(0, 0);
    if (q.indexOf("debug") >= 0) {                // ?debug shows what the browser reports
        var d = $("dbg");
        d.style.display = "block";
        d.textContent = "win " + pw0 + "x" + ph0 + " | rotated=" + rotated + " dir=" + dir +
            " | k=" + Math.round(k * 100) / 100 + " bar=" + bar +
            " | client " + document.documentElement.clientWidth + "x" + document.documentElement.clientHeight +
            " out " + window.outerWidth + "x" + window.outerHeight +
            " scr " + screen.width + "x" + screen.height +
            " | UA: " + (navigator.userAgent || "?");
    }
}

function fit(id, len, a, b) {
    var el = $(id);
    var c = id === "title" ? "title" : "artist";
    var cls = len > b ? c + " l2" : (len > a ? c + " l1" : c);
    if (el.className !== cls) el.className = cls;
}

function localIndex(lyrics) {
    var p = curPos() + lyricOffset, a = -1, i;
    for (i = 0; i < lyrics.length; i++) {
        if (Number(lyrics[i].time) <= p) a = i; else break;
    }
    return a;
}

function tickLyrics() {                 // runs often; only redraws when the line changes
    if (!lastData) return;
    var l = lastData.lyrics instanceof Array ? lastData.lyrics : [];
    if (l.length && localIndex(l) !== prevLine) renderLyrics(lastData);
}

// full-screen white-then-black flash to wipe e-ink ghosting
var cleanedKey = "";
function checkEnd() {
    if (!isPlaying || duration <= 0 || param("clean", 1) === 0) return;
    var left = duration - curPos();
    if (left > 10) { cleanedKey = ""; return; }          // allows a re-clean if you seek back
    if (left <= 2 && cleanedKey !== curKey) { cleanedKey = curKey; cleanScreen(); }
}

var cleaning = false;
function cleanScreen() {
    if (cleaning) return;
    cleaning = true;
    var st = $("flash").style, step = 0;
    var colors = ["#fff", "#000", "#fff", "#000"];
    st.display = "block";
    function next() {
        if (step >= colors.length) { st.display = "none"; cleaning = false; return; }
        st.background = colors[step++];
        setTimeout(next, 700);               // hold each color long enough for e-ink to settle
    }
    next();
}

function renderLyrics(d) {
    var lyrics = d.lyrics instanceof Array ? d.lyrics : [];
    var idx = localIndex(lyrics);
    var landscape = lyricBefore * 10 + lyricAfter + (land ? 100 : 0);
    var key = d.title + "|" + d.artist + "|" + d.album;
    if (key === prevKey && idx === prevLine && lyrics.length === prevCount && landscape === prevMode) return;
    prevKey = key; prevLine = idx; prevCount = lyrics.length; prevMode = landscape;

    var box = $("lyrics");
    box.innerHTML = "";
    var p, i;
    if (lyrics.length === 0) {
        p = document.createElement("p");
        p.className = "lyric empty";
        p.textContent = "No synced lyrics for this track.";
        box.appendChild(p);
        return;
    }
    var before = lyricBefore;
    var after = lyricAfter;
    var start = Math.max(0, idx - before);
    var end = Math.min(lyrics.length, Math.max(idx, 0) + after + 1);
    if (idx < 0) end = Math.min(lyrics.length, after + 1);
    for (i = start; i < end; i++) {
        p = document.createElement("p");
        p.className = "lyric " + (i < idx ? "past" : (i === idx ? "current" : "future"));
        p.textContent = lyrics[i].text || "\u266A";
        box.appendChild(p);
    }
}

// button shows the ACTION: pause bars while playing, play triangle while paused
function renderIcon(playing) {
    var k = playing ? "pause" : "play";
    if (k === prevIcon) return;
    prevIcon = k;
    $("icon").innerHTML = playing
        ? '<i class="bar-v a"></i><i class="bar-v b"></i>'
        : '<i class="tri-r"></i>';
}

function tickProgress() {
    var pos = curPos();
    var sec = Math.floor(pos);
    if (sec === prevSecond) return;       // repaint only when the second changes
    prevSecond = sec;
    var pct = duration > 0 ? Math.max(0, Math.min(100, pos / duration * 100)) : 0;
    $("progress").style.width = pct + "%";
    setText("elapsed", fmt(pos));
    setText("remaining", duration > 0 ? "-" + fmt(duration - pos) : "");
}

function ctl(action) {
    var x = new XMLHttpRequest();
    x.open("POST", "/api/control/" + action, true);
    x.send(null);
    if (action === "toggle") {            // instant feedback; next poll confirms
        basePos = curPos(); baseAt = now();
        isPlaying = !isPlaying;
        renderIcon(isPlaying);
        setText("kicker", isPlaying ? "NOW PLAYING" : "PAUSED");
    }
}

function apply(d) {
    var t = d.title || "Unknown title", a = d.artist || "";
    setText("title", t); fit("title", t.length, land ? 14 : 18, land ? 24 : 30);
    setText("artist", a); fit("artist", a.length, land ? 18 : 24, 99);
    var alb = $("album");
    alb.style.display = d.album ? "inline-block" : "none";
    setText("album", d.album || "");

    var art = $("artwork"), wrap = $("art");
    if (d.artwork) {
        if (d.artwork !== prevArt) { art.src = d.artwork; prevArt = d.artwork; }
        wrap.className = "art";
    } else {
        wrap.className = "art none";
    }

    var sp = Number(d.position) || 0;
    var np = !!d.playing;
    var key = d.title + "|" + d.artist + "|" + d.album;
    if (!synced || key !== curKey || np !== isPlaying || Math.abs(sp - curPos()) > 1.2) {
        basePos = sp; baseAt = now(); prevSecond = -1;   // resync only on a real change
    }
    duration = Number(d.duration) || 0;
    isPlaying = np; curKey = key; synced = true;
    lastData = d;
    lyricOffset = param("offset", Number(d.offset) || 0);

    setText("kicker", isPlaying ? "NOW PLAYING" : "PAUSED");
    renderIcon(isPlaying);
    renderLyrics(d);
    tickProgress();
    setText("sync", "LIVE");
}

function poll() {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/api/state?t=" + now(), true);
    xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) return;
        if (xhr.status === 200) {
            try { apply(JSON.parse(xhr.responseText)); }
            catch (e) { setText("sync", "ERROR"); }
        } else {
            setText("sync", "RECONNECTING");
        }
        setTimeout(poll, 800);
    };
    xhr.send(null);
}

window.onscroll = function () {
    if (window.pageXOffset || window.pageYOffset) window.scrollTo(0, 0);
};
document.addEventListener("touchmove", function (e) { e.preventDefault(); }, false);
$("stage-box").onclick = cleanScreen;      // tap the lyrics to wipe the screen now
layout();
window.onresize = layout;
window.onorientationchange = function () { setTimeout(layout, 300); };
poll();
setInterval(tickProgress, 1000);
setInterval(tickLyrics, 200);
setInterval(checkEnd, 500);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    response = make_response(render_template_string(PAGE))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/api/control/<action>", methods=["POST", "GET"])
def api_control(action):
    if action not in ("toggle", "next", "prev"):
        return jsonify({"ok": False}), 404
    commands.put(action)
    return jsonify({"ok": True})


@app.route("/api/state")
def api_state():
    with state_lock:
        snapshot = dict(state)

    response = jsonify(snapshot)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


if __name__ == "__main__":
    reader_thread = threading.Thread(
        target=lambda: asyncio.run(media_loop()),
        daemon=True,
    )
    reader_thread.start()

    print(f"Paper Lyrics starting at http://127.0.0.1:{PORT}")
    print("Keep this terminal open while using the display.")

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        threaded=True,
        use_reloader=False,
    )