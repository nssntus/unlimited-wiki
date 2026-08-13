"""Stable topic labels for generated and existing wiki pages."""

from __future__ import annotations

import re
from pathlib import Path

# id, Chinese label, one-line blurb, extra match hints
CATALOG: list[tuple[str, str, str, tuple[str, ...]]] = [
    (
        "ai-practice",
        "能力路径",
        "六个阶段总图与底层比喻",
        ("能力跃迁", "发动机", "整车", "4s 店", "大模型"),
    ),
    (
        "prompt-engineering",
        "提问与提示",
        "把话说清楚：公式、开关、思维链",
        (
            "四要素",
            "万能公式",
            "思维链",
            "分步思考",
            "先发散",
            "prompt",
            "chain of thought",
            "格式约束",
            "角色",
        ),
    ),
    (
        "context-engineering",
        "上下文与记忆",
        "让 AI 认识你：说明书、文件、记忆",
        ("自定义指令", "上下文", "说明书", "记忆", "custom instruction", "对话历史"),
    ),
    (
        "privacy",
        "隐私与合规",
        "能传什么、不能传什么",
        ("hipaa", "隐私", "脱敏", "身份证", "合规", "医疗记录", "保密"),
    ),
    (
        "harness-tools",
        "工具与手脚",
        "搜索、文件、应用连接、MCP",
        ("mcp", "model context protocol", "应用连接", "联网搜索", "plugin", "harness"),
    ),
    (
        "skills",
        "技能封装",
        "把反复做的事收成可调用模块",
        ("skill", "技能", "工作流步骤", "输出格式模板"),
    ),
    (
        "loops-automation",
        "自动化",
        "定时任务与事件触发",
        ("定时", "触发式", "自动化", "loop", "早报", "scheduled"),
    ),
    (
        "multi-agent",
        "多智能体",
        "拆任务、并行、流水线",
        ("multi-agent", "多智能体", "编排", "sub-agent", "agent cluster"),
    ),
    (
        "tools",
        "产品与生态",
        "ChatGPT / Claude / Kimi 等产品",
        ("chatgpt", "claude", "kimi", "豆包", "通义", "元宝", "gpt-", "sonnet"),
    ),
    (
        "concepts",
        "未分类",
        "尚未归入上面各类的词条",
        (),
    ),
]

BY_ID = {row[0]: row for row in CATALOG}
ORDER = [row[0] for row in CATALOG]


def label_of(cat_id: str) -> str:
    row = BY_ID.get(cat_id)
    return row[1] if row else cat_id


def blurb_of(cat_id: str) -> str:
    row = BY_ID.get(cat_id)
    return row[2] if row else ""


def parse_category_line(md: str) -> str | None:
    m = re.search(r"^>\s*Category:\s*([a-z0-9-]+)\s*$", md, re.M | re.I)
    if not m:
        return None
    cid = m.group(1).lower()
    return cid if cid in BY_ID else None


def classify(term: str, from_path: str = "", heading: str = "", passage: str = "") -> str:
    blob = " ".join([term, heading, passage, from_path.replace("/", " ").replace("-", " ")]).lower()
    scores: dict[str, int] = {}
    for cid, _label, _blurb, hints in CATALOG:
        if cid == "concepts":
            continue
        score = 0
        for hint in hints:
            if hint.lower() in blob:
                score += 2 if hint.lower() == term.lower() else 1
        if from_path.startswith(cid + "/") or from_path.startswith(cid + "\\"):
            score += 3
        if score:
            scores[cid] = score
    if scores:
        return max(scores, key=lambda k: (scores[k], -ORDER.index(k)))
    folder = Path(from_path).parts[0] if from_path else ""
    if folder in BY_ID and folder != "concepts":
        return folder
    return "concepts"


def category_for_article(md: str, rel: str) -> str:
    tagged = parse_category_line(md)
    if tagged:
        return tagged
    folder = Path(rel).parent.name
    if folder in BY_ID and folder != "concepts":
        return folder
    title = ""
    for line in md.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    return classify(title or Path(rel).stem, rel)


def ensure_category_header(md: str, cat_id: str) -> str:
    if parse_category_line(md) == cat_id:
        return md
    md = re.sub(r"^>\s*Category:.*\n", "", md, flags=re.M)
    lines = md.splitlines()
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            insert_at = i + 1
            if insert_at < len(lines) and lines[insert_at] == "":
                insert_at += 1
            break
    lines.insert(insert_at, f"> Category: {cat_id}")
    if insert_at + 1 >= len(lines) or not lines[insert_at + 1].startswith(">"):
        # keep following metadata flush; add blank only if next is body
        pass
    return "\n".join(lines) + ("\n" if md.endswith("\n") else "")
