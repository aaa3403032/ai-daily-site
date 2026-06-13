#!/usr/bin/env python3
"""
AI 每日情报 — 云端生成脚本(阶段3,智谱 GLM 版)
两段式:① Web Search API 按固定搜索词拿真实结果 ② GLM 从结果中筛选写摘要。
来源链接只允许取自搜索结果,从根上避免编造链接。

本地运行:python scripts/generate_digest.py
  Key 来源:环境变量 ZHIPU_API_KEY,或仓库上一级目录的 anthropic_key.txt(文件名历史遗留)
GitHub Actions:Key 来自 Secrets(环境变量 ZHIPU_API_KEY)
任何一步失败都以非零退出码结束,避免提交残缺内容。
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
POSTS_DIR = REPO_ROOT / "posts"
BASE = "https://open.bigmodel.cn/api/paas/v4"
MODEL = os.environ.get("DIGEST_MODEL", "glm-5.1")
# 跑全部引擎再合并(实测 search_pro 的 link 字段为空,已剔除)。
# 不再"够数就停"——单引擎短路是来源单一(全企鹅号)的根因。
ENGINES = ["search_pro_sogou", "search_pro_quark", "search_std"]
TARGET = 15          # 目标候选素材条数
MAX_PER_MEDIA = 2    # 单一来源最多入选几条(轮转抽取时的软上限,凑不够会放宽)
# Tavily:西方搜索源,补一线国际原始源(OpenAI官方/Bloomberg 等),修智谱"中文二三线站+浅链接"短板。
# 有 TAVILY_API_KEY 才启用,没有则自动退回纯智谱,不阻塞流水线。
TAVILY_URL = "https://api.tavily.com/search"
# 走 Tavily 的查询(英文一线源向),与智谱中文结果互补
INTL_QUERIES = [
    "OpenAI Anthropic Google DeepMind latest announcement",
    "AI agent product launch funding round",
    "new large language model release",
]
# 一线源白名单:只让 Tavily 从这些官方/权威媒体取结果,避免上次那种 vendor 博客/保险站噪音。
# 实测(Run#10):不限域名时 Tavily 返回 boltinsurance.com / vlasti.net 等低质源,GLM 合理地全弃之。
TIER1_DOMAINS = [
    "openai.com", "anthropic.com", "deepmind.google", "blog.google",
    "bloomberg.com", "reuters.com", "techcrunch.com", "theverge.com",
    "arstechnica.com", "venturebeat.com", "wired.com",
    "technologyreview.com", "theinformation.com", "semianalysis.com",
]


def get_api_key() -> str:
    key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if key:
        return key
    key_file = REPO_ROOT.parent / "anthropic_key.txt"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    sys.exit("错误:未找到 API Key(环境变量 ZHIPU_API_KEY 或 ../anthropic_key.txt)")


def search_queries(today: str) -> list[str]:
    return [
        f"AI Agent 大模型 重要新闻 {today}",
        "国产大模型 发布 动态 智谱 阿里 字节 DeepSeek",
        # 英文词偏向一线原始源(官方/权威媒体),抵消搜狗的腾讯系偏好
        "OpenAI Anthropic Google DeepMind announcement official blog",
        "AI agent startup funding launch TechCrunch Bloomberg Reuters",
        "LLM model release research paper enterprise adoption",
    ]


def web_search(key: str, query: str, recency: str, engine: str) -> list[dict]:
    r = requests.post(
        f"{BASE}/web_search",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "search_query": query[:70],
            "search_engine": engine,
            "search_intent": False,
            "count": 10,
            "search_recency_filter": recency,
            "content_size": "high",
        },
        timeout=60,
    )
    items = []
    if r.status_code == 200:
        items = r.json().get("search_result") or []
    linked = [it for it in items if (it.get("link") or "").strip()]
    print(f"搜索[{engine}/{recency}] {query[:30]} → 状态{r.status_code} "
          f"条数{len(items)} 带链接{len(linked)}", flush=True)
    if not linked:
        print(f"  响应体:{r.text[:200]}", flush=True)
    return linked


def tavily_search(key: str, query: str) -> list[dict]:
    """Tavily news 搜索;返回结果归一成与智谱一致的字段(title/link/media/publish_date/content)。"""
    try:
        r = requests.post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={
                "query": query[:380],
                "topic": "news",
                "search_depth": "basic",
                "days": 7,
                "max_results": 8,
                "include_domains": TIER1_DOMAINS,  # 只取一线源
            },
            timeout=60,
        )
    except requests.RequestException as e:
        print(f"Tavily[{query[:30]}] 请求异常:{e}", flush=True)
        return []
    if r.status_code != 200:
        print(f"Tavily[{query[:30]}] 状态{r.status_code}:{r.text[:200]}", flush=True)
        return []
    out = []
    for it in r.json().get("results") or []:
        link = (it.get("url") or "").strip()
        if not link:
            continue
        host = re.sub(r"^https?://(www\.)?", "", link).split("/")[0]
        out.append({
            "title": it.get("title", ""),
            "link": link,
            "media": host,                       # 用域名当来源名
            "publish_date": it.get("published_date", ""),
            "content": it.get("content", ""),
        })
    print(f"Tavily[{query[:30]}] → 条数{len(out)}", flush=True)
    return out


def _media_of(item: dict) -> str:
    m = (item.get("media") or "").strip()
    if m:
        return m
    # 无 media 字段时用域名兜底,避免都归到"未知"互相挤掉
    link = (item.get("link") or "").strip()
    host = re.sub(r"^https?://(www\.)?", "", link).split("/")[0]
    return host or "未知"


def _diversify(pool: list[dict], target: int, cap: int) -> list[dict]:
    """按来源轮转抽取:先保证多样性(每来源不超过 cap),凑不够再放宽。"""
    from collections import OrderedDict
    by_media: "OrderedDict[str, list]" = OrderedDict()
    for it in pool:
        by_media.setdefault(_media_of(it), []).append(it)
    selected, used = [], {m: 0 for m in by_media}
    # 第一轮:每来源最多 cap 条,轮流取
    for _ in range(cap):
        for media, lst in by_media.items():
            if used[media] < cap and used[media] < len(lst):
                selected.append(lst[used[media]])
                used[media] += 1
                if len(selected) >= target:
                    return selected
    # 凑不够 target,放宽上限继续填(保持已有顺序、避免重复)
    for media, lst in by_media.items():
        for it in lst[used[media]:]:
            selected.append(it)
            if len(selected) >= target:
                return selected
    return selected


def gather_material(key: str, today: str) -> str:
    pool, seen = [], set()
    # 跑完所有引擎再合并,不中途短路——这是来源多样性的关键
    for engine in ENGINES:
        before = len(pool)
        for recency in ("oneDay", "oneWeek"):
            for q in search_queries(today):
                for item in web_search(key, q, recency, engine):
                    link = item["link"].strip()
                    if link not in seen:
                        seen.add(link)
                        pool.append(item)
        print(f"引擎 {engine} 新增 {len(pool)-before} 条,池累计 {len(pool)}", flush=True)
    # 西方源(Tavily):有 Key 才跑,补一线国际原始源;没有则跳过(纯智谱兜底)
    tav_pool = []
    tav_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tav_key:
        for q in INTL_QUERIES:
            for item in tavily_search(tav_key, q):
                link = item["link"].strip()
                if link not in seen:
                    seen.add(link)
                    tav_pool.append(item)
        print(f"Tavily 新增 {len(tav_pool)} 条(一线源白名单)", flush=True)
    else:
        print("未配置 TAVILY_API_KEY,跳过西方源(仅用智谱)", flush=True)
    if len(pool) + len(tav_pool) < 8:
        sys.exit(f"错误:有效搜索结果只有 {len(pool)+len(tav_pool)} 条,不足以成稿")
    # Tavily 一线源放前面:轮转抽取从西方源起步,确保它们进素材且 GLM 先看到
    results = _diversify(tav_pool + pool, TARGET, MAX_PER_MEDIA)
    dist = {}
    for it in results:
        dist[_media_of(it)] = dist.get(_media_of(it), 0) + 1
    print(f"来源分布(抽取后):{dist}", flush=True)
    lines = []
    for i, it in enumerate(results, 1):
        lines.append(
            f"[{i}] {it.get('title','')}\n"
            f"  来源:{it.get('media','')} | 发布:{it.get('publish_date','')} | 链接:{it.get('link','')}\n"
            f"  内容:{(it.get('content') or '')[:600]}"
        )
    print(f"搜索完成:{len(results)} 条候选素材")
    return "\n\n".join(lines)


def recent_titles(n_posts: int = 3) -> list[str]:
    titles = []
    for f in sorted(POSTS_DIR.glob("20??-??-??.md"))[-n_posts:]:
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\*\*\d+\.\s*(.+?)\*\*\s*$", line.strip())
            if m:
                titles.append(f"[{f.stem}] {m.group(1)}")
    return titles


def build_prompt(today: str, material: str, dedup: list[str]) -> str:
    dedup_block = "\n".join(f"- {t}" for t in dedup) if dedup else "(无)"
    return f"""你是"AI 每日情报站"的编辑,读者是中文产品经理。今天是 {today}(北京时间)。

下面是搜索引擎抓回的候选素材(标题/来源/发布时间/链接/内容):

{material}

任务:从素材中挑出 4-6 条对 AI/Agent 领域最重要的新闻,写成中文摘要。

已报道过的条目(必须避开,不可重复报道同一事件):
{dedup_block}

输出格式(严格遵守):
# AI / Agent 领域新闻摘要 — {today}

## 今日要点

**1. 标题(含事件日期,如"6月10日")**
事实摘要 2-4 句,新名词用括号给一句白话解释。
**PM 信号:** 一句对产品经理的启示,讲清"这对做产品意味着什么"。
来源:[媒体名](链接)

(2-6 条同上)

## 趋势一句话

一句话概括今天几条新闻背后的共同趋势。

硬性要求:
- 来源链接只能从上面素材的"链接"字段里选,一字不改,严禁编造或拼接 URL
- 每条至少 1 个来源;同一事件有多条素材时可给 2 个来源
- 优先选影响面大的事件(模型发布、平台政策、重要产品、行业格局),忽略软文和重复信息
- 来源多样性:尽量让选中的 4-6 条来自不同媒体,不要 4 条都出自同一来源;同等重要时优先选官方公告、权威媒体(Bloomberg/Reuters/TechCrunch 等)或一线科技媒体,而非二手转载聚合号
- 国际视野:素材里若有来自 openai.com/anthropic.com/bloomberg.com/techcrunch.com 等国际一线源的条目,在同等重要前提下至少纳入 1-2 条,避免整篇只有国内新闻
- 把最终成品完整放在 <digest> 和 </digest> 之间,标签外不要放正文"""


def call_glm(key: str, prompt: str) -> dict:
    r = requests.post(
        f"{BASE}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 8000,
            "temperature": 0.6,
        },
        timeout=300,
    )
    if r.status_code != 200:
        sys.exit(f"错误:生成调用失败({r.status_code}):{r.text[:500]}")
    return r.json()


def extract_digest(data: dict) -> str:
    text = data["choices"][0]["message"]["content"] or ""
    m = re.search(r"<digest>(.*?)</digest>", text, re.DOTALL)
    if not m:
        sys.exit("错误:回复里没有 <digest> 标签,内容如下供排查:\n" + text[:2000])
    return m.group(1).strip() + "\n"


def validate(digest: str, today: str, material: str):
    problems = []
    if today not in digest.splitlines()[0]:
        problems.append("标题行缺少当日日期")
    if "## 今日要点" not in digest:
        problems.append("缺少'## 今日要点'")
    items = re.findall(r"^\*\*\d+\.", digest, re.MULTILINE)
    if len(items) < 4:
        problems.append(f"条目数只有 {len(items)},少于 4")
    if "趋势" not in digest:
        problems.append("缺少趋势段")
    links = re.findall(r"\]\((https?://[^)]+)\)", digest)
    if len(links) < 4:
        problems.append("来源链接过少")
    fabricated = [u for u in links if u not in material]
    if fabricated:
        problems.append(f"发现不在素材里的链接(疑似编造):{fabricated[:3]}")
    if problems:
        sys.exit("错误:摘要未通过校验:" + ";".join(problems) + "\n---\n" + digest)


def write_outputs(digest: str, today: str):
    (POSTS_DIR / f"{today}.md").write_text(digest, encoding="utf-8")
    dates = sorted(f.stem for f in POSTS_DIR.glob("20??-??-??.md"))
    (POSTS_DIR / "index.json").write_text(
        json.dumps(dates, ensure_ascii=False), encoding="utf-8")
    parts = [(POSTS_DIR / f"{d}.md").read_text(encoding="utf-8").strip() for d in dates]
    all_md = "# AI 每日情报站 — 全部摘要汇总\n\n" + "\n\n---\n\n".join(parts) + "\n"
    (POSTS_DIR / "all.md").write_text(all_md, encoding="utf-8")


def main():
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    key = get_api_key()
    material = gather_material(key, today)
    dedup = recent_titles()
    print(f"日期 {today},去重条目 {len(dedup)} 条,调用 {MODEL} ...")
    data = call_glm(key, build_prompt(today, material, dedup))
    digest = extract_digest(data)
    validate(digest, today, material)
    write_outputs(digest, today)
    u = data.get("usage", {})
    print(f"完成:posts/{today}.md 已写入,index.json/all.md 已更新")
    print(f"用量:输入 {u.get('prompt_tokens')} tok,输出 {u.get('completion_tokens')} tok")


if __name__ == "__main__":
    main()
