#!/usr/bin/env python3
"""
离线单测(无外网、不调模型)。覆盖阶段B 调优新逻辑 + 核心纯函数回归。
跑法:python3 scripts/test_offline.py  (任何断言失败即非零退出)

重点验证本轮 4 项调优(对应接力棒 §5.6 待调优清单):
  ① 事件级跨类去重(同一事件多源/中英 → 归并一条)
  ② 分类收紧:动作概念覆盖媒体先验(acquire→biz / investigation→policy)
  ⑤ 教程软文过滤(CSDN 完全指南/实战;一线源 how-to 不误杀)
  + AI 相关性闸(非 AI 新闻剔除)、og 兜底前提、parse/score/rank/assemble 回归。
"""
import sys
import time

import classify
import feeds
import generate_digest as gd

PASS = 0


def ok(cond, msg):
    global PASS
    assert cond, "FAIL: " + msg
    PASS += 1


# ───────────── ① 事件级去重 ─────────────
def test_dedup_events():
    now = time.time()
    same_event = [
        {"title": "Anthropic retires its top Claude model", "content": "deprecate",
         "media": "reuters.com", "link": "https://reuters.com/a", "published_ts": now},
        {"title": "Anthropic 停用顶级模型 Claude", "content": "下架",
         "media": "36氪", "link": "https://36kr.com/b", "published_ts": now},
        {"title": "The Verge: Anthropic deprecates flagship model",
         "content": "", "media": "theverge.com", "link": "https://theverge.com/c",
         "published_ts": now},
        {"title": "WIRED on Anthropic shutting down old Claude",
         "content": "shut down", "media": "wired.com", "link": "https://wired.com/d",
         "published_ts": now},
        # 不同事件:同公司不同动作,不应被并
        {"title": "Anthropic launches new agent feature", "content": "launch",
         "media": "techcrunch.com", "link": "https://techcrunch.com/e", "published_ts": now},
        # 无动作概念:不参与归并,原样保留
        {"title": "OpenAI overview", "content": "company profile",
         "media": "x.com", "link": "https://x.com/f", "published_ts": now},
    ]
    out = classify.dedup_events(same_event, now=now)
    titles = [o["title"] for o in out]
    ok(len(out) == 3, f"4条同事件应并为1 + launch + overview = 3,实际 {len(out)} ({titles})")
    merged = [o for o in out if o.get("_merged_from")]
    ok(len(merged) == 1, "应有1条带 _merged_from")
    ok(len(merged[0]["_merged_from"]) == 3, "被并的另外3源应记录")
    ok(any("launch" in t.lower() for t in titles), "不同动作(launch)不应被并掉")
    ok(any("overview" in t.lower() for t in titles), "无动作条目应保留")

    # 跨实体不并:不同公司同动作
    diff_ent = [
        {"title": "OpenAI acquires Ona", "content": "", "media": "a", "link": "1", "published_ts": now},
        {"title": "Google acquires startup", "content": "", "media": "b", "link": "2", "published_ts": now},
    ]
    ok(len(classify.dedup_events(diff_ent, now=now)) == 2, "不同实体同动作不应并")


# ───────────── ② 分类收紧 ─────────────
def test_classify_actions():
    # acquire → biz,即使媒体先验是 OpenAI(llm)
    it = {"title": "OpenAI to acquire Ona", "content": "acquisition deal",
          "media": "OpenAI", "category_hint": ""}
    ok(classify.classify(it) == "biz", f"收购应进 biz,实际 {classify.classify(it)}")

    # investigation → policy(媒体 OpenAI 先验仍是 llm)
    it = {"title": "OpenAI under investigation by state AGs", "content": "probe",
          "media": "OpenAI", "category_hint": ""}
    ok(classify.classify(it) == "policy", f"调查应进 policy,实际 {classify.classify(it)}")

    # funding → biz
    it = {"title": "AI startup raises $100M funding round", "content": "venture",
          "media": "techcrunch.com", "category_hint": ""}
    ok(classify.classify(it) == "biz", "融资应进 biz")

    # 模型发布仍走 llm(launch 未映射到分类,避免误判)
    it = {"title": "OpenAI releases GPT new model", "content": "large language model",
          "media": "OpenAI", "category_hint": "llm"}
    ok(classify.classify(it) == "llm", "模型发布应留 llm")


# ───────────── AI 相关性闸 ─────────────
def test_ai_gate():
    ok(not classify.is_ai_relevant(
        {"title": "China Inc quiet layoffs", "content": "economy jobs", "media": "x"}),
        "非 AI 新闻应被判不相关")
    ok(classify.is_ai_relevant(
        {"title": "OpenAI ships agent", "content": "", "media": "x"}), "含实体应相关")
    ok(classify.is_ai_relevant(
        {"title": "Some post", "content": "", "media": "OpenAI", "category_hint": "llm"}),
        "RSS 源(带 hint)直接放行")
    # 'ai' 词边界:不被 'available/email' 误命中
    ok(not classify.is_ai_relevant(
        {"title": "New email available now", "content": "campaign maintain", "media": "x"}),
        "ai 子串不应误命中 email/available")


# ───────────── ⑤ 教程软文过滤 ─────────────
def test_tutorial_filter():
    ok(classify.is_tutorial_softarticle(
        {"title": "大模型应用完全指南", "media": "csdn.net", "link": "https://csdn.net/x"}),
        "CSDN 完全指南应判软文")
    ok(classify.is_tutorial_softarticle(
        {"title": "Agent 开发实战避坑", "media": "cnblogs", "link": "http://x"}),
        "实战/避坑应判软文")
    # 一线源的 how-to 不误杀
    ok(not classify.is_tutorial_softarticle(
        {"title": "How to use Claude step-by-step", "media": "theverge.com",
         "link": "https://theverge.com/x"}), "一线源 how-to 不应误杀")
    # 普通新闻标题不命中
    ok(not classify.is_tutorial_softarticle(
        {"title": "OpenAI launches new model", "media": "openai", "link": "x"}),
        "普通新闻不应判软文")


# ───────────── 回归:parse / score / rank / assemble / render ─────────────
RSS2 = """<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel><item><title>Test A</title><link>https://e.com/a</link>
<pubDate>Sat, 13 Jun 2026 10:00:00 GMT</pubDate>
<description>agent tool use</description>
<enclosure url="https://e.com/a.jpg" type="image/jpeg"/></item></channel></rss>"""

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Test B</title>
<link rel="alternate" href="https://e.com/b"/><published>2026-06-13T09:00:00Z</published>
<content>research paper benchmark</content></entry></feed>"""

RSS1 = """<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<item><title>Test C</title><link>https://arxiv.org/c</link>
<dc:date>2026-06-13T08:00:00Z</dc:date><description>scaling reasoning</description></item></rdf:RDF>"""


def test_parse():
    a = feeds.parse_rss(RSS2, "X", "agent")
    ok(len(a) == 1 and a[0]["image"] == "https://e.com/a.jpg", "RSS2 enclosure 题图")
    b = feeds.parse_rss(ATOM, "Y", "research")
    ok(len(b) == 1 and b[0]["link"] == "https://e.com/b", "Atom 链接")
    c = feeds.parse_rss(RSS1, "arXiv cs.AI", "research")
    ok(len(c) == 1 and c[0]["title"] == "Test C", "RSS1/RDF item 解析(arXiv)")
    ok(feeds.parse_rss("<broken", "Z") == [], "坏 XML 返回空不抛")


def test_time_filter():
    now = time.time()
    items = [
        {"published_ts": now, "title": "fresh"},
        {"published_ts": now - 86400 * 30, "title": "old"},
        {"published_ts": None, "title": "nodate"},
    ]
    kept = feeds._recent_and_capped(items, cap=10, days=4)
    titles = [k["title"] for k in kept]
    ok("fresh" in titles and "old" not in titles, "近4天过滤掉30天前")
    ok("nodate" in titles, "无日期保留")


def test_score_rank():
    now = time.time()
    fresh = classify.score({"published_ts": now, "media": "openai", "image": "x"}, now)
    stale = classify.score({"published_ts": now - 86400 * 5, "media": "ipkd.cn"}, now)
    ok(fresh > stale, "新鲜+一线+有图 应高于 陈旧+低质")
    items = [
        {"title": "GPT model", "content": "llm", "media": "openai",
         "link": "1", "published_ts": now},
        {"title": "agent tool", "content": "agentic mcp", "media": "x",
         "link": "2", "published_ts": now},
    ]
    for it in items:
        it["category"] = classify.classify_or_default(it)
    buckets = classify.rank_within_categories(items, per_cat_max=12, now=now)
    ok(sum(len(v) for v in buckets.values()) == 2, "排序分桶不丢条")


def test_assemble_render():
    today = "2026-06-14"
    written = {
        "llm": [{"category": "llm", "title": "T1", "link": "https://e.com/1",
                 "media": "openai", "image": "", "sum": "s1", "lead": ["p1"],
                 "take": "k1", "_score": 9.0}],
        "biz": [{"category": "biz", "title": "T2", "link": "https://e.com/2",
                 "media": "tc", "image": "https://e.com/2.jpg", "sum": "s2",
                 "lead": ["p2"], "take": "k2", "_score": 5.0}],
    }
    obj, items = gd.assemble(today, written)
    ok(obj["date"] == today and len(obj["items"]) == 2, "组装条数")
    heroes = [i for i in obj["items"] if i["hero"]]
    ok(len(heroes) == 1 and heroes[0]["title"] == "T1", "最高分为 hero")
    ok({c["code"] for c in obj["categories"]} == {"llm", "biz"}, "只含出现的分类")
    for i in obj["items"]:
        ok(set(i) >= {"hero", "category", "src", "title", "url", "img", "sum",
                      "lead", "take"}, "前端契约字段齐全")
    md = gd.render_markdown(obj)
    ok("## 大模型" in md and "## 融资与商业" in md, "markdown 分类标题")
    ok("https://e.com/1" in md, "markdown 带来源链接")
    ok("绝对不要" not in md, "无 prompt 泄漏")


def test_parse_json_array():
    ok(gd.parse_json_array('```json\n[{"n":1}]\n```') == [{"n": 1}], "容忍代码围栏")
    ok(gd.parse_json_array("前言 [{\"n\":2}] 后语") == [{"n": 2}], "容忍前后说明")
    ok(gd.parse_json_array("no array") is None, "无数组返回 None")


if __name__ == "__main__":
    for fn in [test_dedup_events, test_classify_actions, test_ai_gate,
               test_tutorial_filter, test_parse, test_time_filter,
               test_score_rank, test_assemble_render, test_parse_json_array]:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n全部通过:{PASS} 项断言")
