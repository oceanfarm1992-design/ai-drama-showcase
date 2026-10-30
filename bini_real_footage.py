"""Fetches real stock video footage for the "Bini's Real Ocean" series.

Candidates come from Pexels (often native vertical 9:16) and Pixabay, both
free for commercial use with no attribution required. Three filters decide
which clip is used:
  1. relevance - clips whose Pexels page slug / Pixabay tags name the
     subject creature are tried first (stock search is fuzzy: a "walrus"
     query happily returns generic ocean footage);
  2. novelty - clip ids already used recently are skipped;
  3. a vision check on 3 frames - the creature must actually be visible,
     ALIVE in water or its natural habitat (not food, cooking, a market or
     a decoration), and nothing may be scary for a 2-6 year old (this is
     real footage, so the script's safety rules can't guarantee what the
     camera shows). A clip that fails is never used; if the check can't run,
     the clip is rejected rather than trusted.
Clips whose metadata talks about food are dropped before download: stock
searches for squid/octopus/shrimp/urchin return lots of cooking footage.
"""
import base64, json, os, pathlib, random, re, ssl, subprocess, urllib.parse, urllib.request

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()

HERE = pathlib.Path(__file__).resolve().parent
_MIN_DUR, _MAX_DUR = 6, 40  # seconds — long enough to loop cleanly, short enough to stay small
_MAX_TRIES = 4              # candidate clips downloaded + vision-checked per segment
_EXTRA_TRIES = 3            # more tries from broader searches before giving up on a segment
_REJECTED = set()           # clips that failed the check this run; never retried for another segment
_STOP = {"sea", "of", "the", "school", "moon", "a"}
# Whole words from a clip's slug/tags that mean food, cooking or fishing.
_FOOD = {
    "cook", "cooks", "cooking", "cooked", "cuisine", "recipe", "dish", "dishes", "food", "foods",
    "meal", "dinner", "lunch", "snack", "restaurant", "kitchen", "chef", "plate", "plated",
    "fry", "fried", "frying", "grill", "grilled", "grilling", "roast", "roasted", "boiled",
    "boiling", "steamed", "baked", "stew", "soup", "sauce", "pan", "wok", "calamari", "sushi",
    "sashimi", "takoyaki", "seafood", "tempura", "paella", "ceviche", "market", "fishmonger",
    "dried", "cocktail", "drink", "drinks", "bar", "delicious", "tasty", "menu", "game",
}
# Pixabay's CDN (and some Pexels edge cases) 403 requests with Python's default
# urllib User-Agent; a normal browser UA avoids that.
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _api_key(name):
    k = os.environ.get(name)
    if k:
        return k
    apis = HERE / ".APIs.txt"
    if apis.exists():
        prefix = {"PEXELS_API_KEY": "pexels-secret=", "PIXABAY_API_KEY": "pixabay-secret="}[name]
        for line in apis.read_text(encoding="utf-8-sig").splitlines():
            if line.lower().startswith(prefix.lower()):
                return line.split("=", 1)[1].strip()
    return None


def _get_json(url, headers):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **headers})
    return json.loads(urllib.request.urlopen(req, context=_SSL, timeout=20).read())


def _pexels_search(query):
    """[(clip_id, download_url, descriptive_text, duration)]"""
    key = _api_key("PEXELS_API_KEY")
    if not key:
        return []
    url = f"https://api.pexels.com/videos/search?query={urllib.parse.quote(query)}&per_page=40&orientation=portrait"
    try:
        data = _get_json(url, {"Authorization": key})
    except Exception as exc:
        print(f"[footage] Pexels search failed: {exc}")
        return []
    out = []
    for v in data.get("videos", []):
        if not (_MIN_DUR <= v.get("duration", 0) <= _MAX_DUR):
            continue
        # Prefer the file closest to our target 720x1280 without upscaling past it
        files = sorted(v["video_files"], key=lambda f: abs(f.get("width", 0) - 720))
        for f in files:
            if f.get("width", 0) >= 720 and f.get("file_type") == "video/mp4":
                out.append((f"pexels:{v['id']}", f["link"], v.get("url", ""), v["duration"]))
                break
    return out


def _pixabay_search(query):
    """[(clip_id, download_url, descriptive_text, duration)]"""
    key = _api_key("PIXABAY_API_KEY")
    if not key:
        return []
    url = f"https://pixabay.com/api/videos/?key={key}&q={urllib.parse.quote(query)}&per_page=40"
    try:
        data = _get_json(url, {})
    except Exception as exc:
        print(f"[footage] Pixabay search failed: {exc}")
        return []
    out = []
    for v in data.get("hits", []):
        if not (_MIN_DUR <= v.get("duration", 0) <= _MAX_DUR):
            continue
        for tier in ("medium", "large", "small"):
            if tier in v["videos"]:
                out.append((f"pixabay:{v['id']}", v["videos"][tier]["url"], v.get("tags", ""), v["duration"]))
                break
    return out


def _is_food(text):
    """Does a clip's slug/tags describe food or cooking rather than a live
    animal? ("game" catches the many Squid Game clips.)"""
    return not _FOOD.isdisjoint(re.findall(r"[a-z]+", text.lower()))


def _mentions(text, subject):
    """Does a clip's slug/tags name the subject? Letters-only matching so
    'clown-fish' matches 'clownfish' and 'sea-turtle' matches 'sea turtle'."""
    norm = re.sub(r"[^a-z]", "", text.lower())
    words = [w for w in subject.lower().split() if w not in _STOP] or [subject.replace(" ", "")]
    # All words must appear: "eagle ray" must not match any clip saying "gray",
    # "mandarin fish" must not match every clip that merely says "fish".
    return all(re.sub(r"[^a-z]", "", w) in norm for w in words)


def _normalize_vertical(src_path, dst_path, ff_exe):
    """Cover-fit crop to exactly 720x1280, whatever the source aspect ratio."""
    subprocess.run([
        ff_exe, "-y", "-i", src_path,
        "-vf", "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280",
        "-an", "-r", "24", dst_path,
    ], check=True, capture_output=True)


def _vision_check(video_path, duration, subject, ff_exe):
    """(usable, reason_or_what_it_shows) from 3 frames: on success the second
    item is a short description of what the clip shows (used to make the
    narration match it), otherwise why it was rejected. If the check can't run (no key,
    network) the clip is rejected: unverified real footage once put cooked
    squid and a sushi bar into kids' episodes."""
    try:
        import seabini_script as ss
        key = ss._openai_key()
    except BaseException:
        return False, "vision check unavailable (no OpenAI key)"
    images = []
    for frac in (0.2, 0.5, 0.8):
        jpg = f"{video_path}.{int(frac * 100)}.jpg"
        subprocess.run([ff_exe, "-y", "-ss", f"{duration * frac:.2f}", "-i", video_path,
                        "-vframes", "1", "-vf", "scale=256:-2", jpg], capture_output=True)
        if os.path.exists(jpg):
            b64 = base64.b64encode(pathlib.Path(jpg).read_bytes()).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"}})
    if not images:
        return False, "could not extract frames"
    prompt = (f"These are 3 frames from a stock clip for a preschool (ages 2-6) nature video about a LIVE {subject}. "
              'Reply with JSON only: {"subject_visible": true/false, "alive_in_habitat": true/false, '
              '"is_food": true/false, "kid_safe": true/false, "shows": "..."}. '
              f"subject_visible = a real {subject} is clearly the main subject in most frames (not a tiny "
              "speck, not a different animal, not a toy, drawing or ride). "
              f"alive_in_habitat = the {subject} is a living animal in the sea, on a shore, or in an aquarium "
              "(false if it is dead, dried, a shell or decoration, held out of water, or a cartoon/toy/costume). "
              "is_food = ANY frame shows food or eating by people: cooking, a pan or wok, a plate or dish, "
              "a restaurant, sushi, a drink or glass, a fish market, or a caught animal. "
              "kid_safe = false if anything could scare or upset a small child: blood, injury, a dead "
              "or eaten animal, hunting/attacking, fishing hooks or nets with caught animals, trash-choked "
              "animals, or anything violent. Divers, boats and ordinary ocean scenes are fine. "
              f"shows = one plain sentence (max 15 words) saying what the {subject} is doing and where.")
    body = {"model": "gpt-4o-mini", "response_format": {"type": "json_object"}, "temperature": 0,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, *images]}]}
    verdict, error = None, ""
    for _attempt in range(2):  # one retry: a transient API error shouldn't cost a good clip
        try:
            req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            verdict = json.loads(json.loads(urllib.request.urlopen(req, context=_SSL, timeout=60).read())
                                 ["choices"][0]["message"]["content"])
            break
        except Exception as exc:
            error = str(exc)
    if verdict is None:
        return False, f"vision check unavailable ({error})"
    if verdict.get("is_food") is not False:
        return False, "shows food/cooking"
    if verdict.get("kid_safe") is not True:
        return False, "not kid-safe"
    if verdict.get("subject_visible") is not True:
        return False, f"no {subject} visible"
    if verdict.get("alive_in_habitat") is not True:
        return False, f"{subject} not alive in its habitat"
    return True, str(verdict.get("shows", "")).strip()


def _download(url, raw_path, out_path, ff_exe):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    pathlib.Path(raw_path).write_bytes(urllib.request.urlopen(req, context=_SSL, timeout=60).read())
    _normalize_vertical(raw_path, out_path, ff_exe)


def _ranked(cands, used, avoid, subject, exclude=()):
    """Dedupe and order candidates: never-used before `used`, relevant
    before not, older-episode clips (`avoid`) after fresh ones."""
    seen, pool = set(exclude) | _REJECTED, []
    for c in cands:
        if _is_food(c[2]):
            continue
        if c[0] not in seen:
            seen.add(c[0]); pool.append(c)
    fresh = [c for c in pool if c[0] not in used] or pool
    random.shuffle(fresh)
    # stable sort: relevant first, then not-recently-used, random within
    fresh.sort(key=lambda c: (not _mentions(c[2], subject), c[0] in avoid))
    return fresh


def fetch_footage(query, tag, work_dir, ff_exe, used=None, subject=None, avoid=None):
    """Returns (local mp4 path, what the clip shows) — normalized to vertical
    720x1280 @24fps — or None if no verified footage exists. `used` is a set of clip ids already
    used (this episode + recent ones); the chosen clip's id is added to it.
    `avoid` holds clip ids from older episodes: allowed, but tried last.
    `subject` is the creature the footage must actually show."""
    used = used if used is not None else set()
    avoid = avoid or set()
    subject = subject or query
    broad = f"{subject} underwater"
    cands = _pexels_search(query) + _pixabay_search(query)
    broadened = len(cands) < 5
    if broadened:  # very specific phrasing found little; broaden once
        cands += _pexels_search(broad) + _pixabay_search(broad)
    fresh = _ranked(cands, used, avoid, subject)
    if not fresh:
        print(f"[footage] No results for '{query}' on Pexels or Pixabay")
        return None

    tried = set()
    for round_no in range(2):
        if round_no == 1:
            # Nothing passed: try a few more from broader searches (the plain
            # creature name, then "<creature> underwater") before giving up.
            more = _pexels_search(subject) + _pixabay_search(subject) if subject != query else []
            if not broadened:
                more += _pexels_search(broad) + _pixabay_search(broad)
            batch = _ranked(fresh + more, used, avoid, subject, exclude=tried)[:_EXTRA_TRIES]
        else:
            batch = fresh[:_MAX_TRIES]
        for clip_id, url, _text, duration in batch:
            n = len(tried)
            tried.add(clip_id)
            raw_path = str(work_dir / f"{tag}_raw{n}.mp4")
            out_path = str(work_dir / f"{tag}_vertical{n}.mp4")
            try:
                _download(url, raw_path, out_path, ff_exe)
            except Exception as exc:
                print(f"[footage] download failed for {clip_id}: {exc}")
                continue
            ok, info = _vision_check(out_path, duration, subject, ff_exe)
            if ok:
                used.add(clip_id)
                print(f"[footage] {clip_id}: '{query}' -> {info}")
                return out_path, info
            _REJECTED.add(clip_id)
            print(f"[footage] rejected {clip_id}: {info}")
    # No clip verifiably shows a live, real one: skip the segment rather than
    # narrate "look at the squid!" over something else.
    return None
