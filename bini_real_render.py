"""Renderer for the "Bini's Real Ocean" series — composites a character's
rigged head (same mouth-viseme rig as the main SEABINI series) over real
stock video footage instead of an AI-generated static background, with an
optional search-question hook and burned-in subtitles.

Reuses seabini_render's character rig, lip-sync, and voice pipeline as-is —
only the background source changes from a static image to a real video clip.
Two layouts: PORTRAIT (720x1280, the shorts and TikTok) and LANDSCAPE
(1280x720, the weekly long video on Facebook/YouTube). One narration can be
drawn in both at once so the voice and lip-sync are made only once.
"""
import math, wave
from dataclasses import dataclass
import numpy as np
from PIL import Image, ImageDraw
from moviepy.editor import VideoFileClip

import seabini_render as sr

FPS = sr.FPS
HOOK_SECONDS = 3.0


@dataclass(frozen=True)
class Layout:
    w: int
    h: int
    corner_scale: float   # narrator head size, relative to the rig's native size
    corner_x: int
    corner_y: int
    sub_y: int
    hook_y: int
    text_max_w: int
    sub_size: int
    hook_size: int


# Real footage isn't directed by us, so we never know where in frame the
# subject creature is. Rather than risk the character covering it, they sit
# as a small "narrator bubble" at the left. It's raised off the very bottom
# because TikTok / Shorts / Reels all overlay their own username + caption
# text on the bottom-left ~20% of the video; text stays clear of the
# right-side action buttons.
PORTRAIT = Layout(w=720, h=1280, corner_scale=0.55, corner_x=int(720 * 0.24), corner_y=int(1280 * 0.74),
                  sub_y=int(1280 * 0.57), hook_y=int(1280 * 0.17), text_max_w=560, sub_size=46, hook_size=54)
# Wide screens (TV, tablet, desktop) have no overlaid app UI except the
# progress bar, so subtitles sit low and centred and the narrator is smaller.
LANDSCAPE = Layout(w=1280, h=720, corner_scale=0.40, corner_x=int(1280 * 0.10), corner_y=int(720 * 0.74),
                   sub_y=int(720 * 0.86), hook_y=int(720 * 0.14), text_max_w=900, sub_size=40, hook_size=48)
LAYOUTS = {"portrait": PORTRAIT, "landscape": LANDSCAPE}


def _wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + ([cur] if cur else [])


def _draw_block(win, text, center_y, size, boxed, max_w):
    """Centered multi-line text with a thick outline (readable on any footage);
    `boxed` adds a translucent panel behind it for the big opening hook."""
    d = ImageDraw.Draw(win)
    font = sr._font(size)
    lines = _wrap(d, text, font, max_w)
    line_h = int(size * 1.2)
    top = center_y - line_h * len(lines) // 2
    cx = win.width / 2
    if boxed:
        panel = Image.new("RGBA", win.size, (0, 0, 0, 0))
        ImageDraw.Draw(panel).rounded_rectangle(
            (cx - max_w / 2 - 24, top - 18, cx + max_w / 2 + 24, top + line_h * len(lines) + 12),
            radius=28, fill=(10, 40, 90, 170))
        win.alpha_composite(panel)
        d = ImageDraw.Draw(win)
    for i, line in enumerate(lines):
        d.text((cx, top + i * line_h + line_h // 2), line, anchor="mm", font=font,
               fill=(255, 255, 255), stroke_width=max(3, size // 12), stroke_fill=(0, 0, 0))


def _subtitle_chunks(text, dur, max_words=5):
    """Split narration into short caption chunks, timed by character count —
    close enough to real speech pacing without a word-level aligner."""
    words = text.split()
    chunks = [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]
    total = sum(len(c) for c in chunks) or 1
    out, t = [], 0.0
    for c in chunks:
        span = dur * len(c) / total
        out.append((t, t + span, c))
        t += span
    return out


def _rig(character):
    ch = sr.CHARACTERS[character]
    base = Image.open(str(sr.ASSET / "characters" / ch["base"])).convert("RGBA")
    s = sr.TH / base.height
    base = base.resize((int(base.width * s), sr.TH))
    return ch, sr._build_heads(base, ch), base.size


def _voice(text, tag, ch):
    """(narration wav, duration, mouth shape at time t) — made once per line."""
    mp3 = sr.get_voiceover(text, tag, ch)
    if sr.VOICE_ENGINE == "kokoro":
        clean, baby = sr._prep_audio(mp3, tag, ch["kokoro_pitch"], ch["kokoro_post_speed"])
    else:
        clean, baby = sr._prep_audio(mp3, tag, ch["pitch"], ch["speed"])
    wf = wave.open(baby, "rb")
    dur = wf.getnframes() / wf.getframerate()
    wf.close()
    cues = sr._cues(clean, dur)
    starts = np.array([c["start"] for c in cues])
    shape_at = lambda t: sr.VMAP.get(
        cues[max(0, min(np.searchsorted(starts, t + 0.05, side="right") - 1, len(cues) - 1))]["value"],
        "closed")
    return baby, dur, shape_at


def render_real_scenes(text, clips, tag, character="bini", hook_text=None, subtitles=True):
    """Draw one narrated line over real footage in one or more layouts.
    `clips` maps a LAYOUTS name to a video already cut to that layout's size.
    Returns ({layout name: frames}, narration wav)."""
    ch, heads, (BW, BH) = _rig(character)
    baby, dur, shape_at = _voice(text, tag, ch)
    blink_times = sr._blink_times(dur, sum(ord(c) for c in tag))
    gaze = sr._gaze_schedule(dur, sum(ord(c) for c in tag))
    subs = _subtitle_chunks(text, dur) if subtitles else []
    videos = {name: VideoFileClip(path) for name, path in clips.items()}

    frames = {name: [] for name in clips}
    for i in range(int(dur * FPS)):
        t = i / FPS
        head = heads[shape_at(t)]
        if ch.get("blink_ready") and sr._is_blinking(t, blink_times): head = sr._blink_frame(head, ch)
        elif ch.get("look_ready"): head = sr._look_frame(head, ch, sr._gaze_at(t, gaze))
        chunk = next((c for start, end, c in subs if start <= t < end), None)
        for name, video in videos.items():
            lay = LAYOUTS[name]
            win = Image.fromarray(video.get_frame(t % video.duration)).convert("RGBA")
            sc = lay.corner_scale * (1 + 0.015 * math.sin(t * 1.5))
            im2 = head.resize((int(BW * sc), int(BH * sc))).rotate(
                3 * math.sin(t * 1.0), expand=True, resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0))
            px = lay.corner_x + 12 * math.sin(t * 0.45)
            py = lay.corner_y + 10 * math.sin(t * 1.5)
            win.alpha_composite(im2, (int(px - im2.width / 2), int(py - im2.height / 2)))
            if hook_text and t < HOOK_SECONDS:
                _draw_block(win, hook_text, lay.hook_y, lay.hook_size, True, lay.text_max_w)
            if chunk:
                _draw_block(win, chunk, lay.sub_y, lay.sub_size, False, lay.text_max_w)
            frames[name].append(np.array(win.convert("RGB")))

    for video in videos.values():
        video.close()
    return frames, baby


def render_real_scene(text, video_path, tag, character="bini", hook_text=None, subtitles=True):
    """Portrait-only version (the daily shorts): (frames, narration wav)."""
    frames, baby = render_real_scenes(text, {"portrait": video_path}, tag, character, hook_text, subtitles)
    return frames["portrait"], baby


def render_card(title, sub, dur, character, layout):
    """Title card (cartoon reef + bobbing character) in any layout. Portrait
    is seabini_render's own card, so the shorts look exactly as before."""
    if layout is PORTRAIT:
        return sr._card(title, sub, dur=dur, character=character)[0]
    bg = Image.open(sr.bg_for("Rainbow Reef")).convert("RGB")
    s = max(layout.w / bg.width, layout.h / bg.height)
    bg = bg.resize((int(bg.width * s) + 1, int(bg.height * s) + 1))
    x0, y0 = (bg.width - layout.w) // 2, (bg.height - layout.h) // 2
    frame0 = bg.crop((x0, y0, x0 + layout.w, y0 + layout.h)).convert("RGBA")
    head = Image.open(str(sr.ASSET / "characters" / sr.CHARACTERS[character]["base"])).convert("RGBA")
    hh = int(layout.h * 0.55)
    head = head.resize((int(head.width * hh / head.height), hh))
    size = 96 if len(title) <= 14 else max(52, int(96 * 14 / len(title)))
    frames = []
    for i in range(int(dur * FPS)):
        t = i / FPS
        f = frame0.copy()
        f.alpha_composite(head, (int(layout.w / 2 - head.width / 2),
                                 int(layout.h * 0.62 - head.height / 2 + 10 * math.sin(t * 1.6))))
        d = ImageDraw.Draw(f)
        d.text((layout.w / 2, 90), title, anchor="mm", font=sr._font(size), fill=(255, 255, 255),
               stroke_width=6, stroke_fill=(30, 90, 160))
        d.text((layout.w / 2, 170), sub, anchor="mm", font=sr._font(40), fill=(255, 255, 255),
               stroke_width=3, stroke_fill=(30, 90, 160))
        frames.append(np.array(f.convert("RGB")))
    return frames
