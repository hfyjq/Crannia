"""
Entertainment skill — music commands.

此 skill 仍在搭建中，剩余功能支持请等待更新。

Handles user-facing entertainment requests like "play a song" / "来首周杰伦的歌".

Hard constraints:
  1. No media download — information first, URLs only.
  2. Returns structured, LLM-friendly dicts.
"""

from __future__ import annotations

from core.security import SecurityGuard

_security: SecurityGuard | None = None


def init(security: SecurityGuard) -> None:
    """装载入口 —— 与其它 skill 同一个接口（`bus/loaders/skill_loader.py` 调它）。"""
    global _security
    _security = security


# Tool: search_song

def search_song(keyword: str, limit: int = 5) -> dict:
    """
    Search NetEase Cloud Music for songs.

    Uses the public search API (no auth required for basic search).
    Returns song ID, name, artist, album for each result.

    Args:
        keyword: Search query (song name, artist name).
        limit: Max results (default 5).

    Returns:
        dict with songs list.
    """
    if not keyword.strip():
        return {"success": False, "error": "请提供搜索关键词"}

    import asyncio
    import concurrent.futures

    async def _search():
        import aiohttp
        url = "https://music.163.com/api/search/get"
        params = {"s": keyword, "type": 1, "limit": min(limit, 20), "offset": 0}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://music.163.com",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status >= 400:
                        return {"success": False, "error": f"搜索 API 返回 {resp.status}"}
                    # NetEase API often returns text/plain instead of application/json
                    data = await resp.json(content_type=None)
        except Exception as e:
            return {"success": False, "error": f"搜索请求失败: {type(e).__name__}: {e}"}

        songs = []
        for item in data.get("result", {}).get("songs", [])[:limit]:
            artists = ", ".join(a.get("name", "") for a in item.get("artists", []))
            songs.append({
                "song_id": str(item.get("id", "")),
                "name": item.get("name", ""),
                "artists": artists,
                "album": item.get("album", {}).get("name", ""),
                "url": f"https://music.163.com/#/song?id={item.get('id', '')}",
            })

        return {
            "success": True,
            "keyword": keyword,
            "count": len(songs),
            "songs": songs,
        }

    # Safe async runner
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_search())
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _search()).result()
