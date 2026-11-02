# -*- coding: utf-8 -*-
"""预批准域名表 —— 命中就不问。

## 它补的是哪一格

`core/egress_gate.py::is_granted` 本来只有两半：

  ① `wm.has_target_grant("egress", host)` —— 会话级 / 一次性 / 用户在设置页加的持久白名单
  ② `core/allowlist.py::matches_any`      —— 通配（`*.example.com`）

结果是：每一个从没见过的域名都要问一次。实际中自动模式读一份政府周报，
`web_fetch` 每次都弹窗。

## 判据

这张表只回答一个问题：哪些出网站点，用户大概率不需要被问那一次？

「用户给过的」不算本表的活 —— 那一半原本在
`core/egress_gate.py::host_named_by_user`（用户本轮原话里出现过的域名直接放行），
那一半已删：纯字符串匹配分不出授权、讨论、否定、引用，一句
「不要访问 https://example.com」会被读成授权。
现在用户贴的链接和任何新域名一样问一次，点过同意之后本会话内不再问。

所以本表管的是另一半：用户还没开口，但这一类站本来就该信。

为什么可以预置：我们服务的是"帮人查资料、写东西、跑数据"的场景，
而这类场景里用户贴过来的链接高度集中在几类官方站 ——
语言与框架的文档、政策与统计的发布口、学术文献、厂商的开发者平台。
把这几类预先信了，省掉的是大量「你确定要访问 docs.python.org 吗」这种问；
不预置的话，每一次都要问一遍，而答案永远是"确定"。

反过来，机制本身仍然是"交给用户"那一支在兜底：设置页的持久白名单、
会话级授权、审批弹窗里的「信任此站点」，三处都能加。本表只是起点，不是上限。

## 三条不许忘的

1. `www.` 刻意不剥。那张起点表里 `docs.python.org` / `learn.microsoft.com`
   都没有 `www.`，而它是精确匹配（`Set.has`）。
表里该写 `www.` 的就逐条写。副作用：写 `stats.gov.cn` 不会命中 `www.stats.gov.cn`。
   这条只管本表；"用户点名过的域名"那一半两侧都认（那边是我们的判据）。
2. 不在表里 ≠ 不许访问 —— 它只是"要多问一次"。`deny` 档与地板一个字节不受影响
   （`core/egress_gate.py` 的模块 docstring：`deny` 先于 `ask`）。
3. 加一条要有理由 —— 这张表是「我们替用户预先信的」。
   加站点时照下面每组那样写清它为什么够格。没有理由的条目 =
   一次没人复核过的授权扩大。

## 它会长

第一组（编程/文档）是起点、不是终局；第二组（官方站）是新开的、还会长。
"""

from __future__ import annotations

__all__ = ["PREAPPROVED_HOSTS", "PATH_SCOPED", "is_preapproved", "host_of_entry"]

#: 表 1｜编程：语言 / 框架 / 数据库 / 工具链
#:
#: 负责：写代码时用户会贴的官方文档 —— 语言规范、框架文档、数据库手册、
#:       云与运维的官方文档、构建与测试工具的官网。
#: 不收：个人博客、教程站、问答站（那些不是"官方口径"，内容随时变）。
#:
#: 排序按实用度；顺序不参与判定（`_split()` 把它灌进 `set`）。
_PROGRAMMING: tuple[str, ...] = (
    # 语言与 Web 平台
    "developer.mozilla.org",
    "docs.python.org",
    "learn.microsoft.com",
    "doc.rust-lang.org",
    "go.dev",
    "pkg.go.dev",
    "www.typescriptlang.org",
    "en.cppreference.com",
    "docs.oracle.com",
    "www.php.net",
    "ruby-doc.org",
    "docs.swift.org",
    # Web / 前端
    "react.dev",
    "nodejs.org",
    "nextjs.org",
    "vuejs.org",
    "tailwindcss.com",
    "expressjs.com",
    "angular.io",
    "jestjs.io",
    "webpack.js.org",
    "d3js.org",
    "threejs.org",
    "reactrouter.com",
    "bun.sh",
    # 数据库与 API 层
    "www.postgresql.org",
    "redis.io",
    "www.mongodb.com",
    "dev.mysql.com",
    "www.sqlite.org",
    "prisma.io",
    "graphql.org",
    # Python 生态
    "pandas.pydata.org",
    "numpy.org",
    "fastapi.tiangolo.com",
    "docs.djangoproject.com",
    "flask.palletsprojects.com",
    "pytorch.org",
    "www.tensorflow.org",
    "scikit-learn.org",
    "matplotlib.org",
    "requests.readthedocs.io",
    "jupyter.org",
    # 版本控制与 Web 服务器
    "git-scm.com",
    "nginx.org",
    "httpd.apache.org",
    # 云 / 运维 / 测试
    "www.docker.com",
    "kubernetes.io",
    "docs.aws.amazon.com",
    "cloud.google.com",
    "www.terraform.io",
    "docs.netlify.com",
    "devcenter.heroku.com",
    "selenium.dev",
    "cypress.io",
    "www.ansible.com",
    # 其他语言的后端与构建
    "docs.spring.io",
    "dotnet.microsoft.com",
    "nuget.org",
    "asp.net",
    "gradle.org",
    "maven.apache.org",
    "laravel.com",
    "wordpress.org",
    "reactnative.dev",
    "docs.flutter.dev",
    # 机器学习与数据平台
    "huggingface.co",
    "spark.apache.org",
    "keras.io",
    "www.kaggle.com",
)

#: 表 2｜学术文献与预印本
#:
#: 负责：用户直接把论文链接贴进来问"这篇讲了什么 / 这个方法可靠吗"。
#: 不收：搜索结果页与聚合器 —— 内容是索引不是文献，而且随时变。
_ACADEMIC: tuple[str, ...] = (
    "arxiv.org",
    "scholar.google.com",
    "ssrn.com",
    "chinaxiv.org",
    "chemrxiv.org",
    "techrxiv.org",
    "preprints.org",
    "pubmed.ncbi.nlm.nih.gov",
)

#: 表 3｜百科与知识
#:
#: 负责：用户贴一个词条问"这是什么"。
#: 不收：内容农场、需要登录的百科镜像。
_REFERENCE: tuple[str, ...] = (
    "zh.wikipedia.org",
    "en.wikipedia.org",
    "baike.baidu.com",
)

#: 表 4｜国内政务与统计
#:
#: 负责：政策原文、统计口径、办事指南、学历学籍这类"以官方发布为准"的内容。
#: 不收：需要登录或验证码才看得到的站 —— 放开审批只是不弹窗，
#:       抓回来还是登录页，等于让模型以为"这个我能读"。
_GOV_CN: tuple[str, ...] = (
    "www.gov.cn",
    "www.stats.gov.cn",
    "www.moa.gov.cn",
    "www.mofcom.gov.cn",
    "www.customs.gov.cn",
    "mof.gov.cn",
    "mohrss.gov.cn",
    "nhc.gov.cn",
    "moe.gov.cn",
    "ndrc.gov.cn",
    "pbc.gov.cn",
    "csrc.gov.cn",
    "chinatax.gov.cn",
    "miit.gov.cn",
    "flk.npc.gov.cn",
    "ncpssd.org",
    "chsi.com.cn",
    "yz.chsi.com.cn",
    "nmpa.gov.cn",
)

#: 表 5｜国际组织与外国政务
#:
#: 负责：国外政策、法律、统计与国际组织的官方页面。
#: 挑的原则是"一个域覆盖尽量多的内容" —— 英国与欧盟把政务集中在一个域里，
#: 一条顶一片；美国是各机构独立域名，只能逐条列。
_GOV_INTL: tuple[str, ...] = (
    "gov.uk",
    "europa.eu",
    "worldbank.org",
    "imf.org",
    "oecd.org",
    "un.org",
    "who.int",
    "census.gov",
    "bls.gov",
    "bea.gov",
    "federalreserve.gov",
    "eur-lex.europa.eu",
    "law.cornell.edu",
    "sec.gov",
)

#: 表 6｜平台开发
#:
#: 负责：做具体平台上的东西时用户会贴的官方站 —— 移动端、游戏引擎、
#:       各家厂商的开发者平台与 AI 模型主页、以及两个开放协议。
#: 不收：只有壳没有内容的站（正文全靠 JS 渲染的），理由同表 4。
_PLATFORM: tuple[str, ...] = (
    # 移动端
    "developer.apple.com",
    "developer.android.com",
    # 游戏引擎
    "docs.unity.com",
    "docs.unrealengine.com",
    "godotengine.org",
    "renpy.org",
    # 厂商开发者平台
    "developer.huawei.com",
    # 办公平台的开放平台 —— 只收开发者文档，不收办公内容
    # （文档 / 表格 / 会话全要登录，抓回来是登录页，理由同表 4）
    "open.feishu.cn",
    "open.dingtalk.com",
    "developer.work.weixin.qq.com",
    "developers.notion.com",
    "api.slack.com",
    "developers.zoom.us",
    # AI 厂商主页
    "openai.com",
    "anthropic.com",
    "deepseek.com",
    "moonshot.cn",
    "bigmodel.cn",
    "minimaxi.com",
    "doubao.com",
    "mistral.ai",
    "cohere.com",
    "x.ai",
    # 协议与规范的官方站
    "modelcontextprotocol.io",
    "agentskills.io",
)

#: 表 7｜艺术与素材
#:
#: 负责：用户贴图片、字体、图标这类素材站问"这张能不能用 / 授权是什么"。
#: 不收：需要登录才看得到素材的（阿里图标库）、强反爬的照片墙
#:       （Pinterest）—— 前者抓回来是登录页，后者抓回来是骨架。
_ARTS: tuple[str, ...] = (
    "unsplash.com",
    "pexels.com",
    "pixabay.com",
    "fonts.google.com",
)

#: 表 8｜国内财经数据
#:
#: 负责：用户贴"帮我看看这个板块 / 这家公司的财报"时指向的公开数据源。
#: 只收能免登录看到数据的；要付费或要登录的（雪球、Wind、天眼查）都不收。
_FINANCE_CN: tuple[str, ...] = (
    "eastmoney.com",
)

#: 八张表拼成一张查询集 —— 对外只有一个名字，分表只是让每一份能说清自己管什么。
PREAPPROVED_HOSTS: tuple[str, ...] = (
    _PROGRAMMING + _ACADEMIC + _REFERENCE + _GOV_CN + _GOV_INTL + _PLATFORM
    + _ARTS + _FINANCE_CN
)

#: 带路径的条目 —— 做法是 `host + 路径前缀`。
#: 判据：必须在路径段边界上匹配 ——
#: `/docs` 不许匹配 `/docs-evil/malware`，只认精确相等或后面跟 `/`。
#: 为什么需要这一格：把整个 host 预批准太宽（谁都能往上面放东西），
#: 只批其中一个路径前缀，才只放开"官方文档那一块"。
PATH_SCOPED: tuple[str, ...] = (
    "vercel.com/docs",
    # OpenHarmony 的代码仓在 Gitee 上；只放这个组织，不放开整个 Gitee。
    "gitee.com/openharmony",
)


def host_of_entry(entry: str) -> str:
    """条目里的 host 部分（`vercel.com/docs` -> `vercel.com`）。"""
    e = str(entry or "").strip().lower()
    return e.split("/", 1)[0]


def is_preapproved(host: str, path: str = "/") -> bool:
    """这个 host（可带路径）在不在预批准表里。fail-closed：拿不准就 False（照问）。

    `path` 只有 `PATH_SCOPED` 那几条会用到；其余条目只看 host。
    不做子域匹配、不剥 `www.` —— 见模块 docstring 第 1 条。
    """
    h = str(host or "").strip().lower()
    if not h:
        return False
    if h in _HOST_ONLY:
        return True
    prefixes = _PATH_PREFIXES.get(h)
    if not prefixes:
        return False
    p = str(path or "/").strip() or "/"
    if not p.startswith("/"):
        p = "/" + p
    for pre in prefixes:
        #: 路径段边界：精确相等，或后面紧跟 `/`。
        if p == pre or p.startswith(pre + "/"):
            return True
    return False


# 模块加载时分一次组（查表 O(1)）
def _split():
    hosts: set[str] = set()
    paths: dict[str, list[str]] = {}
    for entry in PREAPPROVED_HOSTS:
        hosts.add(entry)
    for entry in PATH_SCOPED:
        host, _, rest = entry.partition("/")
        paths.setdefault(host, []).append("/" + rest)
    return frozenset(hosts), {k: tuple(v) for k, v in paths.items()}


_HOST_ONLY, _PATH_PREFIXES = _split()
