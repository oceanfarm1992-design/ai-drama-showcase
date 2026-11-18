"""Publishes a finished episode: uploads the video as a GitHub Release asset
(public URL), updates manifest.json via the GitHub API, and posts to
YouTube / TikTok / Facebook via Buffer's GraphQL API.

Env: GH_TOKEN (the workflow's GITHUB_TOKEN), BUFFER_API_KEY.
Run: python seabini_publish.py <video.mp4> --meta episode_meta.json
The meta file may limit "platforms", set "facebook_type" ("post" for
long-form video; the default "reel" is for shorts), add "extra_videos"
({cut name: file}) uploaded to the same release, and pick a cut per
platform with "platform_video" ({service: cut name}; the main file is the
"portrait" cut).
"""
import base64, os, sys, json, datetime, pathlib, shutil, ssl, subprocess, time, urllib.error, urllib.request

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

SERIES_TITLE = "SEABINI"
REPO = os.environ.get("GITHUB_REPOSITORY", "oceanfarm1992-design/ai-drama-showcase")

# Channel IDs confirmed 2026-09-08 via buffer_channels.py
_BUFFER_CHANNELS = {
    "youtube":  "6aa047f4cd8b9c702c2e9786",
    "tiktok":   "6aa048c7cd8b9c702c2e9b3b",
    "facebook": "6aa047c5cd8b9c702c2e96ac",
}
# Facebook Reels rejects anything longer than 1m30s (Buffer InvalidInputError);
# long videos go up as regular Facebook video posts instead (type "post").
_MAX_REEL_SEC = 90.0
_RETRY_DELAYS = (5, 15, 45)  # seconds between attempts on GitHub 5xx / network errors
# Buffer sometimes can't fetch a just-uploaded release asset yet ("Video could
# not be read from its URL" took out a whole day on 2026-11-08); one delayed
# retry of just those posts is cheap.
_UNREADABLE = "could not be read"
_UNREADABLE_RETRY_SEC = 90
DISCOVERY = "Bini's Ocean Discovery"

_CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id status } }
    ... on InvalidInputError { message }
    ... on UnauthorizedError { message }
    ... on UnexpectedError { message }
    ... on NotFoundError { message }
    ... on RestProxyError { message }
    ... on LimitReachedError { message }
  }
}
"""


def _gh_token():
    raw = os.environ.get("GH_TOKEN") or ""
    return raw.strip().lstrip("﻿")


def _buffer_key():
    k = os.environ.get("BUFFER_API_KEY")
    if k:
        return k
    apis = pathlib.Path(__file__).with_name(".APIs.txt")
    if apis.exists():
        for line in apis.read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("buffer-secret="):
                return line.split("=", 1)[1].strip()
    return None


def _buffer_gql(query, variables):
    key = _buffer_key()
    if not key:
        print("[Buffer] No API key — skipping social post.")
        return None
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.buffer.com/graphql",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        return json.loads(urllib.request.urlopen(req, context=_SSL_CTX, timeout=30).read())
    except Exception as exc:
        print(f"[Buffer] GraphQL error: {exc}")
        return None


_HASHTAGS_TT = (
    "#SEABINI #kidscartoon #cartoonforkids #kidsanimation #fyp"
)
_HASHTAGS_FB = (
    "#SEABINI #KidsShow #Preschool #OceanForKids #ToddlerLearning #KidsCartoon "
    "#ChildrensContent #OceanAdventure #KidsVideos #FamilyFriendly"
)
_HASHTAGS_YT = (
    "#SEABINI #KidsCartoon #OceanForKids #Preschool #ToddlerLearning "
    "#SeaAnimals #KidsShow #Shorts #ChildrensContent #EducationalKids"
)
# Bini's Real Ocean is real nature footage, not a cartoon: tagging it
# #kidscartoon sent it to the wrong audience. The creature's own tag goes
# first (YouTube shows the first 3 description hashtags above the title).
_REAL_TAGS_YT = "#OceanAnimals #AnimalsForKids #KidsLearning #SeaLife #Preschool #ToddlerLearning #NatureForKids #Shorts #SEABINI"
_REAL_TAGS_TT = "#oceananimals #animalsforkids #kidslearning #sealife"
_REAL_TAGS_FB = "#OceanAnimals #AnimalsForKids #KidsLearning #Preschool #SeaLife #FamilyFriendly"
SITE_URL = "https://oceanfarm1992-design.github.io/ai-drama-showcase/"
_DISC_TAGS_TT = "#oceanforkids #sealife #animalsforkids #kidslearning #natureforkids"
_DISC_TAGS_FB = "#OceanForKids #SeaLife #AnimalsForKids #KidsLearning #Preschool #FamilyFriendly #NatureForKids"
_HABITAT_TAGS = {
    "Sandy Beach": "#Beach", "Rocky Shore": "#RockPools", "Mangrove Forest": "#Mangroves",
    "Lagoon": "#Lagoon", "Seagrass Meadow": "#Seagrass", "Coral Reef": "#CoralReef",
    "Shipwreck Reef": "#Shipwreck", "Seaweed Forest": "#Seaweed", "Open Ocean": "#OpenOcean",
    "Icy Polar Seas": "#PolarAnimals", "Glowing Night Sea": "#Bioluminescence",
}


def _creature_tag(creature: str) -> str:
    """'sea urchin' -> '#SeaUrchin' (empty if unknown)."""
    words = [w for w in creature.replace("-", " ").split() if w.isalpha()]
    return "#" + "".join(w.capitalize() for w in words) if words else ""


def _creature_name(creature: str) -> str:
    return " ".join(w.capitalize() for w in creature.split())


def _creature_plural(creature: str) -> str:
    """'penguin' -> 'Penguins' (what parents type: "penguins for kids");
    fish/shrimp/coral and already-plural names stay as they are."""
    name = _creature_name(creature)
    if not name or name.endswith(("fish", "Fish", "s", "Shrimp", "Coral")):
        return name
    return name + "s"

def _discovery_captions(title: str, search_hook: str, chapters: list, minutes: int) -> dict:
    """Long-form journey: lead with the search question, then the habitats
    as timestamped chapters (Facebook turns them into clickable links)."""
    hook_line = f"{search_hook[0].upper()}{search_hook[1:]}?\n\n" if search_hook else ""
    places = [name for _, name in chapters if name not in ("Welcome", "Goodbye")]
    tags = " ".join(_HABITAT_TAGS[p] for p in places if p in _HABITAT_TAGS)
    low = [p.lower() for p in places]
    journey = (", ".join(low[:-1]) + f" and {low[-1]}") if len(low) > 1 else "".join(low)
    stamps = "\n".join(f"{t} {name}" for t, name in chapters)
    tt_text = (f"{hook_line}🌊 {title}! A {minutes}-minute real ocean journey with Bini and friends "
               f"through the {journey}. 🐢🦀🐠\n\n{stamps}\n\n{tags.lower()} {_DISC_TAGS_TT}")
    fb_text = (f"🌊 {hook_line}{title}\n\n"
               f"Join Bini and friends on a {minutes}-minute journey through real ocean homes: "
               f"the {journey}! Real nature footage, gentle narration and simple facts, "
               "made for little explorers ages 2–6. 🐢🦀🐠\n\n"
               f"Chapters:\n{stamps}\n\n"
               "A brand-new ocean journey every week! 💙\n\n"
               f"▶️ All episodes: {SITE_URL}\n\n{tags} {_DISC_TAGS_FB}")
    return {"youtube": fb_text, "tiktok": tt_text, "facebook": fb_text}


def _captions(title: str, objective: str, series_title: str = "SEABINI", narrator: str = "Bini",
              search_hook: str = "", creature: str = "") -> dict:
    if series_title == "Bini's Real Ocean":
        # Lead with the question exactly as a curious viewer would type it
        # into search — this is the actual SEO lever (matching real search
        # phrasing), not the hashtag list, which TikTok/YouTube weight far
        # less than caption + spoken-audio text for surfacing search results.
        # "<animal> for kids" is the other phrase parents search for.
        hook_line = f"{search_hook[0].upper()}{search_hook[1:]}?\n\n" if search_hook else ""
        tag = _creature_tag(creature)
        name = _creature_name(creature)
        about = (f"A real {creature} video for kids: {narrator} shows little ones how the {creature} "
                 "lives, moves and eats, using real underwater footage." if creature else "")
        yt_desc = (
            f"🌊 {hook_line}{title} | Bini's Real Ocean, real sea animals for kids!\n\n"
            f"{objective}\n\n"
            + (f"{about}\n\n" if about else "")
            + f"Bini's Real Ocean is a preschool-friendly series (ages 2–6) where {narrator} "
            "narrates real underwater footage of ocean animals. 🐠🦀🪼\n\n"
            "✅ Safe for kids  ✅ Real nature footage  ✅ New episode every day\n\n"
            f"▶️ Watch every episode: {SITE_URL}\n\n"
            + f"{tag} {_REAL_TAGS_YT}".strip()
        )
        tt_text = (
            f"{hook_line}🌊 {title}! Join {narrator} for a real look at "
            f"{f'the {creature}' if creature else 'ocean life'}! "
            f"{objective} 🐠 New episode daily! " + f"{tag.lower()} {_REAL_TAGS_TT}".strip()
        )
        fb_text = (
            f"🌊 {hook_line}New Bini's Real Ocean episode: {title}!\n\n"
            f"{objective} 🐠\n\n"
            f"Real ocean footage with {narrator} as your guide, every day! "
            "Perfect for little ones ages 2–6. Share with a parent today! 💙\n\n"
            f"▶️ All episodes: {SITE_URL}\n\n"
            + f"{tag} {_REAL_TAGS_FB}".strip()
        )
        return {"youtube": yt_desc, "tiktok": tt_text, "facebook": fb_text}

    yt_desc = (
        f"🌊 {title} | SEABINI — Adventures Beneath the Blue!\n\n"
        f"Today Bini and friends discover: {objective}\n\n"
        "SEABINI is a preschool ocean cartoon for ages 2–6. Every episode has "
        "an adventure story + a fun sing-and-dance song with Bini! 🐠🦑🐢\n\n"
        "✅ Safe for kids  ✅ AI-assisted  ✅ New episode every day\n\n"
        + _HASHTAGS_YT
    )
    tt_text = (
        f"🌊 {title}! Join Bini the seahorse on an ocean adventure! "
        f"{objective} 🐠 New episode daily! " + _HASHTAGS_TT
    )
    fb_text = (
        f"🌊 New SEABINI episode: {title}!\n\n"
        f"Bini and friends learn: {objective} 🐠\n\n"
        "A brand-new ocean adventure + sing-along song every day! "
        "Perfect for little ones ages 2–6. Share with a parent today! 💙\n\n"
        + _HASHTAGS_FB
    )
    return {"youtube": yt_desc, "tiktok": tt_text, "facebook": fb_text}


def _video_titles(title: str, series_title: str, search_hook: str, creature: str = "") -> tuple:
    """(youtube_title, tiktok_title). The YouTube title is its strongest
    search signal, so real-footage episodes lead with the search question,
    then "<animal> for kids". No #Shorts: YouTube detects Shorts by format,
    and the tag only spent characters."""
    if series_title == DISCOVERY:
        hook = f"{search_hook[0].upper()}{search_hook[1:]}?" if search_hook else ""
        full = f"{hook} 🌊 {title}" if hook else title
        return full[:100], full[:90]
    if series_title == "Bini's Real Ocean":
        hook = f"{search_hook[0].upper()}{search_hook[1:]}?" if search_hook else title
        topic = f"Real {_creature_plural(creature)} for Kids" if creature else "Real Sea Animals for Kids"
        yt = f"{hook} 🌊 {topic} | Bini's Real Ocean"
        if len(yt) > 100:
            yt = f"{hook} 🌊 {topic}"
        return yt[:100], f"{hook} 🌊 {title}"[:90]
    return (f"SEABINI | {title} 🌊 | Kids Ocean Cartoon | #Shorts"[:100],
            f"SEABINI | {title} 🌊"[:90])


def _buffer_post(channel_id: str, service: str, video_url: str,
                 captions: dict, title: str, series_title: str = SERIES_TITLE,
                 search_hook: str = "", creature: str = "", facebook_type: str = "reel") -> tuple:
    """(posted, error message)."""
    text = captions.get(service, captions["youtube"])
    assets = [{"video": {"url": video_url, "metadata": {"title": title}}}]
    yt_title, tt_title = _video_titles(title, series_title, search_hook, creature)
    metadata = {}
    if service == "youtube":
        metadata["youtube"] = {
            "title": yt_title,
            "privacy": "public",
            # Real nature footage is Education (27); the cartoon is Film & Animation (1)
            "categoryId": "27" if series_title in ("Bini's Real Ocean", DISCOVERY) else "1",
            "madeForKids": True,
            "isAiGenerated": True,
            "embeddable": True,
            "notifySubscribers": True,
        }
    if service == "tiktok":
        metadata["tiktok"] = {
            "title": tt_title,
            "isAiGenerated": True,
        }
    if service == "facebook":
        # Reels for shorts; long-form goes up as a regular Facebook video post.
        metadata["facebook"] = {"type": facebook_type}
    post_input = {
        "channelId": channel_id,
        "text": text,
        "assets": assets,
        "mode": "shareNow",
        "schedulingType": "automatic",
        "needsApproval": False,
    }
    if metadata:
        post_input["metadata"] = metadata

    resp = _buffer_gql(_CREATE_POST, {"input": post_input})
    if not resp:
        return False, "no response"
    errs = resp.get("errors")
    data = (resp.get("data") or {}).get("createPost") or {}
    typename = data.get("__typename", "")
    post = data.get("post")
    err_msg = data.get("message")
    if errs:
        print(f"[Buffer:{service}] GraphQL error: {errs}")
    elif typename == "PostActionSuccess" and post:
        print(f"[Buffer:{service}] Queued → id={post['id']} status={post['status']}")
        # Buffer can accept the request but report the post itself as failed
        # (seen for TikTok); that is a failed post, not a queued one.
        ok = str(post.get("status", "")).lower() != "error"
        return ok, "" if ok else "post status error"
    elif err_msg:
        print(f"[Buffer:{service}] {typename}: {err_msg}")
    else:
        print(f"[Buffer:{service}] Response: {resp}")
    return False, str(err_msg or errs or resp)


def _video_duration(video_path: str):
    """Seconds, or None if ffprobe isn't available / can't read the file."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", video_path],
                             capture_output=True, text=True, check=True).stdout
        return float(out.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


def _post_all_channels(video_url: str, title: str, objective: str, series_title: str = "SEABINI",
                       narrator: str = "Bini", search_hook: str = "", duration: float = None,
                       creature: str = "", platforms: list = None, facebook_type: str = "reel",
                       chapters: list = None, service_urls: dict = None) -> list:
    """Posts to every chosen channel even if one fails; returns the failed
    services. A Reel the video is too long for is skipped, not failed."""
    if series_title == DISCOVERY:
        caps = _discovery_captions(title, search_hook, chapters or [], round((duration or 0) / 60))
    else:
        caps = _captions(title, objective, series_title, narrator, search_hook, creature)
    todo = {}
    for service, channel_id in _BUFFER_CHANNELS.items():
        if platforms and service not in platforms:
            continue
        if service == "facebook" and facebook_type == "reel" and duration and duration > _MAX_REEL_SEC:
            print(f"::warning::[Buffer:facebook] skipped: video is {duration:.1f}s, Reels limit is {_MAX_REEL_SEC:.0f}s")
            continue
        todo[service] = channel_id
    failed = []
    for attempt in range(2):
        retry = {}
        for service, channel_id in todo.items():
            url = (service_urls or {}).get(service, video_url)
            ok, err = _buffer_post(channel_id, service, url, caps, title, series_title,
                                   search_hook, creature, facebook_type)
            if ok:
                continue
            if attempt == 0 and _UNREADABLE in err:
                retry[service] = channel_id
            else:
                failed.append(service)
        if not retry:
            break
        print(f"[Buffer] video not readable yet for {', '.join(retry)}; retrying in {_UNREADABLE_RETRY_SEC}s")
        time.sleep(_UNREADABLE_RETRY_SEC)
        todo = retry
    return failed


def _retryable(exc) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or exc.code == 429
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def _with_retry(what, fn):
    """Run fn(), retrying transient GitHub failures (5xx, 429, network)."""
    for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
        try:
            return fn()
        except Exception as exc:
            if delay is None or not _retryable(exc):
                raise
            print(f"[{what}] transient error ({exc}), retry {attempt + 1}/{len(_RETRY_DELAYS)} in {delay}s")
            time.sleep(delay)


def _gh_api(method, path, body=None, content_type="application/json"):
    """Call the GitHub REST API with the showcase token."""
    token = _gh_token()
    if not token:
        raise RuntimeError("No GH_TOKEN / SHOWCASE_TOKEN available")
    url = f"https://api.github.com{path}" if path.startswith("/") else path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": content_type,
    })
    try:
        raw = urllib.request.urlopen(req, context=_SSL_CTX, timeout=120).read()
        return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode(errors="replace")
        print(f"[GitHub API] {exc.code} {method} {url}: {err_body[:500]}")
        raise


def _upload_release(video_paths: list, title: str) -> list:
    """Create one release holding every file; returns their download URLs."""
    tag = "seabini-" + datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    token = _gh_token()
    print(f"[Release] Creating {tag} in {REPO} (token len={len(token)})")

    # 1. Create the release. A 5xx can still have created it server-side, so
    # a retry that hits 422 (tag exists) picks up the existing release.
    def create():
        try:
            return _gh_api("POST", f"/repos/{REPO}/releases", {
                "tag_name": tag,
                "name": title,
                "body": "Auto-published by the SEABINI pipeline.",
            })
        except urllib.error.HTTPError as exc:
            if exc.code != 422:
                raise
            return _gh_api("GET", f"/repos/{REPO}/releases/tags/{tag}")
    release = _with_retry("Release", create)
    upload_url = release["upload_url"].split("{")[0]  # strip {?name,label} template
    print(f"[Release] Created: {release['html_url']}")

    # 2. Upload the video asset(s)
    return [_upload_asset(release, upload_url, tag, path, token) for path in video_paths]


def _upload_asset(release, upload_url, tag, video_path, token):
    filename = pathlib.Path(video_path).name
    file_data = pathlib.Path(video_path).read_bytes()
    asset_url = f"{upload_url}?name={filename}"
    req = urllib.request.Request(asset_url, data=file_data, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/octet-stream",
    })
    def upload():
        # A failed attempt can leave a half-uploaded asset that blocks the name.
        for a in _gh_api("GET", f"/repos/{REPO}/releases/{release['id']}/assets"):
            if a["name"] == filename:
                if a.get("state") == "uploaded" and a.get("size") == len(file_data):
                    return a
                _gh_api("DELETE", f"/repos/{REPO}/releases/assets/{a['id']}")
        return json.loads(urllib.request.urlopen(req, context=_SSL_CTX, timeout=600).read())
    try:
        asset = _with_retry("Release", upload)
        print(f"[Release] Uploaded asset: {asset['name']} ({asset['size']} bytes)")
    except urllib.error.HTTPError as exc:
        # Stop here: carrying on would post a dead video link to every platform.
        raise SystemExit(f"[Release] Asset upload failed: {exc.code} {exc.read().decode(errors='replace')[:300]}")
    return f"https://github.com/{REPO}/releases/download/{tag}/{filename}"


def _get_manifest():
    """Fetch manifest.json from ai-drama-showcase via GitHub API. Returns (dict, sha)."""
    token = _gh_token()
    if not token:
        return {"episodes": []}, None
    api_url = f"https://api.github.com/repos/{REPO}/contents/manifest.json"
    req = urllib.request.Request(api_url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
    })
    try:
        resp = json.loads(urllib.request.urlopen(req, context=_SSL_CTX, timeout=15).read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print("[Manifest] No manifest yet, starting fresh.")
            return {"episodes": []}, None
        raise
    content = base64.b64decode(resp["content"]).decode("utf-8")
    return json.loads(content), resp["sha"]


def _put_manifest(manifest_data: dict, title: str, sha=None):
    """Commit updated manifest.json to ai-drama-showcase via GitHub API."""
    token = _gh_token()
    if not token:
        print("[Manifest] No token — skipping manifest update.")
        return
    api_url = f"https://api.github.com/repos/{REPO}/contents/manifest.json"
    content_b64 = base64.b64encode(
        json.dumps(manifest_data, ensure_ascii=False, indent=2).encode()
    ).decode()
    body = {
        "message": f"Publish episode: {title}",
        "content": content_b64,
        "committer": {"name": "seabini-bot", "email": "seabini-bot@users.noreply.github.com"},
    }
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(api_url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
        },
        method="PUT",
    )
    urllib.request.urlopen(req, context=_SSL_CTX, timeout=15)
    print(f"[Manifest] Updated in {REPO}")


def publish(video_path: str, title: str, language: str = "en",
            objective: str = "a fun ocean discovery",
            series_title: str = SERIES_TITLE,
            narrator: str = "Bini",
            search_hook: str = "",
            footage_ids: list = None,
            creature: str = "",
            platforms: list = None,
            facebook_type: str = "reel",
            habitats: list = None,
            chapters: list = None,
            extra_videos: dict = None,
            platform_video: dict = None) -> str:
    extra_videos = extra_videos or {}
    urls = _upload_release([video_path, *extra_videos.values()], title)
    video_url = urls[0]
    cut_urls = {"portrait": video_url, **dict(zip(extra_videos, urls[1:]))}
    service_urls = {svc: cut_urls[cut] for svc, cut in (platform_video or {}).items() if cut in cut_urls}

    entry = {
        "series_title": series_title,
        "title": title,
        "narrator": narrator,
        "creature": creature,
        "language": language,
        "learning_objective": objective,
        "search_hook": search_hook,
        "footage_ids": footage_ids or [],
        "video_url": video_url,
        "published_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    if habitats:
        entry["habitats"] = habitats
    if chapters:
        entry["chapters"] = chapters
    if "landscape" in cut_urls:
        entry["video_url_wide"] = cut_urls["landscape"]
    for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
        # Retry on conflicts (another run updated the manifest meanwhile) and
        # transient errors. A 5xx PUT may still have committed, so skip the
        # insert if this episode is already there.
        try:
            manifest, sha = _get_manifest()
            if any(e.get("video_url") == video_url for e in manifest["episodes"]):
                print("[Manifest] Entry already present")
                break
            # max+1, not count+1: episodes can be removed from the manifest
            n = max((e.get("episode_number", 0) for e in manifest["episodes"]
                     if e.get("series_title") == series_title), default=0) + 1
            manifest["episodes"].insert(0, dict(entry, episode_number=n, series_length=n))
            _put_manifest(manifest, title, sha)
            break
        except Exception as exc:
            conflict = isinstance(exc, urllib.error.HTTPError) and exc.code in (409, 422)
            if delay is None or not (conflict or _retryable(exc)):
                raise
            print(f"[Manifest] {exc}, retrying in {delay}s...")
            time.sleep(delay)

    # The episode is live (release + manifest) at this point, so a social
    # posting problem is a warning, not a failed run — unless every channel
    # failed, which points at the Buffer key/account rather than one post.
    failed = _post_all_channels(video_url, title, objective, series_title, narrator, search_hook,
                                _video_duration(video_path), creature, platforms, facebook_type, chapters,
                                service_urls)
    if failed and len(failed) == len(platforms or _BUFFER_CHANNELS):
        raise SystemExit(f"[Buffer] Posting failed for every channel: {', '.join(failed)}")
    for service in failed:
        print(f"::warning::[Buffer:{service}] posting failed; episode is published, post manually if needed")
    return video_url


if __name__ == "__main__":
    video = sys.argv[1]
    if len(sys.argv) > 3 and sys.argv[2] == "--meta":
        m = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
        print("PUBLISHED:", publish(video, m["title"], "en", m.get("learning_objective", ""),
                                    m.get("series_title", SERIES_TITLE), m.get("narrator", "Bini"),
                                    m.get("search_hook", ""), m.get("footage_ids", []),
                                    m.get("creature", ""), m.get("platforms"),
                                    m.get("facebook_type", "reel"), m.get("habitats"),
                                    m.get("chapters"), m.get("extra_videos"), m.get("platform_video")))
    else:  # legacy positional form, still used by the (disabled) SEABINI workflow
        title = sys.argv[2] if len(sys.argv) > 2 else "SEABINI Short"
        language = sys.argv[3] if len(sys.argv) > 3 else "en"
        objective = sys.argv[4] if len(sys.argv) > 4 else "a fun ocean discovery"
        series_title = sys.argv[5] if len(sys.argv) > 5 else SERIES_TITLE
        print("PUBLISHED:", publish(video, title, language, objective, series_title))
