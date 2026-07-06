#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高分子物理论文翻译 + WebUI 生成
----------------------------------
把 daily_polymer.py 生成的论文笔记翻译成简体中文,
只翻译 [标题 / 英文摘要 / 英文阅读笔记段落], 作者/arXiv ID等保持原样。
通过 DeepSeek API (OpenAI 兼容) 调用模型翻译, 按文本哈希缓存。

用法:
    python translate_polymer.py                  # 翻译今天最新一期
    python translate_polymer.py --date 2026-06-30  # 指定日期
    python translate_polymer.py --no-cache       # 不读缓存(仍会写)
    python translate_polymer.py --no-open        # 生成后不自动打开 HTML

输出: daily_papers/<date>_zh.html (带左侧目录的 WebUI)
"""
import argparse, hashlib, json, os, pathlib, re, sys, time, webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import escape as _html_escape

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 输出根目录以 config.DAILY_OUTPUT_DIR 为准（与 daily_polymer 保持一致）
DEFAULT_PAPERS_ROOT = str(config.DAILY_OUTPUT_DIR)
CACHE_NAME = "translations.json"

# LLM 配置统一走 config（config.py 已 load_dotenv，无需在此重复加载）
API_URL = config.LLM_BASE_URL.rstrip("/") + "/chat/completions"
API_KEY = config.LLM_API_KEY.strip()
DEFAULT_MODEL = config.LLM_MODEL

SYS_PROMPT = (
    "你是专业的科技文献翻译，专精高分子物理领域。把用户给的英文内容准确翻译成简体中文。"
    "要求: 保留专业术语(如 liquid crystal elastomer→液晶弹性体, kirigami→剪纸, "
    "shear softening→剪切软化, DPD→耗散粒子动力学, MD→分子动力学)、"
    "化学式与下标(如 Ca²⁺, RM257, FeTiO₃)、数学符号(如 $\\epsilon$, τ, E⊥)、"
    "计量单位原义; 译文通顺、符合学术中文习惯; "
    "只输出译文本身, 不要任何解释、前后缀或引号。"
)


# ----------------------------- 缓存 -----------------------------
def _key(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def load_cache(cache_path):
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cache(cache, cache_path):
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    tmp = cache_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0)
    os.replace(tmp, cache_path)


# ----------------------------- 翻译 -----------------------------
def translate_one(text, api_key, model, timeout=60):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last = ""
    for attempt in range(3):
        try:
            r = requests.post(API_URL, json=payload, headers=headers, timeout=timeout)
            if r.status_code == 200:
                zh = r.json()["choices"][0]["message"]["content"].strip()
                zh = zh.strip("\"'「」 ")
                zh = re.sub(r"^(译文|翻译)\s*[:：]\s*", "", zh)
                return zh or None
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code in (400, 401, 402, 403):
                break
        except Exception as ex:
            last = ex.__class__.__name__
        time.sleep(1.5 * (attempt + 1))
    print(f"  [!] 翻译失败(保留原文): {last}  | 原文: {text[:50]}...")
    return None


def translate_missing(texts, cache, api_key, model, cache_path, workers=4):
    missing = [t for t in texts if _key(t) not in cache]
    if not missing:
        return 0
    print(f"[i] 需翻译 {len(missing)} 段, 调用 {model} ...")

    def work(t):
        return t, translate_one(t, api_key, model)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for t, zh in ex.map(work, missing):
            if zh:
                cache[_key(t)] = zh
                done += 1
    if done:
        save_cache(cache, cache_path)
    return done


def tr(cache, text):
    return cache.get(_key(text), text)


# ----------------------------- 笔记解析 -----------------------------
def parse_paper_note(md_text):
    """从一篇论文笔记 md 中提取需翻译的字段。
    返回 dict: {title_en, abstract_en, notes_sections: [(heading_en, body_en)]}
    """
    result = {"title_en": "", "abstract_en": "", "notes_sections": []}

    lines = md_text.splitlines()

    # 提取 YAML frontmatter 中的 title
    in_front = False
    for line in lines:
        if line.strip() == "---":
            in_front = not in_front
            continue
        if in_front:
            m = re.match(r'^title:\s*"(.+)"', line)
            if m:
                result["title_en"] = m.group(1)

    # 提取 Abstract（第一段 ## Abstract 到 --- 之间的英文摘要）
    # 摘要行通常格式: "arXiv:XXXXX Announce Type: ... Abstract: 实际内容"
    abstract_lines = []
    in_abstract = False
    in_notes = False
    notes_start_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("## Abstract"):
            in_abstract = True
            continue
        if in_abstract and line.strip() == "---":
            in_abstract = False
            continue
        if in_abstract:
            abstract_lines.append(line)
        # 找到 Reading Notes 部分
        if line.strip() == "## Reading Notes":
            in_notes = True
            notes_start_idx = i
            continue

    # 清洗摘要: 去掉 "arXiv:XXXXX Announce Type: ..." 前缀, 只取 "Abstract: " 之后的内容
    abstract_raw = "\n".join(abstract_lines).strip()
    m = re.search(r"Abstract:\s*", abstract_raw)
    if m:
        abstract_raw = abstract_raw[m.end():]
    result["abstract_en"] = abstract_raw.strip()

    # 提取阅读笔记中的英文段落
    # 笔记可能已部分是中文(由 LLM reader 直接生成中文笔记), 只翻译仍为英文的部分
    if notes_start_idx is not None:
        notes_text = "\n".join(lines[notes_start_idx + 1:])
        # 去掉笔记开头的第二个 YAML frontmatter (--- ... ---)
        notes_text = notes_text.lstrip("\n")
        fm = re.match(r'^---\n.*?\n---\n', notes_text, re.S)
        if fm:
            notes_text = notes_text[fm.end():]
        # 去掉笔记标题重复行 + arXiv 链接引用行 + "论文阅读笔记"标题
        notes_text = notes_text.lstrip("\n")
        notes_text = re.sub(r'^#\s+[^\n]+\n', '', notes_text, count=1)
        notes_text = re.sub(r'^>\s*\*\*ArXiv\*\*[^\n]*\n', '', notes_text, count=1)
        notes_text = re.sub(r'^#\s*论文阅读笔记\n', '', notes_text, count=1)
        notes_text = notes_text.lstrip("\n")
        # 判断笔记主体是否已是中文
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', notes_text))
        total_alpha = len(re.findall(r'[a-zA-Z]', notes_text))
        # 如果中文字符占比 > 30%, 视为已翻译, 不再翻译笔记
        if chinese_chars > total_alpha * 0.3 and chinese_chars > 20:
            result["notes_already_zh"] = True
            result["notes_full"] = notes_text
        else:
            result["notes_already_zh"] = False
            # 找出纯英文段落
            sections = []
            current_heading = ""
            current_body_lines = []
            for line in notes_text.splitlines():
                if re.match(r'^#{1,4}\s+', line):
                    if current_heading and current_body_lines:
                        body = "\n".join(current_body_lines).strip()
                        if body and len(re.findall(r'[a-zA-Z]', body)) > len(re.findall(r'[\u4e00-\u9fff]', body)):
                            sections.append((current_heading, body))
                    current_heading = line
                    current_body_lines = []
                else:
                    current_body_lines.append(line)
            if current_heading and current_body_lines:
                body = "\n".join(current_body_lines).strip()
                if body and len(re.findall(r'[a-zA-Z]', body)) > len(re.findall(r'[\u4e00-\u9fff]', body)):
                    sections.append((current_heading, body))
            result["notes_sections"] = sections

    return result


def collect_all_texts(paper_dir, date_str):
    """扫描指定日期的所有论文笔记, 收集需翻译的文本。"""
    day_dir = os.path.join(paper_dir, date_str)
    if not os.path.isdir(day_dir):
        return [], []

    texts = []
    papers = []

    for fname in sorted(os.listdir(day_dir)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(day_dir, fname)
        with open(fpath, encoding="utf-8") as f:
            md = f.read()
        parsed = parse_paper_note(md)
        parsed["file"] = fname
        parsed["arxiv_id"] = fname[:-3]  # 去掉 .md

        if parsed["title_en"]:
            texts.append(parsed["title_en"])
        if parsed["abstract_en"]:
            texts.append(parsed["abstract_en"])
        for heading, body in parsed["notes_sections"]:
            texts.append(body)

        papers.append(parsed)

    # 去重保序
    seen, out = set(), []
    for t in texts:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out, papers


# ----------------------------- HTML 渲染 -----------------------------
def render_html(papers, cache, run_id, papers_root, other_md=None):
    """生成带左侧目录的 WebUI HTML。"""

    # 翻译标题和摘要
    zh_papers = []
    for p in papers:
        zh_title = tr(cache, p["title_en"]) if p["title_en"] else ""
        zh_abstract = tr(cache, p["abstract_en"]) if p["abstract_en"] else ""

        zh_notes = ""
        if p.get("notes_already_zh"):
            zh_notes = p.get("notes_full", "")
        else:
            # 翻译笔记的英文段落
            translated_sections = []
            for heading, body in p["notes_sections"]:
                zh_body = tr(cache, body)
                translated_sections.append(f"{heading}\n{zh_body}")
            zh_notes = "\n\n".join(translated_sections)

        zh_papers.append({
            "arxiv_id": p["arxiv_id"],
            "title_en": p["title_en"],
            "title_zh": zh_title,
            "abstract_zh": zh_abstract,
            "notes_zh": zh_notes,
            "authors": "",  # 从 md 提取
            "category": "",
        })

    # 从原文 md 提取 authors 和 category
    day_dir = os.path.join(papers_root, run_id)
    for zp in zh_papers:
        fpath = os.path.join(day_dir, zp["arxiv_id"] + ".md")
        try:
            with open(fpath, encoding="utf-8") as f:
                md = f.read()
            m = re.search(r'^authors:\s*"(.+)"', md, re.M)
            if m:
                zp["authors"] = m.group(1)
            m = re.search(r'^categories:\s*"(.+)"', md, re.M)
            if m:
                zp["category"] = m.group(1)
        except Exception:
            pass

    # 构建 HTML
    arxiv_base = config.ARXIV_BASE

    toc_items = ""
    cards = ""
    for i, zp in enumerate(zh_papers):
        short_title = zp["title_zh"][:40] + ("..." if len(zp["title_zh"]) > 40 else "")
        toc_items += f'<a href="#p{i+1}" class="toc-item">{short_title}</a>\n'

        # 笔记内容: HTML 安全转换
        notes_html = _md_to_html(zp["notes_zh"])

        cards += f'''
<div class="card" id="p{i+1}">
  <div class="card-header">
    <h3>{i+1}. {zp["title_zh"]}</h3>
    <p class="title-en">{zp["title_en"]}</p>
  </div>
  <div class="card-meta">
    <span class="meta-label">arXiv:</span>
    <a href="{arxiv_base}/abs/{zp["arxiv_id"]}" target="_blank">{zp["arxiv_id"]}</a>
    &nbsp;&nbsp;
    <span class="meta-label">作者:</span> {zp["authors"]}
    &nbsp;&nbsp;
    <span class="meta-label">分类:</span> {zp["category"]}
  </div>
  <div class="card-abstract">
    <b>摘要:</b> {zp["abstract_zh"]}
  </div>
  <div class="card-notes">
    {notes_html}
  </div>
  <div class="card-footer">
    🔗 <a href="{arxiv_base}/abs/{zp["arxiv_id"]}" target="_blank">原文链接</a>
    &nbsp;|&nbsp;
    📄 <a href="{arxiv_base}/pdf/{zp["arxiv_id"]}" target="_blank">PDF</a>
  </div>
</div>
'''

    # 其他论文表格
    other_html = ""
    if other_md:
        other_html = _render_other_table(other_md, cache)

    html = f'''<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>高分子物理论文日报 · {run_id}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  color: #222;
  line-height: 1.7;
  display: flex;
  min-height: 100vh;
}}
.sidebar {{
  width: 240px;
  position: fixed;
  top: 0; left: 0;
  height: 100vh;
  background: #f7f8fa;
  border-right: 1px solid #e0e0e0;
  overflow-y: auto;
  padding: 20px 12px;
  z-index: 100;
}}
.sidebar h2 {{
  font-size: 15px;
  color: #4a7;
  margin-bottom: 12px;
  border-bottom: 2px solid #4a7;
  padding-bottom: 4px;
}}
.toc-item {{
  display: block;
  font-size: 13px;
  color: #555;
  padding: 4px 6px;
  border-radius: 4px;
  text-decoration: none;
  margin-bottom: 3px;
  transition: background 0.2s;
}}
.toc-item:hover {{
  background: #e8f5e9;
  color: #2e7d32;
}}
.main {{
  margin-left: 240px;
  padding: 32px 24px;
  max-width: 860px;
  width: calc(100% - 240px);
}}
h1 {{
  font-size: 24px;
  margin-bottom: 8px;
  color: #1a1a1a;
}}
.summary {{
  color: #666;
  margin-bottom: 24px;
  font-size: 14px;
}}
.card {{
  background: #fff;
  border: 1px solid #e8e8e8;
  border-radius: 8px;
  padding: 20px;
  margin-bottom: 24px;
  transition: box-shadow 0.2s;
}}
.card:hover {{
  box-shadow: 0 2px 12px rgba(0,0,0,0.08);
}}
.card-header h3 {{
  font-size: 18px;
  color: #1a1a1a;
  margin-bottom: 4px;
}}
.title-en {{
  font-size: 13px;
  color: #888;
  margin-bottom: 8px;
}}
.card-meta {{
  font-size: 13px;
  color: #555;
  margin-bottom: 12px;
}}
.meta-label {{
  color: #888;
}}
.card-abstract {{
  font-size: 14px;
  margin-bottom: 16px;
  padding: 12px;
  background: #f5f5f5;
  border-radius: 6px;
}}
.card-notes {{
  font-size: 14px;
  margin-bottom: 16px;
}}
.card-notes h4 {{
  font-size: 15px;
  margin-top: 12px;
  margin-bottom: 4px;
  color: #4a7;
}}
.card-notes ul, .card-notes ol {{
  margin-left: 20px;
  margin-bottom: 8px;
}}
.card-notes p {{
  margin-bottom: 6px;
}}
.card-notes pre {{
  background: #f5f5f5;
  padding: 10px;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 13px;
  margin: 8px 0;
}}
.card-notes code {{
  background: #f0f0f0;
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 13px;
}}
.card-notes pre code {{
  background: none;
  padding: 0;
}}
.card-notes blockquote {{
  border-left: 3px solid #4a7;
  padding-left: 12px;
  color: #666;
  margin: 8px 0;
}}
.card-notes .md-table {{
  border-collapse: collapse;
  width: 100%;
  margin: 8px 0;
  font-size: 13px;
}}
.card-notes .md-table th,
.card-notes .md-table td {{
  border: 1px solid #e0e0e0;
  padding: 6px 8px;
  text-align: left;
}}
.card-notes .md-table th {{
  background: #f7f8fa;
}}
.card-footer {{
  font-size: 13px;
  padding-top: 12px;
  border-top: 1px solid #eee;
}}
.card-footer a {{
  color: #06c;
}}
.other-section {{
  margin-top: 32px;
}}
.other-section h2 {{
  border-bottom: 2px solid #4a7;
  padding-bottom: 4px;
  margin-bottom: 16px;
}}
.other-table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}}
.other-table th {{
  background: #f7f8fa;
  padding: 6px 8px;
  text-align: left;
  border-bottom: 1px solid #ddd;
}}
.other-table td {{
  padding: 6px 8px;
  border-bottom: 1px solid #eee;
}}
.other-table a {{
  color: #06c;
}}
@media (max-width: 800px) {{
  .sidebar {{ display: none; }}
  .main {{ margin-left: 0; width: 100%; max-width: 100%; }}
}}
</style>
</head>
<body>
<div class="sidebar">
  <h2>📑 目录导航</h2>
  {toc_items}
</div>
<div class="main">
  <h1>📚 高分子物理论文日报 · {run_id}</h1>
  <div class="summary">
    > 共 {len(zh_papers)} 篇高分子物理相关论文 · 从 arXiv cond-mat.soft / mtrl-sci / chem-ph / app-ph 扫描
  </div>
  {cards}
  {other_html}
</div>
</body>
</html>'''

    return html


def _safe_url(url):
    """只允许 http(s)/mailto/相对路径/锚点，拒绝 javascript: 等危险协议。"""
    url = url.strip()
    if re.match(r"^(https?:|mailto:|/|#)", url, re.IGNORECASE):
        return url
    return "#"


def _md_to_html(md_text):
    """Markdown → HTML 转换。

    支持标题/列表/粗体/斜体/行内代码/代码块/引用/表格/链接/段落。
    所有文本先做 HTML 转义再应用 markdown 转换，避免注入。
    """
    if not md_text:
        return ""
    # 先去掉残留的 YAML frontmatter
    fm = re.match(r'^---\n.*?\n---\n', md_text, re.S)
    if fm:
        md_text = md_text[fm.end():]
    md_text = md_text.lstrip("\n")
    # 去掉笔记开头的英文标题行 + arXiv 链接引用行 + "论文阅读笔记"标题
    md_text = re.sub(r'^#\s+[^\n]+\n', '', md_text, count=1)
    md_text = re.sub(r'^>\s*\*\*ArXiv\*\*[^\n]*\n', '', md_text, count=1)
    md_text = re.sub(r'^#\s*论文阅读笔记\n', '', md_text, count=1)
    md_text = md_text.lstrip("\n")

    # 先抽取代码块，避免其内容被后续行内转换破坏
    code_blocks: list[str] = []

    def _stash_code(m):
        code_blocks.append(f"<pre><code>{_html_escape(m.group(1))}</code></pre>")
        return f"\x00CB{len(code_blocks) - 1}\x00"

    md_text = re.sub(r"```[a-zA-Z0-9_+-]*\n(.*?)```", _stash_code, md_text, flags=re.DOTALL)

    lines = md_text.splitlines()
    html_parts: list[str] = []
    in_list = False
    list_type = ""
    table_buf: list[str] = []

    def close_list():
        nonlocal in_list
        if in_list:
            html_parts.append(f"</{list_type}>")
            in_list = False

    def flush_table():
        nonlocal table_buf
        if table_buf:
            html_parts.append(_render_table_block(table_buf))
            table_buf = []

    for line in lines:
        stripped = line.strip()

        # 代码块占位符（整行即占位符）
        m = re.match(r"^\x00CB(\d+)\x00$", stripped)
        if m:
            close_list()
            flush_table()
            html_parts.append(code_blocks[int(m.group(1))])
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.+)", stripped)
        if m:
            close_list()
            flush_table()
            level = min(len(m.group(1)), 4)
            html_parts.append(f"<h{level}>{_inline_md(m.group(2))}</h{level}>")
            continue

        # 引用块
        m = re.match(r"^>\s?(.*)", stripped)
        if m:
            close_list()
            flush_table()
            html_parts.append(f"<blockquote>{_inline_md(m.group(1))}</blockquote>")
            continue

        # 表格行（| ... | ... |）
        if (
            stripped.startswith("|")
            and stripped.endswith("|")
            and "|" in stripped[1:-1]
        ):
            close_list()
            table_buf.append(stripped)
            continue

        # 非表格行：先冲刷已缓冲的表格
        flush_table()

        # 无序列表项
        m = re.match(r"^[-*]\s+(.+)", stripped)
        if m:
            if not in_list or list_type != "ul":
                close_list()
                html_parts.append("<ul>")
                in_list = True
                list_type = "ul"
            html_parts.append(f"<li>{_inline_md(m.group(1))}</li>")
            continue

        # 有序列表项
        m = re.match(r"^\d+\.\s+(.+)", stripped)
        if m:
            if not in_list or list_type != "ol":
                close_list()
                html_parts.append("<ol>")
                in_list = True
                list_type = "ol"
            html_parts.append(f"<li>{_inline_md(m.group(1))}</li>")
            continue

        # 空行
        if not stripped:
            close_list()
            continue

        # 段落
        close_list()
        html_parts.append(f"<p>{_inline_md(stripped)}</p>")

    close_list()
    flush_table()

    result = "\n".join(html_parts)
    # 安全网：替换任何残留在行内的代码块占位符
    result = re.sub(r"\x00CB(\d+)\x00", lambda m: code_blocks[int(m.group(1))], result)
    return result


def _render_table_block(table_lines):
    """把连续的 markdown 表格行渲染成 <table>。"""
    rows = []
    for line in table_lines:
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [c.strip() for c in line[1:-1].split("|")]
        rows.append(cells)
    if not rows:
        return ""

    # 第二行若是分隔行（:---: / --- 等）则第一行作表头
    header = rows[0]
    body_start = 1
    if (
        len(rows) >= 2
        and all(re.match(r"^:?-+:?$", c) for c in rows[1] if c)
    ):
        body_start = 2

    out = ['<table class="md-table"><thead><tr>']
    out.append("".join(f"<th>{_inline_md(c)}</th>" for c in header))
    out.append("</tr></thead><tbody>")
    for row in rows[body_start:]:
        out.append("<tr>" + "".join(f"<td>{_inline_md(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _inline_md(text):
    """处理行内 Markdown: 粗体、斜体、行内代码、链接。先做 HTML 转义防注入。"""
    text = _html_escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)

    def _link_repl(m):
        label, url = m.group(1), m.group(2)
        return f'<a href="{_safe_url(url)}" target="_blank">{label}</a>'

    text = re.sub(r"\[(.+?)\]\((.+?)\)", _link_repl, text)
    return text


def _render_other_table(other_md, cache):
    """把 _other.md 渲染成 HTML 表格。"""
    if not other_md or not os.path.isfile(other_md):
        return ""
    with open(other_md, encoding="utf-8") as f:
        md = f.read()

    rows = []
    for line in md.splitlines():
        m = re.match(r'^\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|', line)
        if m:
            cols = [c.strip() for c in m.groups()]
            # 跳过表头和分隔行
            if cols[0].startswith("arXiv") or cols[0].startswith("-"):
                continue
            # 翻译标题
            title_zh = tr(cache, cols[1])
            rows.append((cols[0], cols[1], title_zh, cols[2]))

    if not rows:
        return ""

    arxiv_base = config.ARXIV_BASE
    table_rows = ""
    for arxiv_id, title_en, title_zh, cat in rows:
        table_rows += f'''<tr>
  <td><a href="{arxiv_base}/abs/{arxiv_id}" target="_blank">{arxiv_id}</a></td>
  <td>{title_zh}<br><span style="color:#888;font-size:12px">{title_en[:60]}{'...' if len(title_en)>60 else ''}</span></td>
  <td>{cat}</td>
</tr>'''

    return f'''
<div class="other-section">
  <h2>📋 其他相关论文 ({len(rows)} 篇)</h2>
  <table class="other-table">
    <tr><th>arXiv ID</th><th>标题</th><th>分类</th></tr>
    {table_rows}
  </table>
</div>'''


# ----------------------------- 主流程 -----------------------------
def build_report(run_id, papers_root=None, model=None, use_cache=True,
                 workers=4, open_browser=True):
    """把某个 run 的论文笔记翻译并生成中文 HTML 报告。

    返回生成的 HTML 路径；若该 run 目录不存在返回 None。
    API_KEY 缺失时抛 RuntimeError（不 sys.exit，以免杀掉编排进程）。
    """
    if not API_KEY:
        raise RuntimeError("缺少 API Key: 请在 .env 中设置 LLM_API_KEY")

    if papers_root is None:
        papers_root = DEFAULT_PAPERS_ROOT
    model = model or DEFAULT_MODEL
    cache_path = os.path.join(papers_root, CACHE_NAME)

    day_dir = os.path.join(papers_root, run_id)
    if not os.path.isdir(day_dir):
        print(f"[!] 未找到 run 目录: {day_dir}")
        return None

    print(f"[i] 翻译 run: {run_id}")
    print(f"[i] 论文目录: {day_dir}")

    texts, papers = collect_all_texts(papers_root, run_id)
    print(f"[i] 待翻译片段: {len(texts)} 段(标题/摘要/笔记, 去重后)")
    print(f"[i] 论文数量: {len(papers)} 篇")

    cache = load_cache(cache_path) if use_cache else {}
    new_n = translate_missing(texts, cache, API_KEY, model, cache_path, workers=workers)
    print(f"[i] 本次新翻译 {new_n} 段, 缓存命中 {len(texts) - new_n} 段")

    # 翻译 other 论文标题
    other_md = os.path.join(papers_root, f"{run_id}_other.md")
    if os.path.isfile(other_md):
        with open(other_md, encoding="utf-8") as f:
            omd = f.read()
        other_titles = []
        for line in omd.splitlines():
            m = re.match(r'^\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|', line)
            if m:
                cols = [c.strip() for c in m.groups()]
                if not cols[0].startswith("arXiv") and not cols[0].startswith("-"):
                    other_titles.append(cols[1])
        # 去重
        other_titles = list(dict.fromkeys(other_titles))
        if other_titles:
            print(f"[i] 翻译 other 论文标题: {len(other_titles)} 段")
            translate_missing(other_titles, cache, API_KEY, model, cache_path, workers=workers)

    html = render_html(papers, cache, run_id, papers_root, other_md)

    html_path = os.path.join(papers_root, f"{run_id}_zh.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n[✓] 完成!")
    print(f"    HTML:  {html_path}")
    print(f"    缓存:  {cache_path}")

    if open_browser:
        try:
            webbrowser.open(pathlib.Path(html_path).as_uri())
        except Exception:
            pass

    return html_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", help="run 标识 (对应输出子目录名)")
    ap.add_argument("--date", help="[兼容] 旧的日期参数, 等价于 --run-id")
    ap.add_argument("--papers-root", default=None, help="输出根目录 (默认取 config)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"模型 id (默认 {DEFAULT_MODEL})")
    ap.add_argument("--no-cache", action="store_true", help="忽略已有缓存重新翻译")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-open", action="store_true", help="生成后不自动打开 HTML")
    args = ap.parse_args()

    run_id = args.run_id or args.date or datetime.now().strftime("%Y-%m-%d")
    result = build_report(
        run_id=run_id,
        papers_root=args.papers_root,
        model=args.model,
        use_cache=not args.no_cache,
        workers=args.workers,
        open_browser=not args.no_open,
    )
    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
