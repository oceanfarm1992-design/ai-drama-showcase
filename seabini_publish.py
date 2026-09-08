"""Publishes a finished SEABINI episode from within the showcase repo itself:
uploads the video as a GitHub Release asset (a public, stable download URL) and
commits the updated manifest.json that the GitHub Pages site reads same-origin.

Because this runs inside the ai-drama-showcase repo's own Actions workflow, it
uses the automatic GITHUB_TOKEN — no personal access token, no external storage.

Env: GH_TOKEN (the workflow passes the automatic GITHUB_TOKEN), GITHUB_REPOSITORY
(auto, "owner/repo"). Run: python seabini_publish.py <video.mp4> "<title>" ["<lang>"]
"""
import os, sys, json, datetime, pathlib, subprocess

SERIES_TITLE = "SEABINI"
REPO = os.environ.get("GITHUB_REPOSITORY", "oceanfarm1992-design/ai-drama-showcase")
MANIFEST = pathlib.Path("manifest.json")


def _run(cmd):
    subprocess.run(cmd, check=True)


def _upload_release(video_path: str, title: str) -> str:
    tag = "seabini-" + datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    _run(["gh", "release", "create", tag, video_path, "--title", title,
          "--notes", "Auto-published by the SEABINI pipeline."])
    filename = pathlib.Path(video_path).name
    return f"https://github.com/{REPO}/releases/download/{tag}/{filename}"


def publish(video_path: str, title: str, language: str = "en") -> str:
    video_url = _upload_release(video_path, title)

    manifest = {"episodes": []}
    if MANIFEST.exists():
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    episode_number = sum(1 for e in manifest["episodes"] if e.get("series_title") == SERIES_TITLE) + 1
    manifest["episodes"].insert(0, {
        "series_title": SERIES_TITLE,
        "title": title,
        "episode_number": episode_number,
        "series_length": episode_number,  # open-ended show, not a fixed-length series
        "language": language,
        "video_url": video_url,
        "published_at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # Commit the updated manifest back to the repo so GitHub Pages serves it.
    # checkout leaves a detached HEAD, so push explicitly to the default branch.
    _run(["git", "config", "user.name", "seabini-bot"])
    _run(["git", "config", "user.email", "seabini-bot@users.noreply.github.com"])
    _run(["git", "add", "manifest.json"])
    _run(["git", "commit", "-m", f"Publish episode: {title}"])
    _run(["git", "push", "origin", "HEAD:master"])
    return video_url


if __name__ == "__main__":
    video = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else "SEABINI Short"
    language = sys.argv[3] if len(sys.argv) > 3 else "en"
    print("PUBLISHED:", publish(video, title, language))
