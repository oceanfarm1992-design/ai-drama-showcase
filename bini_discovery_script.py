"""Script writer for "Bini's Ocean Discovery" — the weekly long-form (8-10
minute) episode. Bini hosts a journey through several sea habitats; a
different friend guides each habitat "chapter" over real stock footage.

Two kinds of LLM call (gpt-4o-mini, ~$0.01/episode in total):
  - write_frame():   episode title, search question, Bini's intro + outro
  - write_chapter(): one habitat's 6-8 narrated segments, each with a footage
                     subject chosen from that habitat's curated list
match_chapter() then rewrites each line to fit the clip actually found.
"""
import json, urllib.request

import seabini_script as ss  # reuse _openai_key()
from bini_real_script import CHARACTER_PROFILES

# Subjects are limited to ones with plenty of free stock footage whose
# Pexels/Pixabay metadata actually names them (probed 2026-11-13). Kelp,
# manatee, dugong, parrotfish etc. had under 10 usable clips, so they're out.
# Listed in journey order: from the shore out to the open and polar seas.
HABITATS = {
    "Sandy Beach":       {"places": ["sandy beach", "ocean waves"],
                          "animals": ["crab", "pelican", "seagull", "sea turtle"]},
    "Rocky Shore":       {"places": ["rock pool", "seaweed"],
                          "animals": ["starfish", "sea anemone", "crab", "sea snail", "sea urchin", "seagull"]},
    "Mangrove Forest":   {"places": ["mangrove"],
                          "animals": ["heron", "egret", "kingfisher", "crab", "school of fish"]},
    "Lagoon":            {"places": ["lagoon"],
                          "animals": ["flamingo", "heron", "stingray", "school of fish"]},
    "Seagrass Meadow":   {"places": ["sea grass"],
                          "animals": ["sea turtle", "seahorse", "stingray", "starfish", "school of fish"]},
    "Coral Reef":        {"places": ["coral reef", "coral"],
                          "animals": ["reef fish", "clownfish", "sea anemone", "sea turtle", "octopus", "stingray"]},
    "Shipwreck Reef":    {"places": ["shipwreck", "sea cave"],
                          "animals": ["school of fish", "sea turtle", "octopus", "reef fish"]},
    "Seaweed Forest":    {"places": ["seaweed"],
                          "animals": ["sea lion", "seal", "otter", "sea urchin", "school of fish"]},
    "Open Ocean":        {"places": ["ocean waves"],
                          "animals": ["dolphin", "whale", "manta ray", "whale shark", "jellyfish", "school of fish"]},
    "Icy Polar Seas":    {"places": ["sea ice", "iceberg"],
                          "animals": ["penguin", "seal", "polar bear", "whale"]},
    "Glowing Night Sea": {"places": ["bioluminescent sea"],
                          "animals": ["jellyfish", "plankton"]},
}
_MIN_SEGMENTS, _MAX_SEGMENTS = 6, 8
_MIN_WORDS_PER_LINE = 15

_SAFETY = """SAFETY (this is REAL footage for ages 2-6):
- Only peaceful, observable behaviour: swimming, floating, resting, walking,
  eating plants/plankton/small harmless food, hiding, playing, flying, nesting.
- NEVER hunting, attacking, being eaten, fighting, biting, storms, danger,
  pollution or anything sad. Defence is framed gently ("it hides in its shell").
- Warm, simple words a 2-6 year old understands. Short sentences.
- Facts must be true, simple and well known."""


def _chat(system, user, temperature=0.8):
    body = {"model": "gpt-4o-mini", "response_format": {"type": "json_object"}, "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {ss._openai_key()}", "Content-Type": "application/json"})
    return json.loads(json.load(urllib.request.urlopen(req, timeout=120))["choices"][0]["message"]["content"])


def _retrying(what, call, problems):
    """Up to 3 attempts of call(); problems(result) lists why it's unusable."""
    probs = []
    for attempt in range(3):
        try:
            out = call()
        except Exception as exc:
            probs = [f"request/parse failed: {exc}"]
        else:
            probs = problems(out)
            if not probs:
                return out
        print(f"[discovery-script] {what} attempt {attempt + 1} unusable: {'; '.join(probs)}")
    raise SystemExit(f"[discovery-script] {what} failed after 3 attempts: {'; '.join(probs)}")


def _str(d, key):
    return isinstance(d.get(key), str) and d[key].strip()


def write_frame(habitats, avoid_titles):
    """{"title", "search_hook", "intro", "outro"} for this week's journey."""
    system = f"""You write "Bini's Ocean Discovery", a weekly ~9 minute preschool (ages 2-6)
nature show. {CHARACTER_PROFILES['bini']} Bini hosts a journey through several real
sea habitats; friends guide each one. You write only the episode frame.

{_SAFETY}

Return ONLY JSON:
{{"title": "Bini's Ocean Discovery: <short journey name, e.g. From Mangroves to Coral Reefs>",
 "search_hook": "the question a parent would type into search that this episode answers, lowercase,
                 no punctuation, 6-10 words, e.g. what animals live in a mangrove forest",
 "intro": "Bini's welcome, 3-4 short sentences (40-60 words): greet the little explorers,
           say we'll visit these habitats today, invite them to look closely",
 "outro": "Bini's goodbye, 3 short sentences (35-50 words): recall 2-3 favourite moments
           by habitat, praise the explorers, invite them back next week"}}"""
    user = f"Today's habitats, in order: {', '.join(habitats)}."
    if avoid_titles:
        user += f"\nEarlier titles (choose a different one): {'; '.join(avoid_titles[:10])}"

    def problems(f):
        probs = [f"missing {k}" for k in ("title", "search_hook", "intro", "outro") if not _str(f, k)]
        if not probs and len(f["intro"].split()) < 25:
            probs.append("intro too short")
        return probs
    return _retrying("frame", lambda: _chat(system, user), problems)


def write_chapter(habitat, guide):
    """{"title", "segments": [{"subject", "kind", "search_query", "narration"}]}"""
    spec = HABITATS[habitat]
    allowed = {s: "place" for s in spec["places"]} | {s: "animal" for s in spec["animals"]}
    system = f"""You write one chapter of "Bini's Ocean Discovery", a preschool (ages 2-6) nature
show over REAL stock footage. This chapter visits the {habitat}, guided by
{CHARACTER_PROFILES[guide]} Speak in the guide's own voice and personality.

{_SAFETY}

Write {_MIN_SEGMENTS}-{_MAX_SEGMENTS} segments. Each plays over one real video clip:
- "subject": EXACTLY one of these footage subjects —
    places: {', '.join(spec['places'])}
    animals: {', '.join(spec['animals'])}
  The FIRST segment's subject must be a place: the guide welcomes the
  viewer to the {habitat} and says what it looks like. After that, mostly
  animals; use each subject at most twice.
- "search_query": 2-4 word stock-footage search containing the subject,
  concrete and visual (e.g. "heron walking mangrove", "sea turtle eating seagrass").
- "narration": 2-3 short sentences (25-35 words): react to what we see, then
  one simple true fact about how this animal lives in the {habitat}.
  The last segment says a cheerful goodbye to the {habitat} (don't name
  the next place).

Return ONLY JSON:
{{"title": "{habitat}", "segments": [{{"subject": "...", "search_query": "...", "narration": "..."}}]}}"""

    def problems(c):
        segs = c.get("segments")
        if not isinstance(segs, list) or not _MIN_SEGMENTS <= len(segs) <= _MAX_SEGMENTS:
            return [f"need {_MIN_SEGMENTS}-{_MAX_SEGMENTS} segments"]
        probs = []
        for i, s in enumerate(segs):
            if not isinstance(s, dict) or not all(_str(s, k) for k in ("subject", "search_query", "narration")):
                probs.append(f"segment {i} incomplete")
            elif s["subject"].strip().lower() not in allowed:
                probs.append(f"segment {i} subject '{s['subject']}' not allowed")
            elif len(s["narration"].split()) < _MIN_WORDS_PER_LINE:
                probs.append(f"segment {i} narration too short")
        if not probs and allowed[segs[0]["subject"].strip().lower()] != "place":
            probs.append("first segment must show the place")
        return probs

    chapter = _retrying(habitat, lambda: _chat(system, f"Write the {habitat} chapter."), problems)
    for s in chapter["segments"]:
        s["subject"] = s["subject"].strip().lower()
        s["kind"] = allowed[s["subject"]]
    chapter["title"] = habitat
    return chapter


def match_chapter(habitat, guide, clips):
    """Rewrite each line to talk about what its clip really shows (`clips`:
    [(segment, shows)]), keeping the planned fact. Falls back to the plan."""
    original = [seg["narration"] for seg, _ in clips]
    lines = [{"subject": seg["subject"], "planned_narration": seg["narration"], "clip_shows": shows}
             for seg, shows in clips]
    system = (f"You narrate a preschool (ages 2-6) nature show as {CHARACTER_PROFILES[guide]} "
              f"We are visiting the {habitat}. For each clip, rewrite the planned narration as 2-3 short, "
              "warm sentences in the narrator's voice: first react to what the clip ACTUALLY shows (never "
              "claim it shows something it doesn't), then keep the planned fun fact as a general statement. "
              "Keep the first line's welcome and the last line's goodbye. Never mention cameras, footage, "
              "clips or videos. Facts must be true and simple. "
              'Return JSON: {"narrations": ["...", ...]} with exactly one entry per clip, in order.')
    try:
        new = _chat(system, json.dumps(lines, ensure_ascii=False), temperature=0.7).get("narrations")
        if (isinstance(new, list) and len(new) == len(original)
                and all(isinstance(n, str) and len(n.split()) >= 10 for n in new)):
            return new
        print(f"[discovery-script] {habitat}: narration rewrite malformed, keeping the plan")
    except Exception as exc:
        print(f"[discovery-script] {habitat}: narration rewrite failed ({exc}), keeping the plan")
    return original
