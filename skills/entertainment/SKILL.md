---
name: entertainment
description: 娱乐指令 Skill — 音乐点歌。Use when the user asks to play music.
metadata:
  kind: skill
  loader: manifest.yaml
  tools: search_song
---

# entertainment

娱乐指令 Skill — 音乐点歌。

> 此 skill 仍在搭建中，剩余功能支持请等待更新。

## 架构

```
用户: "来首周杰伦的歌"
  → entertainment.search_song
    → 网易云音乐公开搜索接口
    → 返回结构化结果给 LLM → 回复用户
```

## 工具

| 工具 | 说明 | 依赖 |
|------|------|------|
| `search_song` | 搜索网易云音乐歌曲 | 无（直接 HTTP API） |

## 待扩展

- `get_trending` — 热榜/热门内容
- `download_audio` — 音频下载（需 yt-dlp）
- `create_playlist` — 歌单管理
