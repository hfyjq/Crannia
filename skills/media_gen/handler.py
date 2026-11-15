"""生图 skill —— 厂商直连（OpenAI 兼容 `/images/generations`）。

## 为什么是原生 skill

对话模型自己不会画图。这一层把"让另一个服务画"变成一个工具调用：
描述进去、图出来，而且直接进聊天区（走 `core/artifacts` 那条呈现链，
不是给用户一段 URL 让他自己点）。

## 配置（`data/media_gen_credentials.json`）

独立文件，不进 `settings.json`（后者会被设置导入导出带走）::

    {
      "default": "ark",
      "entries": {
        "ark": {
          "base_url": "https://ark.cn-beijing.volces.com/api/v3",
          "model": "doubao-seedream-4-0-20260415",
          "api_key": "…"
        }
      }
    }

- `base_url` 填到那家的 API 根（不一定是 `/v1`：火山是 `/api/v3`）；
- 没配，工具照常注册，调用时回一句"还没配置"（不是崩、也不是假装成功）；
- 密钥一个字都不回显（回执里只出现条目名与模型名）。

## 出网

两段，都过 `core/security.check_url_egress`（地板）：

1. 请求厂商 API —— 地址由用户配置给出，必须过闸；
2. 下载返回的图片 URL —— 每一跳都过闸，且只同站跟随（跨站交回模型，
   与 `§3.10` 同一套判据）。厂商给的 URL 有时效，拿到就下，不存着。

## 拿不到的东西

- 花费：直连厂商的响应里没有 cost 字段，不做收费通知；
- 多张：一次一张（各家的 `n` / `sequential_image_generation` 支持不一，
  一套参数打所有模型会撞 400 —— 这是真调时踩过的坑）。
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

__all__ = ["generate_image"]

_TIMEOUT_S = 120.0
_MAX_HOPS = 3

#: 图片字节 → 扩展名（按魔数，不信上游说的 mime）。
_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def init(security_guard=None) -> None:
    """装载钩子。不读配置、不发请求 —— 那是每次调用时的事。"""
    return None


# 配置

def _cred_path() -> Path:
    """配置文件在哪 —— 以 manifest 的 `settings.storage` 为准。

    配置文件只有一处口径：界面（`/api/skills/<name>/config`）与本 handler
      读写的必须是同一个文件，否则"界面上改了、模型那边没变"。
    """
    try:
        from core import skill_settings as _ss
        p = _ss.storage_for("media_gen")
        if p is not None:
            return p
    except Exception:                                     # noqa: BLE001
        pass
    return Path("data") / "media_gen_credentials.json"


def _load_entries() -> tuple[dict, str]:
    """`(entries, default)`；读不到/坏文件，空表（调用方给一句实话）。"""
    p = _cred_path()
    if not p.is_file():
        return {}, ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:                              # noqa: BLE001
        print("[media_gen] %s 读不出来：%s: %s" % (p, type(exc).__name__, exc))
        return {}, ""
    if not isinstance(data, dict):
        return {}, ""
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}, ""
    clean = {str(k): v for k, v in entries.items() if isinstance(v, dict)}
    return clean, str(data.get("default") or "")


def _pick_entry(entries: dict, default: str, wanted: str) -> tuple[str, dict] | tuple[str, None]:
    if not entries:
        return "", None
    name = str(wanted or "").strip() or default
    if name and name in entries:
        return name, entries[name]
    if len(entries) == 1:
        only = next(iter(entries))
        return only, entries[only]
    return "", None


def _no_config() -> dict:
    return {
        "success": False,
        "error": "还没有配置生图。",
        "hint": ("在 %s 里写一条（base_url / model / api_key），"
                 "或者等设置页的「生图与视频」卡片建一条。" % _cred_path()),
    }


# HTTP

def _client(*, follow_redirects: bool = False):
    """按当前设置造一个 httpx 客户端（代理跟着设置页那一档走）。"""
    from core import net_proxy as _np
    import httpx
    return _np.httpx_client(httpx.Client, follow_redirects=follow_redirects,
                            timeout=_TIMEOUT_S)


def _api_url(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    return base + "/images/generations"


def _same_site(a: str, b: str) -> bool:
    """同站：scheme + 端口 + 主机（忽略 `www.` 前缀，不吸子域）。"""
    try:
        pa, pb = urlsplit(a), urlsplit(b)
    except ValueError:
        return False
    def norm(p):
        host = (p.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        port = p.port or (443 if (p.scheme or "").lower() == "https" else 80)
        return (p.scheme or "").lower(), port, host
    return norm(pa) == norm(pb)


def _download_image(url: str, dest: Path) -> str:
    """把厂商给的 URL 立刻下到文件。返回错误串；成功，空串。

    每一跳都过 `check_url_egress`（地板），只同站跟随 ——
      跨站跳转不跟，交回模型/用户重来（与 `§3.10` 逐字同一条判据）。
    """
    from core.security import UrlEgressError, check_url_egress

    cur = url
    for _hop in range(_MAX_HOPS + 1):
        try:
            cur = check_url_egress(cur)
        except UrlEgressError as exc:
            return "图片地址没通过出网闸：%s" % exc
        try:
            with _client() as c:
                with c.stream("GET", cur) as r:
                    if 300 <= r.status_code < 400:
                        nxt = r.headers.get("location") or ""
                        if not nxt or not _same_site(cur, nxt):
                            return ("图片地址跨站跳转了（未跟随）。"
                                    "让模型重新生成一次，或把它下载到本机后再处理。")
                        cur = nxt
                        continue
                    if r.status_code >= 400:
                        return "下载图片失败：HTTP %d" % r.status_code
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    with tmp.open("wb") as f:
                        for chunk in r.iter_bytes():
                            f.write(chunk)
                    tmp.replace(dest)
                    return ""
        except Exception as exc:                          # noqa: BLE001
            return "下载图片失败（%s）。如果是网络问题：设置页的「出网代理」要选对。" % type(exc).__name__
    return "图片地址跳转次数过多（>%d）。" % _MAX_HOPS


def _ext_for(data: bytes) -> str:
    """按魔数给扩展名（上游说的 mime 不是证据）。"""
    try:
        from core import artifacts as _art
        mime = _art.sniff(data) or ""
    except Exception:                                     # noqa: BLE001
        mime = ""
    return _EXT_BY_MIME.get(mime, ".png")


def _out_dir() -> Path:
    """落点：只有一处口径（`core/workspace.primary_output_dir`）。"""
    from core.workspace import primary_output_dir
    d = Path(str(primary_output_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


# 工具

def generate_image(prompt: str, size: str = "", entry: str = "") -> dict:
    """生成一张图片并保存到工作区（产出的图会直接显示在聊天里）。"""
    text = str(prompt or "").strip()
    if not text:
        return {"success": False, "error": "prompt 不能为空。"}

    entries, default = _load_entries()
    name, cfg = _pick_entry(entries, default, entry)
    if cfg is None:
        if not entries:
            return _no_config()
        return {"success": False,
                "error": "没有叫「%s」的配置。" % (entry or default),
                "hint": "可用的条目：%s" % "、".join(sorted(entries))}

    base_url = str(cfg.get("base_url") or "").strip()
    model = str(cfg.get("model") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip()
    kind = str(cfg.get("type") or "vendor").strip().lower()

    # 本地后端：协议完全不同（SD WebUI 的 `/sdapi/v1/txt2img`），
    #   而且它不过出网闸 —— 因为它按定义就是本机；但"本机"只能是回环地址，
    #   否则一个配置就能把局域网当成生图服务（那是 SSRF，不是本地接入）。
    if kind == "local":
        return _generate_local(name, cfg, text, size)

    if not base_url or not model or not api_key:
        missing = [k for k, v in (("base_url", base_url), ("model", model),
                                  ("api_key", api_key)) if not v]
        return {"success": False,
                "error": "配置「%s」缺字段：%s。" % (name, "、".join(missing)),
                "hint": "补在 %s 里（或等设置页那张卡）。" % _cred_path()}

    body = {"model": model, "prompt": text}
    if str(size or "").strip():
        body["size"] = str(size).strip()
    # 官方示例里 `watermark: true` —— 我们必须显式写 false（不往用户图上盖水印）。
    body["watermark"] = False
    body["response_format"] = "url"

    try:
        from core.security import UrlEgressError, check_url_egress
    except Exception as exc:                              # noqa: BLE001
        return {"success": False, "error": "出网闸不可用（%s）。" % type(exc).__name__}
    try:
        api_url = check_url_egress(_api_url(base_url))
    except UrlEgressError as exc:
        return {"success": False, "error": "生图服务地址没通过出网闸：%s" % exc}
    except Exception as exc:                              # noqa: BLE001
        return {"success": False, "error": "生图服务地址不合法：%s" % exc}

    try:
        with _client() as c:
            r = c.post(api_url, headers={"Authorization": "Bearer " + api_key,
                                         "Content-Type": "application/json"},
                       json=body)
    except Exception as exc:                              # noqa: BLE001
        return {"success": False,
                "error": "连不上生图服务（%s）。" % type(exc).__name__,
                "hint": "如果是网络问题：设置页的「出网代理」要选对（system / manual）。"}

    if r.status_code == 401:
        return {"success": False, "status": 401,
                "error": "密钥无效（401）。",
                "hint": "检查条目「%s」的 api_key 是不是**密钥本身**（有些平台显示的是密钥名）。" % name}
    if r.status_code == 404:
        return {"success": False, "status": 404,
                "error": "模型或地址不对（404）。",
                "hint": "模型「%s」可能不存在或已下架；base_url 要填到 API 根（如火山是 /api/v3）。" % model}
    if r.status_code == 429:
        return {"success": False, "status": 429,
                "error": "生图服务拒绝了调用（429，多半是额度/限流）。",
                "hint": "火山方舟常见原因：「安心体验」把服务暂停了（免费额度为 0）—— 去控制台关掉它，或换一个有额度的模型。"}
    if r.status_code >= 400:
        try:
            detail = r.json()
        except ValueError:
            detail = (r.text or "")[:300]
        return {"success": False, "status": r.status_code,
                "error": "生图服务返回 %d：%s" % (r.status_code, str(detail)[:300])}

    try:
        payload = r.json()
    except ValueError:
        return {"success": False, "error": "生图服务回的不是 JSON（可能被网关改写了）。"}

    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        return {"success": False, "error": "生图服务没回图片（响应里没有 data[]）。"}

    #: 一次只处理第一张 —— 各家对一次多图的支持不一致（见模块 docstring）。
    item = items[0] if isinstance(items[0], dict) else {}
    out_dir = _out_dir()
    dest = out_dir / ("media-gen-%s%s" % (_stamp(), ".png"))

    b64 = item.get("b64_json")
    if isinstance(b64, str) and b64:
        return _write_b64(b64, model, name)
    else:
        url = str(item.get("url") or "").strip()
        if not url:
            return {"success": False, "error": "生图服务既没回 URL 也没回 base64。"}
        ext = ".png"
        low = urlsplit(url).path.lower()
        for e in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            if low.endswith(e):
                ext = ".jpg" if e == ".jpeg" else e
                break
        dest = dest.with_suffix(ext)
        err = _download_image(url, dest)
        if err:
            return {"success": False, "error": err}

    return {
        "success": True,
        "entry": name,
        "model": model,
        "path": str(dest),
        # 交给 `core/artifacts` 的契约（`collect_tool_artifacts`）：路径 + 展示名。
        "artifacts": [{"path": str(dest), "name": dest.name}],
    }


# 本地后端—— Stable Diffusion WebUI / Forge
#
# 协议与厂商那条完全不同：`POST {base}/sdapi/v1/txt2img`，回的是
# base64 列表（`{"images": ["<b64 或 data URL>", …]}`）。
#
# 它不过出网闸：按定义它就是本机 —— 但"本机"只能是回环地址。
#    一个配置能指向 `192.168.x.x` 的话，这不是"本地接入"，是内网请求
#    （而 `check_url_egress` 那一层正是设计来挡这个的，不能因为改了名字就绕开）。
# 没有可验的现场：只做到"结构对 + 报错清楚"+ 机械读者（假响应）。

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _parse_size(size: str, default: int = 1024) -> tuple[int, int]:
    """`"1024x768"` / `"2K"` → `(w, h)`；读不出，默认方形。"""
    s = str(size or "").strip().lower()
    if "x" in s:
        a, _, b = s.partition("x")
        try:
            w, h = int(a), int(b)
            if 64 <= w <= 4096 and 64 <= h <= 4096:
                return w, h
        except ValueError:
            pass
    if s in ("1k", "2k", "4k"):
        n = {"1k": 1024, "2k": 2048, "4k": 4096}[s]
        return n, n
    return default, default


def _write_b64(b64: str, model: str, entry: str) -> dict:
    """一段 base64（可带 `data:` 前缀）→ 工作区文件 + 记录。"""
    s = str(b64 or "").strip()
    if s.startswith("data:"):
        s = s.partition(",")[2]
    try:
        raw = base64.b64decode(s, validate=False)
    except Exception:                                     # noqa: BLE001
        return {"success": False, "error": "返回的 base64 解不开。"}
    if not raw:
        return {"success": False, "error": "返回的图片是空的。"}
    dest = _out_dir() / ("media-gen-%s%s" % (_stamp(), _ext_for(raw)))
    try:
        dest.write_bytes(raw)
    except OSError as exc:                                # noqa: BLE001
        return {"success": False, "error": "写文件失败：%s" % exc}
    return {"success": True, "entry": entry, "model": model, "path": str(dest),
            "artifacts": [{"path": str(dest), "name": dest.name}]}


def _generate_local(name: str, cfg: dict, prompt: str, size: str) -> dict:
    base = str(cfg.get("base_url") or "http://127.0.0.1:7860").strip()
    url = base.rstrip("/") + "/sdapi/v1/txt2img"
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    if host not in _LOOPBACK_HOSTS:
        return {"success": False,
                "error": "本地生图只允许本机地址（127.0.0.1 / localhost），收到「%s」。"
                         % (host or "(空)"),
                "hint": "SD WebUI 默认 http://127.0.0.1:7860；"
                        "如果它跑在另一台机器上，用「厂商直连」那条。"}
    w, h = _parse_size(size)
    body = {"prompt": prompt, "width": w, "height": h, "steps": 20}
    try:
        with _client() as c:
            r = c.post(url, json=body)
    except Exception as exc:                              # noqa: BLE001
        return {"success": False,
                "error": "连不上本地 SD WebUI（%s）。" % type(exc).__name__,
                "hint": "先启动它，并带 --api（默认地址 http://127.0.0.1:7860）。"}
    if r.status_code >= 400:
        try:
            detail = str(r.json())[:200]
        except ValueError:
            detail = (r.text or "")[:200]
        return {"success": False, "status": r.status_code,
                "error": "SD WebUI 返回 %d：%s" % (r.status_code, detail)}
    try:
        payload = r.json()
    except ValueError:
        return {"success": False, "error": "SD WebUI 回的不是 JSON。"}
    imgs = payload.get("images") if isinstance(payload, dict) else None
    if not isinstance(imgs, list) or not imgs:
        return {"success": False,
                "error": "SD WebUI 没回图片（响应里没有 images[]）。"}
    return _write_b64(imgs[0], str(cfg.get("model") or "SD WebUI"), name)
