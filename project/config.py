"""
Global configuration module.
Loads all settings from .env file at project root.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ── Project Root ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.resolve()
load_dotenv(PROJECT_ROOT / ".env")

# ── LLM ──────────────────────────────────────────────────────
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "16000"))

# ── Skills ───────────────────────────────────────────────────
SKILLS_DIR = PROJECT_ROOT / "skills"

# ── ArXiv Fetch ──────────────────────────────────────────────
ARXIV_CATEGORIES = [
    "cond-mat.soft",       # 软物质、高分子物理、液晶、胶体
    "cond-mat.mtrl-sci",   # 材料科学
    "physics.chem-ph",     # 化学物理
    "physics.app-ph",      # 应用物理（功能材料、器件）
]
ARXIV_MAX_RESULTS = 200
FETCH_LOOKBACK_DAYS = 1

# arXiv 源配置 — 官方源不可达时可切换镜像
# 2026-06-30 测试：arxiv.org / rss.arxiv.org / export.arxiv.org 均可达
# 镜像：cn.arxiv.org 可达；xxx.itp.ac.cn / arxiv.org.cn 已下线
ARXIV_BASE = os.getenv("ARXIV_BASE", "https://arxiv.org")
ARXIV_RSS_BASE = os.getenv("ARXIV_RSS_BASE", "https://rss.arxiv.org")

# ── 抓取健壮性配置 ───────────────────────────────────────────
RSS_TIMEOUT = int(os.getenv("RSS_TIMEOUT", "15"))        # RSS 请求超时(秒)
RSS_MAX_RETRIES = int(os.getenv("RSS_MAX_RETRIES", "3"))  # RSS 重试次数
RSS_PARALLEL = os.getenv("RSS_PARALLEL", "1") == "1"     # 是否并行拉取分类

EPRINT_TIMEOUT = int(os.getenv("EPRINT_TIMEOUT", "60"))  # e-print 下载超时(秒)
EPRINT_MAX_RETRIES = int(os.getenv("EPRINT_MAX_RETRIES", "3"))

# ── 礼貌性配置 ───────────────────────────────────────────────
# 真实联系邮箱：写入 User-Agent 与 OpenAlex mailto，进 polite pool。
# 留空则 UA 不带邮箱、OpenAlex 用占位符。
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "").strip()
# 两次 e-print 下载之间的最小间隔(秒)，防止深读批量下载时被 arXiv 限流。
EPRINT_MIN_INTERVAL = float(os.getenv("EPRINT_MIN_INTERVAL", "3.0"))

# ── OpenAlex 补充数据源 ──────────────────────────────────────
OPENALEX_ENABLED = os.getenv("OPENALEX_ENABLED", "0") == "1"
OPENALEX_BASE = "https://api.openalex.org"

# ── Output ───────────────────────────────────────────────────
# 每日论文摘要输出目录（默认输出到 E 盘）
DAILY_OUTPUT_DIR = os.getenv(
    "DAILY_OUTPUT_DIR",
    "E:/ArXiv_Polymer_Papers/daily_papers"
)
