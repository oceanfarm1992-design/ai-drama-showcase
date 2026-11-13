"""Weekly long-form "Bini's Ocean Discovery" (~8-10 min) for TikTok + Facebook.

Bini opens over real ocean footage, then a friend guides each habitat
chapter (beach, rock pools, mangroves, seagrass, coral reef, polar seas...),
each introduced by a title card; Bini closes. Same verified-footage, voice,
lip-sync and subtitle pipeline as the daily Real Ocean shorts.

A 10-minute video is ~14,000 frames, far too many to hold in memory like the
shorts do, so every segment is encoded to its own file as soon as it's
rendered and the files are joined losslessly at the end.

Run: python bini_discovery_episode.py out.mp4 ["Habitat A,Habitat B"]
"""
import datetime, json, math, os, pathlib, random, subprocess, sys, urllib.request, wave
import numpy as np
from moviepy.editor import ImageSequenceClip

import seabini_render as sr
import bini_real_footage as brf
import bini_real_render as brr
import bini_discovery_script as bds

SERIES_TITLE = "Bini's Ocean Discovery"
_MANIFEST_URL = "https://raw.githubusercontent.com/oceanfarm1992-design/ai-drama-showcase/master/manifest.json"
# TikTok's posting API caps most accounts at 10 minutes, so aim for ~9:15
# and never plan past ~9:50, even though Facebook would take longer.
_MIN_SEC, _TARGET_SEC, _MAX_SEC = 480.0, 555.0, 590.0
_PLANNED_CHAPTERS = 7         # one or two usually get dropped/skipped; ~85s each
_MIN_CHAPTER_SEGMENTS = 3     # fewer verified clips than this: skip the habitat
_CARD_SEC, _END_CARD_SEC = 2.5, 3.0
_MUSIC_GAIN = 0.12
_HARD_CLIP_ENTRIES = 40       # clips in the newest manifest entries (any series) are never reused
_SOFT_CLIP_ENTRIES = 150      # older ones only if nothing fresher passes
_GUIDES = ["tula", "ollo", "dodo", "pipi", "bini"]
# ~2 Mbps cap keeps a 10-minute 720p file around 150 MB for Buffer to fetch.
_X264 = ["-crf", "27", "-maxrate", "2000k", "-bufsize", "4000k"]


def _history():
    """(habitats featured before, newest first; earlier titles; hard-excluded
    clips; soft clips)."""
    try:
        data = json.loads(urllib.request.urlopen(_MANIFEST_URL, context=sr._SSL, timeout=15).read())
    except Exception as exc:
        print(f"[history] manifest unavailable ({exc}); no repeat protection this run")
        return [], [], set(), set()
    eps = data.get("episodes", [])
    hard = {i for e in eps[:_HARD_CLIP_ENTRIES] for i in e.get("footage_ids", [])}
    soft = {i for e in eps[:_SOFT_CLIP_ENTRIES] for i in e.get("footage_ids", [])} - hard
    mine = [e for e in eps if e.get("series_title") == SERIES_TITLE]
    seen = [h for e in mine for h in e.get("habitats", [])]
    return seen, [e.get("title", "") for e in mine[:10]], hard, soft


def _pick_habitats(seen, forced=None):
    """Least-recently-featured habitats first, so every habitat comes round
    every couple of weeks; then put them in shore-to-open-sea journey order."""
    order = list(bds.HABITATS)
    if forced:
        picks = [h for h in forced if h in bds.HABITATS]
    else:
        last_seen = {h: (seen.index(h) if h in seen else math.inf) for h in order}
        shuffled = random.sample(order, len(order))
        picks = sorted(shuffled, key=lambda h: -last_seen[h])[:_PLANNED_CHAPTERS]
    return sorted(picks, key=order.index)


def _samples(n_frames):
    return int(round(n_frames / sr.FPS * sr.SR))


def _words(text):
    return len(text.split())


def _fmt(sec):
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


class _Timeline:
    """Appends rendered pieces: each piece's video goes straight to disk,
    its audio (int16 stereo) is kept, padded/trimmed to the frame count."""

    def __init__(self):
        self.parts, self.audio, self.frames = [], [], 0
        self.speech_sec, self.speech_words = 0.0, 0

    @property
    def seconds(self):
        return self.frames / sr.FPS

    def sec_per_word(self):
        return self.speech_sec / self.speech_words if self.speech_words else 1 / 2.3

    def add(self, frames, wav=None, words=0):
        path = str(sr.WORK / f"disc_part{len(self.parts):03d}.mp4")
        ImageSequenceClip(frames, fps=sr.FPS).write_videofile(
            path, fps=sr.FPS, codec="libx264", audio=False, preset="medium",
            ffmpeg_params=_X264, logger=None)
        n = _samples(len(frames))
        self.audio.append(sr._wav_samples(wav, n) if wav else np.zeros((n, 2), dtype=np.int16))
        self.parts.append(path)
        self.frames += len(frames)
        if words:
            self.speech_sec += len(frames) / sr.FPS
            self.speech_words += words

    def write(self, out_path):
        listing = sr.WORK / "disc_parts.txt"
        listing.write_text("".join(f"file '{pathlib.Path(p).as_posix()}'\n" for p in self.parts), encoding="utf-8")
        video = str(sr.WORK / "disc_video.mp4")
        subprocess.run([sr.FF, "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", video],
                       check=True, capture_output=True)
        audio = np.vstack(self.audio).astype(np.float32)
        bed = _music_bed(len(audio))
        if bed is not None:
            audio += bed.astype(np.float32) * _MUSIC_GAIN
        wav_path = str(sr.WORK / "disc_audio.wav")
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr.SR)
            w.writeframes(np.clip(audio, -32768, 32767).astype(np.int16).tobytes())
        subprocess.run([sr.FF, "-y", "-i", video, "-i", wav_path, "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                        "-shortest", "-movflags", "+faststart", out_path], check=True, capture_output=True)


def _music_bed(n_samples):
    src = sr.ASSET / "music" / "theme.mp3"
    if not src.exists():
        print("  (no background music found)")
        return None
    mw = str(sr.WORK / "disc_music.wav")
    subprocess.run([sr.FF, "-y", "-i", str(src), "-ac", "2", "-ar", str(sr.SR), mw], check=True, capture_output=True)
    with wave.open(mw, "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, 2)
    return np.tile(a, (int(np.ceil(n_samples / len(a))), 1))[:n_samples]


def _clean(tag, keep=None):
    """Delete a segment's downloaded/rejected clips (dozens per episode)."""
    for f in sr.WORK.glob(f"{tag}_*"):
        if keep is None or str(f) != keep:
            f.unlink(missing_ok=True)


def _fetch(seg, tag, used, soft):
    got = brf.fetch_footage(seg["search_query"], tag, sr.WORK, sr.FF, used,
                            subject=seg["subject"], avoid=soft, kind=seg["kind"])
    _clean(tag, keep=got[0] if got else None)
    return got


def _render(tl, text, clip, tag, character, hook_text=None):
    frames, wav = brr.render_real_scene(text, clip, tag, character, hook_text=hook_text)
    tl.add(frames, wav, _words(text))
    del frames


def _fit(found, texts, room_sec, sec_per_word):
    """Drop middle segments (keeping the welcome and the goodbye) until the
    chapter fits in `room_sec`."""
    pairs = list(zip(found, texts))
    while (len(pairs) > _MIN_CHAPTER_SEGMENTS
           and _CARD_SEC + sum(_words(t) for _, t in pairs) * sec_per_word > room_sec):
        pathlib.Path(pairs[-2][0][1]).unlink(missing_ok=True)
        del pairs[-2]
    return pairs


def _shoot_chapter(tl, n, habitat, guide, used, soft, budget_sec):
    """Fetch + verify footage for one habitat, then render its card and
    segments, trimmed to end by `budget_sec`. Returns False (adding nothing)
    if too little footage verified."""
    chapter = bds.write_chapter(habitat, guide)
    found = []
    for i, seg in enumerate(chapter["segments"]):
        print(f"  [{habitat}] {seg['subject']}: '{seg['search_query']}'")
        got = _fetch(seg, f"c{n}s{i}", used, soft)
        if got:
            found.append((seg, *got))
    if len(found) < _MIN_CHAPTER_SEGMENTS:
        print(f"  {habitat}: only {len(found)} verified clip(s), skipping this habitat")
        for _, path, _ in found:
            pathlib.Path(path).unlink(missing_ok=True)
        return False
    texts = bds.match_chapter(habitat, guide, [(seg, shows) for seg, _, shows in found])
    pairs = _fit(found, texts, budget_sec - tl.seconds, tl.sec_per_word())
    card, _ = sr._card(habitat, f"with {guide.title()}", dur=_CARD_SEC, character=guide)
    tl.add(card)
    for i, ((_, path, _), text) in enumerate(pairs):
        _render(tl, text, path, f"c{n}s{i}", guide)
        pathlib.Path(path).unlink(missing_ok=True)
    print(f"  {habitat}: {len(pairs)} segments, total now {_fmt(tl.seconds)}")
    return True


def _bookend(seg, tag, used, soft, fallback=None):
    got = _fetch(seg, tag, used, soft)
    return got[0] if got else fallback


def build_discovery(out_path, forced_habitats=None):
    seen, past_titles, hard, soft = _history()
    habitats = _pick_habitats(seen, forced_habitats)
    week = datetime.date.today().isocalendar()[1]
    guides = [_GUIDES[(week + i) % len(_GUIDES)] for i in range(len(habitats))]
    frame = bds.write_frame(habitats, past_titles)
    hook = frame["search_hook"].strip()
    print(f"DISCOVERY: {frame['title']} — {', '.join(habitats)}")

    used = set(hard)
    tl = _Timeline()
    intro_clip = _bookend({"search_query": "ocean waves aerial", "subject": "ocean waves", "kind": "place"},
                          "intro", used, soft)
    if not intro_clip:
        raise SystemExit("No verified opening footage — not publishing.")
    _render(tl, frame["intro"], intro_clip, "intro", "bini", hook_text=f"{hook[0].upper()}{hook[1:]}?")
    chapters = [(0.0, "Welcome")]

    outro_est = lambda: _words(frame["outro"]) * tl.sec_per_word() + _END_CARD_SEC
    for n, (habitat, guide) in enumerate(zip(habitats, guides)):
        budget = _MAX_SEC - outro_est()
        smallest = _CARD_SEC + _MIN_CHAPTER_SEGMENTS * 25 * tl.sec_per_word()
        if tl.seconds >= _TARGET_SEC or tl.seconds + smallest > budget:
            break
        start = tl.seconds
        if _shoot_chapter(tl, n, habitat, guide, used, soft, budget):
            chapters.append((start, habitat))

    shown = [h for _, h in chapters[1:]]
    if len(shown) < 3:
        raise SystemExit(f"Only {len(shown)} habitat(s) had verified footage — not publishing.")
    outro_clip = _bookend({"search_query": "ocean sunset", "subject": "ocean waves", "kind": "place"},
                          "outro", used, soft, fallback=intro_clip)
    chapters.append((tl.seconds, "Goodbye"))
    _render(tl, frame["outro"], outro_clip, "outro", "bini")
    end, _ = sr._card("Bini's Ocean Discovery", "A new journey every week!", dur=_END_CARD_SEC)
    tl.add(end)

    if tl.seconds < _MIN_SEC:
        raise SystemExit(f"Episode is only {_fmt(tl.seconds)} — under the {_fmt(_MIN_SEC)} minimum, not publishing.")
    tl.write(out_path)
    print(f"wrote {out_path} ({_fmt(tl.seconds)}, {len(shown)} habitats)")

    meta = {"title": frame["title"], "series_title": SERIES_TITLE, "character": "bini",
            "narrator": "Bini & friends", "search_hook": hook, "creature": "",
            "habitats": shown, "chapters": [[_fmt(t), name] for t, name in chapters],
            "footage_ids": sorted(used - hard), "learning_objective": frame["intro"],
            "platforms": ["tiktok", "facebook"], "facebook_type": "post"}
    pathlib.Path("episode_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "discovery.mp4"
    forced = [h.strip() for h in (sys.argv[2] if len(sys.argv) > 2 else os.environ.get("DISCOVERY_HABITATS", "")).split(",") if h.strip()]
    build_discovery(out, forced or None)
