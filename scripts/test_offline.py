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


# ───────────── ① 续:cut off 词表 + 标题相似度兜底 ─────────────
def test_dedup_cutoff_and_titlesim():
    now = time.time()
    # Run#16 真实漏并:shuts down(在表) + cuts off access(原不在表)→ 词表补全后应并
    cutoff = [
        {"title": "Ars: Anthropic shuts down older Claude models", "content": "shut down",
         "media": "arstechnica.com", "link": "https://arstechnica.com/x", "published_ts": now},
        {"title": "The Verge: Anthropic cuts off access to flagship Claude",
         "content": "cuts off", "media": "theverge.com", "link": "https://theverge.com/y",
         "published_ts": now},
    ]
    ok(len(classify.dedup_events(cutoff, now=now)) == 1, "cut off + shuts down 应并为1事件")

    # 标题相似度兜底:同实体 + 无动作命中 + 标题近重复 → 并
    titlesim = [
        {"title": "OpenAI overview of its Sora video tool", "content": "profile",
         "media": "a.com", "link": "https://a.com/1", "published_ts": now},
        {"title": "OpenAI overview of its Sora video tool today", "content": "profile",
         "media": "b.com", "link": "https://b.com/2", "published_ts": now},
    ]
    ok(len(classify.dedup_events(titlesim, now=now)) == 1, "同实体无动作近重复标题应兜底并")

    # 同实体无动作但标题不同 → 不并(保守)
    diff_title = [
        {"title": "OpenAI overview of Sora", "content": "", "media": "a", "link": "1", "published_ts": now},
        {"title": "OpenAI dashboard for enterprise billing analytics", "content": "",
         "media": "b", "link": "2", "published_ts": now},
    ]
    ok(len(classify.dedup_events(diff_title, now=now)) == 2, "同实体无动作但标题差异大不应并")


# ───────────── ① 续:罕见实体(型号代号)驱动的结构性去重 ─────────────
def test_dedup_rare_entity():
    """Run#17 硬伤:「美政府要求 Anthropic 停用 Fable 5/Mythos 5」一事件出现4次/跨3类。
    动词是 offline/directive/orders/force,全不在动作词表 → 词表路线必漏。
    罕见型号代号 Fable 5 是极强同事件信号,应把 4 条并成 1。"""
    now = time.time()
    ban = [
        {"title": "Anthropic takes Fable 5 and Mythos 5 offline after US government directive",
         "content": "compliance directive", "media": "anthropic.com",
         "link": "https://anthropic.com/a", "published_ts": now},
        {"title": "Anthropic pulls Fable 5 and Mythos 5 offline under US order",
         "content": "national security", "media": "wired.com",
         "link": "https://wired.com/b", "published_ts": now},
        {"title": "US government orders Anthropic to take Fable 5 offline",
         "content": "white house", "media": "theverge.com",
         "link": "https://theverge.com/c", "published_ts": now},
        {"title": "Amazon research pushes White House to force Anthropic Fable 5 offline",
         "content": "amazon", "media": "36氪",
         "link": "https://36kr.com/d", "published_ts": now},
    ]
    out = classify.dedup_events(ban, now=now)
    ok(len(out) == 1, f"Fable 5 停用事件4条应并为1,实际 {len(out)}({[o['title'][:30] for o in out]})")
    ok(out[0].get("_merged_from") and len(out[0]["_merged_from"]) == 3,
       "应记录被并的另外3源")

    # 同型号 + 同公司 + 不同框架(动作检测可能噪声) → 单日报内按同事件并(B-1)。
    # 这正是 Run#17 漏并的结构:官方声明被误判 launch、WIRED 判 retire,实为同一停用事件。
    same_model_company = [
        {"title": "Anthropic statement on Fable 5", "content": "introduce launch",
         "media": "anthropic.com", "link": "https://anthropic.com/1", "published_ts": now},
        {"title": "Anthropic takes Fable 5 offline", "content": "retire deprecate",
         "media": "wired.com", "link": "https://wired.com/2", "published_ts": now},
    ]
    ok(len(classify.dedup_events(same_model_company, now=now)) == 1,
       "同型号+同公司(即便动作检测分歧)应并为同事件")

    # 守卫1b:同型号但不同公司 + 标题差异大 → 不应并(B-1 不触发,B-2 标题不过线)
    diff_company = [
        {"title": "Anthropic launches Fable 5 for coding", "content": "release",
         "media": "a.com", "link": "https://a.com/1", "published_ts": now},
        {"title": "Reseller bundles Fable 5 into enterprise suite pricing tiers",
         "content": "channel partner", "media": "b.com", "link": "https://b.com/2",
         "published_ts": now},
    ]
    ok(len(classify.dedup_events(diff_company, now=now)) == 2,
       "同型号但不同公司+标题差异大不应并")

    # 守卫2:常见模型家族(GPT-5)不算罕见实体——两篇无关 GPT-5 新闻不应靠型号误并
    common = [
        {"title": "GPT-5 tops new coding benchmark", "content": "benchmark",
         "media": "a.com", "link": "https://a.com/3", "published_ts": now},
        {"title": "GPT-5 API pricing cut for developers", "content": "pricing",
         "media": "b.com", "link": "https://b.com/4", "published_ts": now},
    ]
    ok(len(classify.dedup_events(common, now=now)) == 2,
       "无关的两篇 GPT-5(常见家族)不应靠型号误并")

    # 守卫3:不同罕见型号(Fable 5 vs Orion 2)不共享实体,不应并
    diff_model = [
        {"title": "Anthropic ships Fable 5", "content": "", "media": "a", "link": "5", "published_ts": now},
        {"title": "OpenAI ships Orion 2", "content": "", "media": "b", "link": "6", "published_ts": now},
    ]
    ok(len(classify.dedup_events(diff_model, now=now)) == 2, "不同型号不应并")


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

    # ② 续:法院判决 → policy(曾误进 product)
    it = {"title": "Google found liable in AI copyright case, court rules",
          "content": "verdict ruling", "media": "wired.com", "category_hint": ""}
    ok(classify.classify(it) == "policy", f"法院判决应进 policy,实际 {classify.classify(it)}")

    # ② 续:网信办/举报 → policy(中文监管,曾误进 llm)
    it = {"title": "网信办开设涉AI举报专区", "content": "网络安全 规范 监管部门",
          "media": "36氪", "category_hint": ""}
    ok(classify.classify(it) == "policy", f"网信办举报应进 policy,实际 {classify.classify(it)}")

    # Run#21:开源模型发布(标题含 open-source)→ oss,治被 model/coding 关键词拉进 llm
    it = {"title": "Kimi K2.7-Code: open-source coding model released",
          "content": "weights coding model", "media": "huggingface.co", "category_hint": ""}
    ok(classify.classify(it) == "oss", f"开源模型发布应进 oss,实际 {classify.classify(it)}")
    # 不误伤:标题无开源词的普通模型发布不被 oss 抢(仍 llm)
    it = {"title": "OpenAI releases GPT new model", "content": "large language model",
          "media": "OpenAI", "category_hint": "llm"}
    ok(classify.classify(it) == "llm", "非开源模型发布仍留 llm(oss 不误抢)")


# ───────────── AI 相关性闸 ─────────────
def test_ai_gate():
    ok(not classify.is_ai_relevant(
        {"title": "China Inc quiet layoffs", "content": "economy jobs", "media": "x"}),
        "非 AI 新闻应被判不相关")
    ok(classify.is_ai_relevant(
        {"title": "OpenAI ships agent", "content": "", "media": "x"}), "含实体应相关")
    # 一手纯 AI 官博:正文没 AI 词也兜底放行(它们几乎不发非 AI 内容)
    ok(classify.is_ai_relevant(
        {"title": "Some post", "content": "", "media": "OpenAI", "category_hint": "llm"}),
        "纯 AI 官博(OpenAI)兜底放行")
    # §5.10 核心修复:带 hint 的泛科技 RSS 不再一律放行——非 AI 内容要被剔
    ok(not classify.is_ai_relevant(
        {"title": "SpaceX valuation hits new high", "content": "rocket launch funding",
         "media": "VentureBeat", "category_hint": "biz"}),
        "VentureBeat 的 SpaceX 估值(非 AI)即便带 hint 也应被剔")
    ok(not classify.is_ai_relevant(
        {"title": "华为鸿蒙生态再思考", "content": "操作系统 国产替代 观点",
         "media": "半佛仙人", "category_hint": "product"}),
        "鸿蒙观点文(非 AI)即便带 hint 也应被剔")
    # 带 hint 但确含 AI 内容 → 仍放行(不误杀)
    ok(classify.is_ai_relevant(
        {"title": "VentureBeat: new LLM agent platform", "content": "large language model",
         "media": "VentureBeat", "category_hint": "agent"}),
        "带 hint 且含 AI 词应放行")
    # 'ai' 词边界:不被 'available/email' 误命中
    ok(not classify.is_ai_relevant(
        {"title": "New email available now", "content": "campaign maintain", "media": "x"}),
        "ai 子串不应误命中 email/available")
    # 中文夹缝里的独立 "AI" token 应判相关(治"涉AI举报"被相关性闸误杀)
    ok(classify.is_ai_relevant(
        {"title": "网信办开设涉AI举报专区", "content": "网络安全", "media": "x"}),
        "中文夹缝里的 AI 应判相关")
    # Run#18 收紧回归:标题非AI + 无具名实体 + 正文仅捎带泛词(算力)→ 应被剔
    ok(not classify.is_ai_relevant(
        {"title": "读懂SpaceX的两万亿故事", "content": "马斯克 商业航天 算力 资本",
         "media": "36氪", "category_hint": "biz"}),
        "SpaceX 商业稿(正文捎带算力)收紧后应被剔")
    ok(not classify.is_ai_relevant(
        {"title": "The bill that would let Jimmy Kimmel sue Brendan Carr is here",
         "content": "FCC chairman lawsuit free speech", "media": "theverge.com",
         "category_hint": "policy"}),
        "Kimmel/FCC 法案(非AI)收紧后应被剔")
    # 标题无AI词但正文含*具名*实体 → 仍放行(不误杀重要条目,如官方声明)
    ok(classify.is_ai_relevant(
        {"title": "Statement on US government directive to suspend access to flagship",
         "content": "Claude models affected", "media": "reuters.com"}),
        "标题无AI但正文含具名实体(Claude)应放行")
    # Run#21 修:arXiv 论文标题用 VLM/diffusion 等术语不含"AI"字,但 arxiv 是纯 AI 研究源 → 放行
    ok(classify.is_ai_relevant(
        {"title": "When Good Verifiers Go Bad: Self-Improving VLMs",
         "content": "vision language model", "media": "arXiv cs.AI", "category_hint": "research"}),
        "arXiv 论文(标题无AI字)应放行(纯AI研究源白名单)")
    # 不放宽过头:非白名单泛源、标题无AI信号、正文无具名实体 → 仍被剔
    ok(not classify.is_ai_relevant(
        {"title": "When Good Verifiers Go Bad", "content": "generic systems work",
         "media": "someblog.com", "category_hint": "research"}),
        "非白名单源、标题无AI信号应被剔")
    # Run#19 长尾修:36氪「9点1氪」晨间快讯大杂烩——正文捎带 AI 公司名也应被挡
    ok(not classify.is_ai_relevant(
        {"title": "9点1氪 | SpaceX估值创新高;胖东来回应;OpenAI 据传新融资",
         "content": "OpenAI Anthropic 融资 商业航天 FIFA", "media": "36氪",
         "category_hint": "biz"}),
        "36氪 9点1氪 晨间快讯(大杂烩)即便正文有 AI 公司名也应被挡")
    ok(not classify.is_ai_relevant(
        {"title": "AI 晚报:今日要点速览", "content": "OpenAI GPT 模型",
         "media": "36氪", "category_hint": "llm"}),
        "晚报/早报聚合栏目应被挡")
    # 不误杀:正经单条 AI 新闻标题(非快讯格式)仍放行
    ok(classify.is_ai_relevant(
        {"title": "OpenAI 发布 GPT-6 推理模型", "content": "新模型 发布",
         "media": "36氪", "category_hint": "llm"}),
        "正经单条 AI 新闻(非快讯格式)不应误杀")
    # M1 收紧:新浪整点快讯仍被挡
    ok(not classify.is_ai_relevant(
        {"title": "新浪AI热点小时报08时_今日实时AI热点速递", "content": "杂烩", "media": "新浪网"}),
        "新浪热点小时报应被挡")
    # M1 收紧后不误杀:"小时报告""实时…速递功能"等正经新闻应放行
    ok(classify.is_ai_relevant(
        {"title": "OpenAI 发布实时语音速递功能", "content": "模型", "media": "36氪"}),
        "实时语音速递(正经产品)不应误杀")
    ok(classify.is_ai_relevant(
        {"title": "GPT-5 小时报告显示推理提升", "content": "模型基准", "media": "x"}),
        "小时报告(非快讯)不应误杀")


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
    # ⑤ 续:榜单/天梯/排行/盘点 listicle 应判软文
    ok(classify.is_tutorial_softarticle(
        {"title": "2026 大模型天梯榜单出炉", "media": "csdn.net", "link": "https://csdn.net/y"}),
        "天梯/榜单 应判软文")
    ok(classify.is_tutorial_softarticle(
        {"title": "国产大模型排行大盘点", "media": "cnblogs", "link": "http://x"}),
        "排行/盘点 应判软文")
    # ⑤ 续:深度解读/深入解读/干货 listicle(Run#18 漏网的 csdn/音乐报告)
    ok(classify.is_tutorial_softarticle(
        {"title": "深度解读:网信办清朗整治AI应用乱象专项行动", "media": "blog.csdn.net",
         "link": "https://blog.csdn.net/x"}), "深度解读 应判软文")
    ok(classify.is_tutorial_softarticle(
        {"title": "全球音乐报告2026深入解读(究极干货)", "media": "rainlain.com",
         "link": "https://rainlain.com/y"}), "深入解读/干货 应判软文")
    # 普通新闻标题不命中
    ok(not classify.is_tutorial_softarticle(
        {"title": "OpenAI launches new model", "media": "openai", "link": "x"}),
        "普通新闻不应判软文")


# ───────────── ⑤ 续:自媒体软降权(沉底不清零) ─────────────
def test_selfmedia_weight():
    now = time.time()
    cn = classify.score({"published_ts": now, "media": "CSDN", "link": "https://csdn.net/a"}, now)
    t1 = classify.score({"published_ts": now, "media": "The Verge", "link": "https://theverge.com/a"}, now)
    mid = classify.score({"published_ts": now, "media": "某中文站", "link": "https://x.cn/a"}, now)
    low = classify.score({"published_ts": now, "media": "ipkd", "link": "https://ipkd.cn/a"}, now)
    ok(cn < t1, "自媒体(CSDN)应低于一线源")
    ok(cn < mid, "自媒体(0.2)应低于普通二三线站(0.45)")
    ok(cn > low, "自媒体(0.2)应高于 LOWQ 低质站(0)——保命不清零")


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
    ok(len(heroes) == 1 and heroes[0]["title"] == "T2",
       "hero 优先有题图条目(T2 有图,即便分低于无图的 T1)")
    # 兜底:全部无题图时退回纯按分选最高(N2 7.0 > N1 3.0)
    obj2, _ = gd.assemble(today, {
        "llm": [{"category": "llm", "title": "N1", "link": "https://e.com/n1",
                 "media": "x", "image": "", "sum": "s", "lead": ["p"], "take": "k", "_score": 3.0}],
        "biz": [{"category": "biz", "title": "N2", "link": "https://e.com/n2",
                 "media": "y", "image": "", "sum": "s", "lead": ["p"], "take": "k", "_score": 7.0}],
    })
    h2 = [i for i in obj2["items"] if i["hero"]]
    ok(len(h2) == 1 and h2[0]["title"] == "N2", "全无图时退回最高分为 hero")
    # hero 一线源优先:一线源+有图 即便分低,也胜过非一线(github/HN)+有图的高分
    obj3, _ = gd.assemble(today, {
        "llm": [{"category": "llm", "title": "HN1", "link": "https://github.com/x",
                 "media": "github.com", "image": "https://e.com/h.jpg",
                 "sum": "s", "lead": ["p"], "take": "k", "_score": 9.0}],
        "biz": [{"category": "biz", "title": "R1", "link": "https://reuters.com/x",
                 "media": "reuters.com", "image": "https://e.com/r.jpg",
                 "sum": "s", "lead": ["p"], "take": "k", "_score": 4.0}],
    })
    h3 = [i for i in obj3["items"] if i["hero"]]
    ok(len(h3) == 1 and h3[0]["title"] == "R1", "hero 优先一线源(reuters 胜 github 高赞)")
    ok({c["code"] for c in obj["categories"]} == {"llm", "biz"}, "只含出现的分类")
    for i in obj["items"]:
        ok(set(i) >= {"hero", "category", "src", "title", "url", "img", "sum",
                      "lead", "take"}, "前端契约字段齐全")
    md = gd.render_markdown(obj)
    ok("## 大模型" in md and "## 融资与商业" in md, "markdown 分类标题")
    ok("https://e.com/1" in md, "markdown 带来源链接")
    ok("绝对不要" not in md, "无 prompt 泄漏")


# ───────────── Hacker News 热度源(映射 + points 进排序) ─────────────
def test_hackernews():
    hit = {"title": "Big AI model released &amp; open", "url": "https://example.com/x",
           "points": 250, "created_at": "2026-06-14T00:00:00Z",
           "created_at_i": 1781740800, "story_text": "<p>some text</p>"}
    it = feeds._hn_item(hit)
    ok(it is not None and it["points"] == 250, "HN hit 映射出 points")
    ok(it["link"] == "https://example.com/x" and it["media"] == "example.com",
       "HN 链接/来源域名")
    ok(it["published_ts"] == 1781740800.0, "HN 时间戳")
    ok("<p>" not in it["content"] and "&" in it["title"], "HN 正文去标签 + 标题反转义")
    ok(feeds._hn_item({"title": "Ask HN: thoughts?", "url": ""}) is None,
       "无外链(Ask HN)应丢弃")
    # points 进 score:高热度应抬分(填上之前恒 0 的热度槽)
    now = time.time()
    hot = classify.score({"published_ts": now, "media": "example.com",
                          "link": "https://example.com/a", "points": 300}, now)
    cold = classify.score({"published_ts": now, "media": "example.com",
                           "link": "https://example.com/b", "points": 0}, now)
    ok(hot > cold, "高 points 应抬分(热度槽生效)")


def test_parse_json_array():
    ok(gd.parse_json_array('```json\n[{"n":1}]\n```') == [{"n": 1}], "容忍代码围栏")
    ok(gd.parse_json_array("前言 [{\"n\":2}] 后语") == [{"n": 2}], "容忍前后说明")
    ok(gd.parse_json_array("no array") is None, "无数组返回 None")
    # S1 修:数组后多余 [n] 引用,括号配平只取第一个平衡数组(不再整类静默丢失)
    ok(gd.parse_json_array('[{"n":1,"keep":true}] 注:已剔除[3]号') == [{"n": 1, "keep": True}],
       "数组后多余[n]引用不破坏解析")
    # S1 修:LLM 常见尾逗号,剥掉后能解析
    ok(gd.parse_json_array('[{"n":1},{"n":2},]') == [{"n": 1}, {"n": 2}], "尾逗号容错")
    # S1:字符串内的 ] 不误判数组结束
    ok(gd.parse_json_array('[{"n":1,"sum":"a]b"}]') == [{"n": 1, "sum": "a]b"}], "字符串内]不误判")


# ───────────── 治本:GLM keep/drop 质量判断(语义判垃圾,替代正则穷举) ─────────────
def test_glm_keepdrop():
    items = [
        {"title": "OpenAI 发布 GPT-6 推理模型", "content": "新模型"},
        {"title": "新浪AI热点小时报08时_今日实时AI热点速递", "content": "杂烩"},
        {"title": "Anthropic 完成新一轮融资", "content": "funding"},
        {"title": "大模型应用完全指南合集", "content": "x"},
    ]
    arr = [
        {"n": 1, "keep": True, "sum": "s1", "lead": ["p1", "p2"], "take": "t1"},
        {"n": 2, "keep": False, "reason": "聚合快讯,非单篇新闻"},
        {"n": 3, "sum": "s3", "lead": ["q1"], "take": "t3"},      # 无 keep=默认保留
        {"n": 4, "keep": False, "reason": "教程合集"},
    ]
    out, dropped = gd._apply_glm_records(arr, items, "llm")
    titles = [o["title"] for o in out]
    ok(len(out) == 2, f"keep=false 应剔除,剩 2 条,实际 {len(out)}")
    ok(titles[0].startswith("OpenAI") and titles[1].startswith("Anthropic"),
       "保留的是合格条目(新浪快讯/教程被剔)")
    ok(all(o["category"] == "llm" for o in out), "category 回填")
    ok(len(dropped) == 2 and any("快讯" in r for _, r in dropped),
       "剔除记录带理由(便于复查)")
    # 向后兼容:GLM 万一不给 keep 字段,默认保留(绝不误删全部)
    out2, _ = gd._apply_glm_records(
        [{"n": 1, "sum": "s", "lead": ["p"], "take": "t"}], [{"title": "X"}], "biz")
    ok(len(out2) == 1, "无 keep 字段默认保留(不误删)")
    # keep=true 但 sum/lead 残缺 → 不计入(数据不完整)
    out3, _ = gd._apply_glm_records(
        [{"n": 1, "keep": True, "sum": "", "lead": []}], [{"title": "Y"}], "biz")
    ok(len(out3) == 0, "keep=true 但缺 sum/lead 不计入")


# ───────────── 新栏目:GLM 语义标注(企业落地归类 + AI原生观察) ─────────────
def test_apply_tags():
    items = [{"title": "某银行用大模型做客服落地", "category": "product"},
             {"title": "Cursor 是 AI-first 编辑器", "category": "product"},
             {"title": "OpenAI 发布 GPT-6", "category": "llm"}]
    arr = [{"n": 1, "enterprise": True, "ai_native": False},
           {"n": 2, "enterprise": False, "ai_native": True},
           {"n": 3, "enterprise": False, "ai_native": False}]
    ent, ain = gd._apply_tags(arr, items)
    ok(ent == 1 and ain == 1, f"标注计数应 ent=1 ain=1,实际 {ent}/{ain}")
    ok(items[0]["category"] == "enterprise", "企业落地条改归 enterprise")
    ok(items[1].get("ai_native") is True, "AI原生条打观察标记(不改类)")
    ok(items[1]["category"] == "product", "AI原生不改原类(暂不开栏)")
    ok(items[2]["category"] == "llm", "未命中标签的保持原类")
    # 坏记录/越界不崩
    gd._apply_tags([{"n": 99}, "garbage", {"enterprise": True}], items)
    # enterprise 已在分类表
    ok("enterprise" in classify.CAT_CODES and classify.CAT_LABEL["enterprise"] == "企业落地",
       "enterprise 分类已注册")


# ───────────── 人工置顶/添加(manual.json 合并) ─────────────
def test_parse_manual():
    today = "2026-06-20"
    data = [
        {"date": today, "category": "product", "src": "TechRadar", "title": "Copilot 键可改回",
         "url": "https://techradar.com/x", "sum": "可重映射 AI 键", "lead": ["段1"],
         "take": "信号", "pin": True},
        {"date": "2026-01-01", "category": "llm", "title": "旧", "sum": "s", "lead": ["p"]},  # 非今天→不取
        {"category": "biz", "title": "无日期常驻", "sum": "s", "lead": ["p"]},                # 无date→取
        {"date": today, "title": "缺sum/lead", "category": "product"},                        # 不完整→跳
        "garbage",                                                                            # 坏类型→跳
    ]
    out = gd._parse_manual(data, today)
    ok(len(out) == 2, f"应取 2 条(今天pin + 无日期常驻),实际 {len(out)}")
    kb = out[0]
    ok(kb["_pin_hero"] is True and kb["_score"] >= 10000, "pin → 置顶高分(hero 优先)")
    ok(kb["link"] == "https://techradar.com/x" and kb["media"] == "TechRadar",
       "url/src 映射到内部 link/media")
    ok(kb["category"] == "product" and kb["lead"] == ["段1"], "字段正确")
    out2 = gd._parse_manual(
        [{"date": today, "category": "无此类", "title": "t", "sum": "s", "lead": ["p"]}], today)
    ok(out2 and out2[0]["category"] == "product", "未知分类回落 product")
    ok(gd._parse_manual("not a list", today) == [], "坏数据返回空不崩")


if __name__ == "__main__":
    for fn in [test_dedup_events, test_dedup_cutoff_and_titlesim,
               test_dedup_rare_entity,
               test_classify_actions, test_ai_gate,
               test_tutorial_filter, test_selfmedia_weight,
               test_parse, test_time_filter,
               test_score_rank, test_hackernews,
               test_assemble_render, test_parse_json_array,
               test_glm_keepdrop, test_apply_tags, test_parse_manual]:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n全部通过:{PASS} 项断言")
