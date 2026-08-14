"""Detect clickable wiki keywords from article titles, headings, and short terms."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

WIKI_SKIP = {"index.md", "log.md"}
SKIP_HEADINGS = {
    "overview",
    "see also",
    "全书结构",
    "怎么往本页继续加",
    "在哪个工具上创建",
    "分享与维护",
    "三个定时场景",
    "三个触发场景",
    "任务类型决策表",
    "使用预期：知识面极广的实习生",
    "为什么同一句话答案完全不同",
    "把文件丢给 ai",
    "真正要带走的不是工具",
    "行业时间线（教程口径）",
}

SKIP_TERMS = {
    "ai",
    "llm",
    "overview",
    "sources",
    "raw",
    "updated",
    "archived",
    "see also",
    "python",
    "google drive",
    "pro/enterprise",
    "工具",
    "任务",
    "输出",
    "输入",
    "场景",
    "正文",
    "附录",
    "教程",
    "关键要点",
    "现在就试试",
    "并且",
    "还行",
    "扮演",
    "做事",
    "聊天",
    "混合",
    "给建议",
    "说清楚",
    "了解 ai",
    "待确认",
    "推断的",
    "查到的",
    "开关一",
    "开关二",
    "不要啰嗦",
    "不要说教",
    "努力推进了",
    "完成了 x",
    "今天吃红烧肉",
    "今天日历为空",
    "刚发的这句话",
    "你觉得怎么样",
    "我是产品经理",
    "力和运动",
    "最烦的事",
    "喜欢什么",
    "你现在就能用",
    "鸡汤",
    "每周周报草稿",
    "每日早报",
    "每月复盘",
    "买家电",
    "旅游攻略",
    "行业调研",
    "桌面分类",
    "任务列表变化",
    "竞品新动态",
    "收到合同邮件",
    "市场分析",
    "竞争分析",
    "全能辅助",
    "加了壳的产品",
    "你打开的那个东西",
    "全篇最重要的警告",
    "你应该能做的事",
    "我喜欢的方式",
    "什么值得封装",
    "穷人的 skill",
    "自己动手做每一件事",
    "10 个蓝色链接",
    "ai 建议你去做",
    "ai 直接帮你做",
    "一个 ai 什么都做",
    "讲解",
    "年终总结",
    "生活场景",
    "我是谁",
    "excel 趋势和异常",
    "pdf 合同对比",
    "六个阶段与自查标准",
    "让 ai 认识你",
    "ai 团队的产品经理",
    "loop 设置三步法",
    "你打开的那个 app / 入口",
    "2026 年 7 月的工具能力",
    "先串行验证，再并行运行",
}

SEED_TERMS = (
    "Prompt Engineering",
    "Context Engineering",
    "Multi-Agent",
    "Skills",
    "Skill",
    "MCP",
    "Chain of Thought",
    "Custom Instructions",
    "Scheduled Tasks",
    "Triggered Automation",
    "四要素",
    "四要素万能公式",
    "万能公式",
    "自定义指令",
    "上下文",
    "思维链",
    "先发散再收敛",
    "分步思考",
    "大模型",
    "定时任务",
    "触发式自动化",
    "关于我的说明书",
    "隐私红线",
    "能力跃迁",
    "人设与背景",
    "工作流步骤",
    "输出格式模板",
    "联网搜索",
    "应用连接",
)

SKIP_IF_CONTAINS = (
    "→",
    "截至",
    "（阶段",
    "(阶段",
    "阶段一",
    "阶段二",
    "阶段三",
    "阶段四",
    "阶段五",
    "阶段六",
    "50%",
    "100 次",
    "2024",
)

HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.M)
BOLD_RE = re.compile(r"\*\*([^*]{1,40})\*\*")
QUOTE_RE = re.compile(r"「([^」]{2,16})」")
PAREN_EN_RE = re.compile(r"[（(]([A-Za-z][A-Za-z0-9][A-Za-z0-9 /&+\-]{1,40})[）)]")
PRODUCT_RE = re.compile(
    r"\b(ChatGPT(?: Work)?|Claude(?: Code| Cowork| App)?|Kimi(?: Code| Work| Claw| K3| K2\.6)?"
    r"|GPT-(?:5(?:\.\d)?|6)|Sonnet 5|DeepSeek|Llama|Hermes|Codex|OpenClaw"
    r"|MCP|AgentKit|HiAgent 3\.0|Ultra Mode|Sub-agents|Agent Clusters"
    r"|Custom Instructions|Chain of Thought|Scheduled Tasks"
    r"|Triggered Automation|Plugin Directory|Office Agent|GPT Store)\b"
)
META_RE = re.compile(r"^>\s*(Category|Status|Aliases|Sources|Raw|Updated|Archived|Generation|Evidence|Redirect):", re.I)
REDIRECT_RE = re.compile(r"^>\s*Redirect:\s*(.+?)\s*$", re.M | re.I)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class Keyword:
    term: str
    path: str | None = None
    title: str | None = None
    aliases: list[str] = field(default_factory=list)


def wiki_root(project_root: Path) -> Path:
    return project_root / "wiki"


def iter_articles(project_root: Path) -> list[Path]:
    root = wiki_root(project_root)
    if not root.exists():
        return []
    files = []
    for path in sorted(root.rglob("*.md")):
        if path.name in WIKI_SKIP:
            continue
        try:
            head = path.read_text(encoding="utf-8")[:4096]
        except (OSError, UnicodeError):
            continue
        if REDIRECT_RE.search(head):
            continue
        files.append(path)
    return files


def rel_article(project_root: Path, path: Path) -> str:
    return path.relative_to(wiki_root(project_root)).as_posix()


def normalize(term: str) -> str:
    return re.sub(r"\s+", " ", term).strip()


def is_skippable(term: str) -> bool:
    t = normalize(term)
    if not t:
        return True
    low = t.lower()
    if low in SKIP_TERMS or t in SKIP_TERMS:
        return True
    if t.endswith(("？", "?", "。", "！", "!")):
        return True
    if any(token in t for token in SKIP_IF_CONTAINS):
        return True
    if t.startswith(("为什么", "怎么", "如何", "如果", "你觉得", "你打开", "我是谁")):
        return True
    if "，" in t or "、" in t:
        return True
    if "2026" in t or "年 7 月" in t:
        return True
    if "[" in t or "]" in t or t.startswith("Agent "):
        return True
    if t.startswith("PPT") or "/" in t and "Word" in t:
        return True
    if "+" in t:
        return True
    if len(t) < 2:
        return True
    cjk = len(CJK_RE.findall(t))
    if cjk:
        if cjk > 12 or len(t) > 18:
            return True
    elif len(t) < 3 or len(t) > 28:
        return True
    if t.isdigit():
        return True
    return False


def clean_heading(raw: str) -> str:
    text = raw.strip()
    text = re.sub(r"^第\s*\d+\s*节[：:].*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def bold_label(raw: str) -> list[str]:
    text = raw.strip().rstrip("。.:：；,，")
    text = re.sub(r"^[①-⑳\d]+[\.、\s]+", "", text)
    out = []
    m = re.match(r"^(.+?)[（(]([A-Za-z].+?)[）)]$", text)
    if m:
        left, right = normalize(m.group(1)), normalize(m.group(2))
        if not is_skippable(left):
            out.append(left)
        if not is_skippable(right):
            out.append(right)
        return out
    if "—" in text or "–" in text:
        text = re.split(r"[—–]", text, maxsplit=1)[0].strip()
    if "：" in text or ":" in text:
        text = re.split(r"[：:]", text, maxsplit=1)[0].strip()
    if "=" in text:
        for part in text.split("="):
            part = normalize(part)
            if not is_skippable(part):
                out.append(part)
        return out
    if " / " in text:
        for part in text.split("/"):
            part = normalize(part)
            if not is_skippable(part):
                out.append(part)
        return out
    text = normalize(text)
    if not is_skippable(text):
        out.append(text)
    return out


def parse_title(md: str) -> str | None:
    for line in md.splitlines():
        if line.startswith("# "):
            return normalize(line[2:])
    return None


def extract_from_markdown(md: str) -> set[str]:
    terms: set[str] = set()
    body_lines = []
    for line in md.splitlines():
        if META_RE.match(line):
            continue
        body_lines.append(line)
    body = "\n".join(body_lines)
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    body = re.sub(r"~~~.*?~~~", "", body, flags=re.S)

    for _, heading in HEADING_RE.findall(body):
        heading = clean_heading(heading)
        if not heading or heading.lower() in SKIP_HEADINGS:
            continue
        if any(ch in heading for ch in "，、。？?："):
            continue
        if heading.startswith(("你", "我", "一个", "一天", "什么", "全篇", "自己")):
            continue
        if is_skippable(heading):
            continue
        if heading.lower().startswith("阶段"):
            continue
        if " / " in heading:
            for part in heading.split("/"):
                part = normalize(part)
                if not is_skippable(part):
                    terms.add(part)
            continue
        terms.add(heading)

    for bold in BOLD_RE.findall(body):
        for item in bold_label(bold):
            terms.add(item)

    for quoted in QUOTE_RE.findall(body):
        q = normalize(quoted)
        if not is_skippable(q) and len(q) <= 10 and "、" not in q and "，" not in q:
            terms.add(q)

    for en in PAREN_EN_RE.findall(body):
        en = normalize(en)
        if not is_skippable(en):
            terms.add(en)

    for prod in PRODUCT_RE.findall(body):
        if not is_skippable(prod):
            terms.add(prod)

    return terms


def collect_keywords(project_root: Path) -> list[dict]:
    """Return keywords sorted longest-first. Existing pages win over bare terms."""
    by_norm: dict[str, Keyword] = {}

    def upsert(term: str, path: str | None = None, title: str | None = None) -> None:
        term = normalize(term)
        if not term:
            return
        if not path and is_skippable(term):
            return
        key = term.lower()
        existing = by_norm.get(key)
        if existing is None:
            by_norm[key] = Keyword(term=term, path=path, title=title or term)
            return
        if path and not existing.path:
            existing.path = path
            existing.title = title or existing.title
        if title and existing.title == existing.term:
            existing.title = title
        if term != existing.term and term not in existing.aliases:
            existing.aliases.append(term)

    for path in iter_articles(project_root):
        rel = rel_article(project_root, path)
        md = path.read_text(encoding="utf-8")
        title = parse_title(md) or path.stem
        upsert(title, path=rel, title=title)
        from aliases import parse_aliases

        for alias in parse_aliases(md):
            upsert(alias, path=rel, title=title)
        for term in extract_from_markdown(md):
            if term.lower() == title.lower():
                upsert(term, path=rel, title=title)
            else:
                upsert(term)

    corpus = "\n".join(p.read_text(encoding="utf-8") for p in iter_articles(project_root))
    for seed in SEED_TERMS:
        if seed.lower() in corpus.lower():
            upsert(seed)

    from aliases import SEED_ALIASES, norm as alias_norm

    titled = {alias_norm(kw.term): kw for kw in by_norm.values() if kw.path}
    titled.update({alias_norm(kw.title or ""): kw for kw in by_norm.values() if kw.path and kw.title})
    for alias, canonical in SEED_ALIASES.items():
        host = titled.get(alias_norm(canonical))
        if host and host.path:
            upsert(alias, path=host.path, title=host.title)

    rows = []
    for kw in by_norm.values():
        rows.append(
            {
                "term": kw.term,
                "path": kw.path,
                "title": kw.title or kw.term,
                "aliases": kw.aliases,
            }
        )
    rows.sort(key=lambda r: (-len(r["term"]), r["term"]))
    return rows


def clip_para(para: str, limit: int = 900) -> str:
    clip = re.sub(r"\s+", " ", para).strip()
    if clip.startswith(">"):
        clip = clip.lstrip("> ").strip()
    if len(clip) > limit:
        clip = clip[: limit - 3] + "..."
    return clip


def _match_needles(text: str, needles: list[str]) -> bool:
    low = text.lower()
    return any(n.lower() in low for n in needles if n)


def excerpts_for(
    project_root: Path,
    keyword: str,
    limit: int = 16,
    extra_needles: list[str] | None = None,
) -> list[dict]:
    needle = keyword.strip()
    if not needle:
        return []
    needles = [needle]
    for extra in extra_needles or []:
        extra = normalize(extra)
        if extra and extra.lower() != needle.lower() and extra not in needles:
            needles.append(extra)

    found: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(title: str, path: str, para: str) -> bool:
        clip = clip_para(para)
        if len(clip) < 20:
            return False
        key = (path, clip[:80])
        if key in seen:
            return False
        seen.add(key)
        found.append({"title": title, "path": path, "text": clip})
        return len(found) >= limit

    for path in iter_articles(project_root):
        md = path.read_text(encoding="utf-8")
        title = parse_title(md) or path.stem
        rel = rel_article(project_root, path)
        hits = 0
        for para in re.split(r"\n\s*\n", md):
            if not _match_needles(para, needles):
                continue
            if add(title, rel, para):
                return found
            hits += 1
            if hits >= 3:
                break

    raw_root = project_root / "raw"
    if raw_root.exists():
        for path in sorted(raw_root.rglob("*")):
            if path.suffix.lower() not in {".md", ".markdown", ".txt"} or path.is_symlink():
                continue
            md = path.read_text(encoding="utf-8")
            title = parse_title(md) or path.stem
            rel = path.relative_to(project_root).as_posix()
            for para in re.split(r"\n\s*\n", md):
                if not _match_needles(para, [needle]):
                    continue
                if add(title, rel, para):
                    return found
                break
    return found


def about_the_term(text: str, keyword: str) -> bool:
    k = re.escape(keyword)
    if re.search(k + r".{0,36}(是指|指的是|叫做|定义为|是一种|是一个|是目前|就是|这是)", text, re.S):
        return True
    if re.search(r"(所谓|称为|叫作|叫做)" + k, text):
        return True
    stripped = text.lstrip("#>* ").strip()
    return stripped.startswith(keyword)


def coverage_sufficient(excerpts: list[dict], keyword: str) -> bool:
    evidence = coverage_evidence(excerpts, keyword)
    return evidence["sufficient"]


def coverage_evidence(excerpts: list[dict], keyword: str) -> dict:
    relevant = [item for item in excerpts if keyword.casefold() in item["text"].casefold()]
    if not relevant:
        return {"sufficient": False, "reason": "no_local_evidence", "evidence_count": 0, "document_count": 0, "char_count": 0}
    for item in relevant:
        if len(item["text"]) >= 80 and about_the_term(item["text"], keyword):
            return {
                "sufficient": True,
                "reason": "explicit_definition",
                "evidence_count": len(relevant),
                "document_count": len({entry["path"] for entry in relevant}),
                "char_count": sum(len(entry["text"]) for entry in relevant),
            }
    documents = {item["path"] for item in relevant}
    joined = "\n".join(item["text"] for item in relevant)
    mentions = sum(item["text"].casefold().count(keyword.casefold()) for item in relevant)
    has_wiki = any(not item["path"].startswith("raw/") for item in relevant)
    sufficient = len(documents) >= 2 and mentions >= 2 and len(joined) >= 350 and has_wiki
    return {
        "sufficient": sufficient,
        "reason": "multi_document_coverage" if sufficient else "insufficient_local_evidence",
        "evidence_count": len(relevant),
        "document_count": len(documents),
        "char_count": len(joined),
        "mentions": mentions,
        "has_wiki": has_wiki,
    }
