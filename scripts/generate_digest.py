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
SEARCH_ENGINE = "search_pro"  # ¥0.03/次


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
        "OpenAI Anthropic Google AI news announcement",
        "国产大模型 发布 动态 智谱 阿里 字节 DeepSeek",
        "AI agent product launch funding news",
    ]


def web_search(key: str, query: str, recency: str) -> list[dict]:
    r = requests.post(
        f"{BASE}/web_search",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "search_query": query[:70],
            "search_engine": SEARCH_ENGINE,
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
    print(f"搜索[{recency}] {query[:35]} → 状态{r.status_code} 条数{len(items)}", flush=True)
    if not items:  # 调试:失败或 0 结果时打印原始响应
        print(f"  响应体:{r.text[:300]}", flush=True)
    return items


def gather_material(key: str, today: str) -> str:
    results, seen = [], set()
    for recency in ("oneDay", "oneWeek", "noLimit"):  # 逐级放宽时间范围
        for q in search_queries(today):
            for item in web_search(key, q, recency):
                link = item.get("link", "")
                if link and link not in seen:
                    seen.add(link)
                    results.append(item)
        if len(results) >= 15:
            break
    if len(results) < 8:
        sys.exit(f"错误:搜索结果只有 {len(results)} 条,不足以成稿")
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
