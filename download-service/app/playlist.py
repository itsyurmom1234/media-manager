from yt_dlp import YoutubeDL


def probe_playlist(url: str):
    """Peeks at a URL without downloading anything.

    Returns None if it's a single video.
    Returns (title, items) if it's a playlist, where items is a list of
    {"url": str | None, "title": str} in playlist order. A None url means
    that entry is unavailable (private/deleted) - the caller is expected
    to still record its slot so numbering stays consistent.
    """
    opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "skip_download": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info or info.get("_type") != "playlist":
        return None

    entries = info.get("entries") or []
    if len(entries) <= 1:
        return None

    items = []
    for entry in entries:
        if not entry:
            items.append({"url": None, "title": "Unavailable video"})
            continue

        entry_url = entry.get("url")
        if not entry_url or not str(entry_url).startswith("http"):
            video_id = entry.get("id")
            entry_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else None

        items.append({"url": entry_url, "title": entry.get("title") or "Untitled video"})

    title = info.get("title") or "Playlist"
    return title, items
