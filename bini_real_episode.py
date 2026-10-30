"""One-command "Bini's Real Ocean" episode factory: generate a behavior-based
script -> fetch real footage per behavior -> render the narrator over each
-> stitch segments (+ Bini's song) + an end card, with background music.

Run: python bini_real_episode.py ["optional theme"] out.mp4 [character]
Mirrors seabini_render.build_episode()'s stitching pattern so both series
produce consistent output (same W/H/FPS, same audio-mixing approach).
"""
import json, pathlib, random, subprocess, sys, urllib.request, wave
import numpy as np
from moviepy.editor import ImageSequenceClip

import seabini_render as sr
import bini_real_footage as brf
import bini_real_render as brr
from bini_real_script import CREATURES, generate_real_episode

SERIES_TITLE = "Bini's Real Ocean"
_MANIFEST_URL = "https://raw.githubusercontent.com/oceanfarm1992-design/ai-drama-showcase/master/manifest.json"
_RECENT_CREATURES = 15   # with 22 creatures and 5 episodes/day, no animal repeats within 3 days
_RECENT_EPISODES_FOR_CLIPS = 15   # clips from these are never reused
_SOFT_EPISODES_FOR_CLIPS = 60     # clips from these are only used if nothing fresher fits
_RECENT_TITLES = 30
_MIN_SEGMENTS = 2
_MUSIC_GAIN = 0.15
_END_CARD_SEC = 1.8
# Facebook Reels rejects videos over 1m30s; leave a little headroom for
# encoder rounding. Bini's song is trimmed (with a fade) to fit.
_MAX_EPISODE_SEC = 88.0
_MIN_SONG_SEC = 20.0      # shorter than this the song feels cut off, so drop it
_SONG_FADE_SEC = 2.5


def _recent_history():
    """Best-effort (creatures, hard_clips, soft_clips, titles) from the most
    recent episodes in the public manifest, so a new episode avoids repeating
    them. Public repo, no auth needed; any failure just means no exclusion."""
    try:
        data = json.loads(urllib.request.urlopen(_MANIFEST_URL, context=sr._SSL, timeout=10).read())
    except Exception:
        return [], set(), set(), []
    eps = [e for e in data.get("episodes", []) if e.get("series_title") == SERIES_TITLE]
    creatures = []
    for ep in eps[:_RECENT_CREATURES]:
        c = ep.get("creature") or next((c for c in CREATURES if c in ep.get("title", "").lower()), None)
        if c:
            creatures.append(c)
    clips = {i for ep in eps[:_RECENT_EPISODES_FOR_CLIPS] for i in ep.get("footage_ids", [])}
    soft = {i for ep in eps[:_SOFT_EPISODES_FOR_CLIPS] for i in ep.get("footage_ids", [])}
    titles = [ep.get("title", "") for ep in eps[:_RECENT_TITLES]]
    return creatures, clips, soft - clips, titles


def _pick_theme(theme, recent_creatures):
    """The LLM is unreliable at picking a genuinely random creature when
    just asked to "pick any" — it's primed by its own prompt describing
    Bini as a seahorse and gravitates there. Pick uniformly at random
    ourselves instead, excluding whatever recent episodes covered."""
    if theme:
        return theme
    pool = [c for c in CREATURES if c not in recent_creatures]
    return random.choice(pool or CREATURES)


def _music_bed(n_samples):
    """Background music (stereo int16) looped/trimmed to n_samples, or None.
    theme.mp3 is the committed copy; *.wav is gitignored, so CI only has the mp3."""
    for name in ("theme.mp3", "theme.wav"):
        src = sr.ASSET / "music" / name
        if src.exists():
            break
    else:
        return None
    mw = str(sr.WORK / "real_music.wav")
    subprocess.run([sr.FF, "-y", "-i", str(src), "-ac", "2", "-ar", str(sr.SR), mw], check=True, capture_output=True)
    w = wave.open(mw, "rb")
    a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, 2)
    w.close()
    return np.tile(a, (int(np.ceil(n_samples / len(a))), 1))[:n_samples]


def _samples(frames):
    return int(round(len(frames) / sr.FPS * sr.SR))


def _fit_song(song_wav, budget):
    """Trim the song to `budget` seconds with a fade-out so the episode stays
    under the platform length limit. Returns the wav to use, or None if the
    budget is too short for the song to be worth keeping."""
    w = wave.open(song_wav, "rb")
    dur = w.getnframes() / w.getframerate()
    w.close()
    if dur <= budget:
        return song_wav
    if budget < _MIN_SONG_SEC:
        print(f"  song skipped: only {budget:.1f}s left under the {_MAX_EPISODE_SEC:.0f}s cap")
        return None
    trimmed = str(sr.WORK / "realsong0_fit.wav")
    fade_start = max(0.0, budget - _SONG_FADE_SEC)
    subprocess.run([sr.FF, "-y", "-i", song_wav, "-t", f"{budget:.2f}",
                    "-af", f"afade=t=out:st={fade_start:.2f}:d={_SONG_FADE_SEC}", trimmed],
                   check=True, capture_output=True)
    print(f"  song trimmed {dur:.1f}s -> {budget:.1f}s to fit the {_MAX_EPISODE_SEC:.0f}s cap")
    return trimmed


def build_real_episode(theme, out_path, character="bini"):
    recent_creatures, recent_clips, older_clips, recent_titles = _recent_history()
    theme = _pick_theme(theme, recent_creatures)
    episode = generate_real_episode(theme, character, avoid_titles=recent_titles)
    creature = episode["creature"]
    name = character.title()
    title = episode.get("title", f"{name}'s Real Ocean: {creature}")
    hook = episode.get("search_hook", "").strip()
    print(f"EPISODE: {title} ({creature}) — narrated by {character}")

    # Open straight on real footage with the search question on screen:
    # on Shorts/TikTok the first second decides whether a viewer swipes away,
    # so the branded title card moved to the end.
    segments, segment_videos = [], []
    used_clips = set(recent_clips)
    for i, seg in enumerate(episode["segments"]):
        query = seg.get("search_query", creature)
        print(f"  [{seg.get('behavior', '?')}] fetching '{query}' ...")
        video_path = brf.fetch_footage(query, f"seg{i}", sr.WORK, sr.FF, used_clips, subject=creature,
                                       avoid=older_clips)
        if not video_path:
            print("    no suitable footage, skipping this segment")
            continue
        hook_text = f"{hook[0].upper()}{hook[1:]}?" if hook and not segments else None
        frames, wav = brr.render_real_scene(seg["narration"], video_path, f"seg{i}", character, hook_text=hook_text)
        segments.append((frames, wav))
        segment_videos.append(video_path)
        print(f"    rendered {len(frames)} frames")

    if len(segments) < _MIN_SEGMENTS:
        raise SystemExit(f"Only {len(segments)} segment(s) had usable footage — not publishing a too-short episode.")

    all_frames, chunks = [], []
    for frames, wav in segments:
        all_frames.extend(frames)
        chunks.append(sr._wav_samples(wav, _samples(frames)))
    narration_audio = np.vstack(chunks).astype(np.float32)

    end_frames, _ = sr._card(f"{name}'s Real Ocean", "New episode every day!", dur=_END_CARD_SEC, character=character)
    end_len = _samples(end_frames)
    bed = _music_bed(len(narration_audio) + end_len)
    end_audio = np.zeros((end_len, 2), dtype=np.float32)
    if bed is not None:
        narration_audio += bed[:len(narration_audio)].astype(np.float32) * _MUSIC_GAIN
        end_audio += bed[len(narration_audio):].astype(np.float32) * _MUSIC_GAIN
    else:
        print("  (no background music found)")

    song = episode.get("song")
    if song and song.get("lyrics"):
        print("generating song...")
        song_mp3 = sr.get_song(song["lyrics"], "realsong0")
        song_wav = sr._song_to_wav(song_mp3, "realsong0")
        budget = _MAX_EPISODE_SEC - len(all_frames) / sr.FPS - _END_CARD_SEC
        song_wav = _fit_song(song_wav, budget)
        if song_wav:
            song_frames, _ = brr.render_real_dance_scene(song_wav, segment_videos[0], "realsong0", character)
            all_frames = all_frames + song_frames
            narration_audio = np.vstack([narration_audio, sr._wav_samples(song_wav, _samples(song_frames)).astype(np.float32)])
            print("added song:", song.get("title"), round(len(song_frames) / sr.FPS, 1), "s")

    all_frames = all_frames + end_frames
    final = np.clip(np.vstack([narration_audio, end_audio]), -32768, 32767).astype(np.int16)
    wav_path = str(sr.WORK / "real_episode_audio.wav")
    ww = wave.open(wav_path, "wb")
    ww.setnchannels(2); ww.setsampwidth(2); ww.setframerate(sr.SR); ww.writeframes(final.tobytes())
    ww.close()

    silent = str(sr.WORK / "real_episode_silent.mp4")
    ImageSequenceClip(all_frames, fps=sr.FPS).write_videofile(
        silent, fps=sr.FPS, codec="libx264", audio=False, preset="medium", logger=None)
    subprocess.run([sr.FF, "-y", "-i", silent, "-i", wav_path,
                    "-c:v", "copy", "-c:a", "aac", "-shortest", out_path], check=True, capture_output=True)
    print(f"wrote {out_path} ({round(len(all_frames) / sr.FPS, 1)}s)")

    meta = {"title": title, "creature": creature, "series_title": SERIES_TITLE,
            "character": character, "narrator": name, "search_hook": hook,
            "footage_ids": sorted(used_clips - recent_clips),
            "learning_objective": episode["segments"][0]["narration"]}
    pathlib.Path("episode_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    theme = sys.argv[1] if len(sys.argv) > 1 else ""
    out = sys.argv[2] if len(sys.argv) > 2 else "episode.mp4"
    character = sys.argv[3] if len(sys.argv) > 3 else "bini"
    build_real_episode(theme, out, character)
