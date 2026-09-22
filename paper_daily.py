import requests
from bs4 import BeautifulSoup
import json
import time
import signal
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import logging
import re
import traceback
import os
import math
from urllib.parse import urljoin

# -------------------------- 全局变量与信号处理 --------------------------

all_papers_global: Dict[str, List[Dict]] = {}
llm_quota_exhausted = False

# -------------------------- 基础配置 --------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("arxiv_crawler.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

ARXIV_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.8,en-US;q=0.5,en;q=0.3",
    "Connection": "keep-alive",
}

# LLM 配置（OpenRouter）
# 优先使用 OPENROUTER_API_KEY；保留 LLM_API_KEY 兼容旧 workflow。
LLM_API_KEY = (
    os.getenv("OPENROUTER_API_KEY")
    or os.getenv("LLM_API_KEY")
    or ""
).strip()
LLM_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
PREFERRED_LLM_MODEL = "qwen/qwen3.8-27b:free"
LLM_MODEL = PREFERRED_LLM_MODEL
LLM_PROMPT = os.getenv("LLM_PROMPT")

# 请求节流与共享池临时 429 的退避。
LLM_REQUEST_INTERVAL = 3.2
LLM_TRANSIENT_429_BACKOFF = (10, 20, 40, 60, 90)
_last_llm_request_at = 0.0

# OpenRouter 可选的应用标识头；不影响鉴权。
OPENROUTER_HTTP_REFERER = os.getenv(
    "OPENROUTER_HTTP_REFERER",
    "https://github.com/RainbowNebula/robot-paper-daily",
)
OPENROUTER_APP_TITLE = os.getenv(
    "OPENROUTER_APP_TITLE",
    "robot-paper-daily",
)

# 爬取配置
REQUEST_INTERVAL = 1.2
PAPERS_PER_PAGE = 100

# None = 根据 arXiv 当天论文总数自动翻页。
# 如果设置整数，例如 3，表示最多实际抓 3 页，而不是 page index <= 3。
MAX_CRAWL_PAGES: Optional[int] = None

INITIAL_ARXIV_URL = "https://arxiv.org/list/cs.RO/recent?show=100"

# 存储配置
JSON_SAVE_PATH = "arxiv_cs_ro_papers_final.json"
MD_SAVE_PATH = "README.md"
RECENT_DISPLAY_DAYS = 5

# -------------------------- 正则 --------------------------

URL_PATTERN = re.compile(r"https?://\S+|www\.\S+")
PDF_LINK_PATTERN = re.compile(r"pdf", re.IGNORECASE)

# 支持：
# 分数：5分
# 分数： 5分
# 分数: 5分
# 【相关性】分数： 5分
# 评分：5
# 相关性评分：5/5
LLM_SCORE_PATTERN = re.compile(
    r"(?:【?\s*相关性\s*】?\s*)?"
    r"(?:相关性\s*)?(?:评分|分数)"
    r"\s*[：:]\s*\**\s*([1-5])\s*\**\s*(?:分|/5)?",
    re.IGNORECASE,
)

# 再放宽一层，只要出现“分数：5分”也可以抓到
LLM_SCORE_FALLBACK_PATTERN = re.compile(
    r"分数\s*[：:]\s*\**\s*([1-5])\s*\**\s*分",
    re.IGNORECASE,
)

PAGE_FIGURE_FRAGMENT_PATTERN = re.compile(
    r"\d+\s+(pages?|page)\s*,?\s*\d*\s*(figures?|figure)?"
    r"\s*,?\s*\d*\s*(tables?|table)?",
    re.IGNORECASE,
)

ARXIV_DATE_PATTERN = re.compile(
    r"^([A-Za-z]{3},\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4})"
)

ARXIV_TOTAL_PATTERN = re.compile(
    r"\bof\s+(\d+)\s+entries\b",
    re.IGNORECASE,
)

ARXIV_SIMPLE_TOTAL_PATTERN = re.compile(
    r"\((?:[^)]*?\b)?(\d+)\s+entries\b",
    re.IGNORECASE,
)

ARXIV_ID_PATTERN = re.compile(
    r"arxiv\.org/(?:abs|html|pdf)/([^/?#]+)",
    re.IGNORECASE,
)


# -------------------------- 基础工具 --------------------------

def save_json() -> None:
    """保存当前全局论文数据。"""
    with open(JSON_SAVE_PATH, "w", encoding="utf-8") as f:
        json.dump(all_papers_global, f, ensure_ascii=False, indent=4)


def signal_handler(sig, frame):
    """处理 Ctrl+C，确保中断时保存数据。"""
    logging.info("\n检测到手动中断（Ctrl+C），正在保存当前数据...")
    try:
        if all_papers_global:
            save_json()
            logging.info(
                "已保存数据到 JSON，包含 %d 个日期的数据",
                len(all_papers_global),
            )
            json_to_markdown(JSON_SAVE_PATH, MD_SAVE_PATH)
        else:
            logging.info("当前无爬取数据，无需保存")
    except Exception as e:
        logging.error("中断时保存数据失败：%s", str(e))
    finally:
        sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)


def get_arxiv_soup(url: str) -> Optional[BeautifulSoup]:
    """获取 arXiv 页面并返回 BeautifulSoup。"""
    try:
        response = requests.get(
            url=url,
            headers=ARXIV_HEADERS,
            proxies=None,
            timeout=30,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        time.sleep(REQUEST_INTERVAL)
        return soup
    except Exception as e:
        logging.error("arXiv 页面请求失败（%s）：%s", url, str(e))
        return None


def parse_score_from_summary(summary: str) -> int:
    """从 LLM 文本中尽可能稳健地解析 1~5 分。"""
    if not summary:
        return 0

    for pattern in (LLM_SCORE_PATTERN, LLM_SCORE_FALLBACK_PATTERN):
        match = pattern.search(summary)
        if match:
            try:
                score = int(match.group(1))
                if 1 <= score <= 5:
                    return score
            except (TypeError, ValueError):
                pass
    return 0


def normalize_arxiv_id(value: str) -> str:
    """将 arXiv ID 统一为不带版本号、不带 .pdf 的形式。"""
    if not value:
        return ""

    value = value.strip()
    match = ARXIV_ID_PATTERN.search(value)
    if match:
        value = match.group(1)

    value = value.replace(".pdf", "")
    value = re.sub(r"v\d+$", "", value)
    return value


def extract_arxiv_id_from_dt(dt_tag: BeautifulSoup) -> str:
    """优先从 Abstract 链接提取 arXiv ID。"""
    abs_tag = dt_tag.find("a", title="Abstract")
    if abs_tag and abs_tag.get("href"):
        return normalize_arxiv_id(abs_tag["href"])

    # fallback：找 /abs/、/html/ 或 /pdf/
    for a in dt_tag.find_all("a", href=True):
        href = a["href"]
        if "/abs/" in href or "/html/" in href or "/pdf/" in href:
            arxiv_id = normalize_arxiv_id(href)
            if arxiv_id:
                return arxiv_id
    return ""


def paper_identity(paper: Dict) -> str:
    """历史数据兼容：优先 arxiv_id，否则从链接提取。"""
    arxiv_id = normalize_arxiv_id(str(paper.get("arxiv_id", "")))
    if arxiv_id:
        return arxiv_id

    for key in ("arxiv_abs_link", "arxiv_html_link", "pdf_link"):
        arxiv_id = normalize_arxiv_id(str(paper.get(key, "")))
        if arxiv_id:
            return arxiv_id

    return ""


def summary_is_successful(paper: Dict) -> bool:
    summary = str(paper.get("llm_summary", "") or "").strip()
    if not summary:
        return False
    if summary == "大模型总结失败":
        return False
    return True


def repair_historical_scores() -> int:
    """
    修复历史 JSON 中：
    llm_summary 明明写了 1~5 分，但 llm_score 因旧正则失败而为 0 的记录。
    """
    fixed = 0
    for papers in all_papers_global.values():
        for paper in papers:
            old_score = paper.get("llm_score", 0)
            try:
                old_score_int = int(old_score)
            except (TypeError, ValueError):
                old_score_int = 0

            if 1 <= old_score_int <= 5:
                paper["llm_score"] = old_score_int
                continue

            score = parse_score_from_summary(
                str(paper.get("llm_summary", "") or "")
            )
            if score:
                paper["llm_score"] = score
                fixed += 1

            # 顺便补 arxiv_id，兼容旧数据
            if not paper.get("arxiv_id"):
                arxiv_id = paper_identity(paper)
                if arxiv_id:
                    paper["arxiv_id"] = arxiv_id

    return fixed


def find_existing_paper(arxiv_id: str) -> Optional[Tuple[str, int, Dict]]:
    """在所有历史日期中按 arXiv ID 查找论文。"""
    if not arxiv_id:
        return None

    for date_key, papers in all_papers_global.items():
        for idx, paper in enumerate(papers):
            if paper_identity(paper) == arxiv_id:
                return date_key, idx, paper
    return None


def remove_duplicate_identity(arxiv_id: str, keep_date: str, keep_index: int) -> int:
    """删除同一 arXiv ID 的其它重复记录。"""
    removed = 0

    for date_key in list(all_papers_global.keys()):
        papers = all_papers_global[date_key]
        new_papers = []

        for idx, paper in enumerate(papers):
            is_keep = (date_key == keep_date and idx == keep_index)
            if not is_keep and paper_identity(paper) == arxiv_id:
                removed += 1
                continue
            new_papers.append(paper)

        all_papers_global[date_key] = new_papers

    return removed


def upsert_paper(target_date: str, paper_data: Dict) -> None:
    """
    同一 arXiv ID 只保留一条。
    若历史失败后重跑成功，则更新原论文并移动到 arXiv 实际日期，
    不会 append 到 crawler 运行日期。
    """
    arxiv_id = paper_identity(paper_data)
    all_papers_global.setdefault(target_date, [])

    existing = find_existing_paper(arxiv_id) if arxiv_id else None

    if existing is None:
        all_papers_global[target_date].append(paper_data)
        return

    old_date, old_idx, _ = existing

    if old_date == target_date:
        all_papers_global[old_date][old_idx] = paper_data
        keep_index = old_idx
    else:
        del all_papers_global[old_date][old_idx]
        all_papers_global[target_date].append(paper_data)
        keep_index = len(all_papers_global[target_date]) - 1

    if arxiv_id:
        remove_duplicate_identity(arxiv_id, target_date, keep_index)


# -------------------------- arXiv 日期与分页 --------------------------

def parse_arxiv_heading_date(text: str) -> Optional[str]:
    """
    例如：
    Fri, 18 Sep 2026 (showing first 100 of 133 entries)
    -> 2026-09-18
    """
    match = ARXIV_DATE_PATTERN.search(text.strip())
    if not match:
        return None

    try:
        dt = datetime.strptime(match.group(1), "%a, %d %b %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_latest_batch_info(soup: BeautifulSoup) -> Tuple[str, int]:
    """
    从 recent 页第一个日期标题读取：
      1) arXiv 最新批次日期
      2) 该日期总 entry 数

    例如：
      Fri, 18 Sep 2026 (showing first 100 of 133 entries)
    """
    h3_list = soup.find_all("h3")
    for h3 in h3_list:
        text = h3.get_text(" ", strip=True)
        arxiv_date = parse_arxiv_heading_date(text)
        if not arxiv_date:
            continue

        total_match = ARXIV_TOTAL_PATTERN.search(text)
        if total_match:
            return arxiv_date, int(total_match.group(1))

        total_match = ARXIV_SIMPLE_TOTAL_PATTERN.search(text)
        if total_match:
            return arxiv_date, int(total_match.group(1))

        # 如果标题里没明确总数，先退化为当前页文章数
        pairs = extract_article_pairs(soup)
        return arxiv_date, len(pairs)

    raise RuntimeError("无法从 arXiv recent 页面解析最新论文日期")


def build_recent_page_url(skip: int) -> str:
    """构造 arXiv recent 分页 URL。"""
    return (
        "https://arxiv.org/list/cs.RO/recent"
        f"?skip={skip}&show={PAPERS_PER_PAGE}"
    )


def extract_article_pairs(soup: BeautifulSoup) -> List[Tuple[BeautifulSoup, BeautifulSoup]]:
    """
    提取页面中的 (dt, dd)。
    兼容 arXiv 页面使用一个或多个 dl#articles 的情况。
    """
    pairs: List[Tuple[BeautifulSoup, BeautifulSoup]] = []

    dls = soup.select("dl#articles")

    if not dls:
        dlpage = soup.find("div", id="dlpage")
        search_root = dlpage if dlpage else soup
        dls = [
            dl for dl in search_root.find_all("dl")
            if dl.find("dt") is not None and dl.find("dd") is not None
        ]

    for dl in dls:
        dt_list = dl.find_all("dt", recursive=False)
        dd_list = dl.find_all("dd", recursive=False)

        # 某些 HTML parser 情况下 recursive=False 可能取不到，做 fallback
        if not dt_list:
            dt_list = dl.find_all("dt")
        if not dd_list:
            dd_list = dl.find_all("dd")

        if len(dt_list) != len(dd_list):
            min_len = min(len(dt_list), len(dd_list))
            logging.warning(
                "页面 dt/dd 数量不一致：dt=%d, dd=%d，仅处理前 %d 条",
                len(dt_list),
                len(dd_list),
                min_len,
            )
            dt_list = dt_list[:min_len]
            dd_list = dd_list[:min_len]

        pairs.extend(zip(dt_list, dd_list))

    return pairs


# -------------------------- 论文内容提取 --------------------------

def extract_abstract(soup: BeautifulSoup) -> str:
    """兼容 arXiv HTML 页面与 abs 页面。"""
    # arXiv HTML
    abstract_container = soup.find("div", class_="ltx_abstract")
    if abstract_container:
        abstract_p = abstract_container.find("p", class_="ltx_p")
        if abstract_p:
            return (
                abstract_p.get_text(" ", strip=True)
                .replace("\xa0", " ")
                .strip()
            )

    # arXiv abs 页面
    abstract_block = soup.find("blockquote", class_=lambda c: c and "abstract" in c)
    if abstract_block:
        text = abstract_block.get_text(" ", strip=True).replace("\xa0", " ")
        text = re.sub(r"^\s*Abstract:\s*", "", text, flags=re.IGNORECASE)
        return text.strip()

    logging.warning("摘要容器缺失")
    return "未获取到摘要"


def extract_introduction(soup: BeautifulSoup) -> str:
    """从 arXiv HTML 中提取 S1。abs 页面会返回未获取到引言。"""
    intro_section = soup.find("section", id="S1")
    if not intro_section:
        return "未获取到引言"

    for tag in intro_section.find_all(["div", "button"], class_=["ltx_pagination", "sr-only button"]):
        tag.decompose()

    contents = []

    for para_div in intro_section.find_all("div", class_="ltx_para"):
        para_p = para_div.find("p", class_="ltx_p")
        if para_p:
            contents.append(
                para_p.get_text(" ", strip=True).replace("\xa0", " ")
            )

    for ul in intro_section.find_all("ul", class_="ltx_itemize"):
        for idx, li in enumerate(ul.find_all("li", class_="ltx_item"), 1):
            para_div = li.find("div", class_="ltx_para")
            li_p = para_div.find("p", class_="ltx_p") if para_div else None
            if li_p:
                text = li_p.get_text(" ", strip=True).replace("\xa0", " ")
                contents.append(f"{idx}. {text}")

    return "\n\n".join(contents) if contents else "未获取到引言"


def extract_related_work(soup: BeautifulSoup) -> str:
    """从 arXiv HTML 中提取 S2。"""
    related_work_section = soup.find("section", id="S2")
    if not related_work_section:
        return "未获取到相关工作"

    for tag in related_work_section.find_all(
        ["div", "button"],
        class_=["ltx_pagination", "sr-only button"],
    ):
        tag.decompose()

    contents = []

    section_title = related_work_section.find("h2", class_="ltx_title_section")
    if section_title:
        contents.append(
            "# " + section_title.get_text(" ", strip=True).replace("\xa0", " ")
        )

    subsection_list = related_work_section.find_all(
        "section", class_="ltx_subsection"
    )

    if subsection_list:
        for subsection in subsection_list:
            sub_title = subsection.find("h3", class_="ltx_title_subsection")
            if sub_title:
                contents.append(
                    "## " + sub_title.get_text(" ", strip=True).replace("\xa0", " ")
                )

            for para_div in subsection.find_all("div", class_="ltx_para"):
                para_p = para_div.find("p", class_="ltx_p")
                if para_p:
                    contents.append(
                        para_p.get_text(" ", strip=True).replace("\xa0", " ")
                    )
    else:
        # 某些论文 S2 没有 subsection，直接提取段落
        for para_div in related_work_section.find_all("div", class_="ltx_para"):
            para_p = para_div.find("p", class_="ltx_p")
            if para_p:
                contents.append(
                    para_p.get_text(" ", strip=True).replace("\xa0", " ")
                )

    return "\n\n".join(contents) if contents else "未获取到相关工作"


def process_comment_and_code(comment_tag: BeautifulSoup) -> Tuple[str, str]:
    """处理 comments 与其中的外部链接。"""
    if not comment_tag:
        return "", ""

    urls = []
    seen = set()

    for tag in comment_tag.find_all("a", href=True):
        url = tag["href"].strip()
        if url.startswith("/"):
            url = urljoin("https://arxiv.org", url)

        if url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            urls.append(url)

    code = ", ".join(urls)

    raw_text = (
        comment_tag.get_text(" ", strip=True)
        .replace("Comments:", "")
        .strip()
    )
    clean_comment = URL_PATTERN.sub("", raw_text).strip()
    clean_comment = re.sub(r"[,; ]+$", "", clean_comment)

    if clean_comment:
        clean_comment = PAGE_FIGURE_FRAGMENT_PATTERN.sub("", clean_comment)
        clean_comment = re.sub(r"\s*,\s*", ", ", clean_comment)
        clean_comment = re.sub(r"^[,; ]+|[,:; ]+$", "", clean_comment)
        clean_comment = clean_comment.strip()

    return clean_comment, code


def extract_pdf_link(dt_tag: BeautifulSoup) -> str:
    """从 dt 中提取 PDF 链接。"""
    for a_tag in dt_tag.find_all("a", href=True):
        href = a_tag["href"].strip()
        if PDF_LINK_PATTERN.search(href):
            return urljoin("https://arxiv.org", href)
    return ""


def extract_links(dt_tag: BeautifulSoup) -> Tuple[str, str, str]:
    """返回 abs_link, html_link, pdf_link。"""
    abs_link = ""
    html_link = ""

    abs_tag = dt_tag.find("a", title="Abstract")
    if abs_tag and abs_tag.get("href"):
        abs_link = urljoin("https://arxiv.org", abs_tag["href"].strip())

    html_tag = dt_tag.find("a", title="View HTML")
    if html_tag and html_tag.get("href"):
        html_link = urljoin("https://arxiv.org", html_tag["href"].strip())

    pdf_link = extract_pdf_link(dt_tag)
    return abs_link, html_link, pdf_link


def extract_list_metadata(dd: BeautifulSoup) -> Tuple[str, str, str, str, str]:
    """从 arXiv recent 页 dd 中提取基本元数据。"""
    meta_div = dd.find("div", class_="meta") or dd

    title_tag = meta_div.find("div", class_="list-title")
    title = (
        title_tag.get_text(" ", strip=True).replace("Title:", "").strip()
        if title_tag else "未知标题"
    )

    authors_tag = meta_div.find("div", class_="list-authors")
    authors = (
        authors_tag.get_text(" ", strip=True).replace("Authors:", "").strip()
        if authors_tag else "未知作者"
    )

    subjects_tag = meta_div.find("div", class_="list-subjects")
    subjects = (
        subjects_tag.get_text(" ", strip=True).replace("Subjects:", "").strip()
        if subjects_tag else "未知学科"
    )

    comment_tag = meta_div.find("div", class_="list-comments")
    comment, code = process_comment_and_code(comment_tag)

    return title, authors, subjects, comment, code


# -------------------------- LLM --------------------------

def _openrouter_headers() -> Dict[str, str]:
    """统一构造 OpenRouter 请求头。"""
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    if OPENROUTER_HTTP_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_HTTP_REFERER
    if OPENROUTER_APP_TITLE:
        headers["X-OpenRouter-Title"] = OPENROUTER_APP_TITLE
    return headers


def _model_is_free_chat(model: Dict) -> bool:
    """只保留 :free 且可 text-in/text-out 的生成模型，排除 embedding 等专用模型。"""
    model_id = str(model.get("id", "") or "")
    name = str(model.get("name", "") or "")
    if not model_id.endswith(":free"):
        return False

    architecture = model.get("architecture") or {}
    if not isinstance(architecture, dict):
        architecture = {}

    input_modalities = set(architecture.get("input_modalities") or [])
    output_modalities = set(architecture.get("output_modalities") or [])

    if input_modalities and "text" not in input_modalities:
        return False
    if output_modalities and "text" not in output_modalities:
        return False

    # API 元数据偶尔不完整，再用名字兜底排除非 chat 类模型。
    label = f"{model_id} {name}".lower()
    excluded = (
        "embed", "embedding", "rerank", "transcrib",
        "text-to-speech", "speech-to-text", "tts", "moderation",
    )
    return not any(word in label for word in excluded)


def _free_model_sort_key(model: Dict) -> Tuple[int, int, str]:
    """优先 Qwen3.8；其余优先上下文更大的模型，保证顺序稳定。"""
    model_id = str(model.get("id", "") or "")
    try:
        context_length = int(model.get("context_length") or 0)
    except (TypeError, ValueError):
        context_length = 0
    preferred = 0 if model_id == PREFERRED_LLM_MODEL else 1
    return preferred, -context_length, model_id


def _list_free_chat_models() -> List[Dict]:
    """从 OpenRouter 实时模型目录获取 free chat 模型。GET 目录本身不做推理。"""
    response = requests.get(
        OPENROUTER_MODELS_URL,
        headers=_openrouter_headers(),
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()
    models = payload.get("data", []) if isinstance(payload, dict) else []
    candidates = [m for m in models if isinstance(m, dict) and _model_is_free_chat(m)]
    candidates.sort(key=_free_model_sort_key)
    return candidates


def _probe_free_model(model_id: str) -> Tuple[str, str]:
    """
    用极小请求探测一个 free 模型。

    返回 (state, detail)：state 为 ok / skip / fatal。
    shared-pool 429 直接 skip，不在启动阶段原地等待。
    """
    _wait_for_llm_slot()
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Reply with exactly OK."}],
        "temperature": 0,
        "max_tokens": 8,
    }

    try:
        response = requests.post(
            LLM_API_URL,
            headers=_openrouter_headers(),
            json=payload,
            timeout=45,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        return "skip", f"network error: {exc}"

    if response.status_code == 200:
        try:
            data = response.json()
        except ValueError:
            return "skip", "HTTP 200 but invalid JSON"
        if _extract_message_text(data):
            return "ok", "HTTP 200"
        return "skip", "HTTP 200 but empty content"

    error = _openrouter_error(response)
    if _is_shared_pool_429(response, error):
        provider = error["provider"] or "unknown"
        return "skip", f"upstream shared-pool 429 ({provider})"

    if response.status_code in (401, 403):
        return "fatal", f"HTTP {response.status_code}: {response.text}"

    if _is_account_quota_429(response, error):
        return "fatal", f"account quota 429: {response.text}"

    detail = error["raw"] or error["message"] or response.text
    return "skip", f"HTTP {response.status_code}: {detail}"


def select_available_free_model() -> Optional[str]:
    """启动时枚举 free chat 模型，依次探测，选择第一个实际可返回文本的模型。"""
    global LLM_MODEL
    global llm_quota_exhausted

    try:
        candidates = _list_free_chat_models()
    except Exception as exc:
        logging.exception("获取 OpenRouter free 模型列表失败：%s", exc)
        return None

    if not candidates:
        logging.error("OpenRouter 模型目录中没有找到可用的 :free text chat 模型")
        return None

    logging.info(
        "启动模型检测：发现 %d 个 free text chat 模型（已排除 embedding/rerank 等）",
        len(candidates),
    )

    for index, model in enumerate(candidates, 1):
        model_id = str(model.get("id", ""))
        logging.info("[%d/%d] 探测 free 模型：%s", index, len(candidates), model_id)

        state, detail = _probe_free_model(model_id)
        if state == "ok":
            LLM_MODEL = model_id
            logging.info("✓ 选定可用 free 模型：%s (%s)", model_id, detail)
            return model_id

        if state == "fatal":
            if "quota" in detail.lower():
                llm_quota_exhausted = True
            logging.error("模型探测遇到不可继续错误：%s", detail)
            return None

        logging.warning("✗ 跳过 %s：%s", model_id, detail)

    logging.error("已遍历全部 free text chat 模型，没有找到当前可正常返回文本的模型")
    return None


def _llm_result(summary: str = "大模型总结失败", score: int = 0,
                error: str = "", quota_exhausted: bool = False) -> Dict:
    """统一构造 LLM 返回值，避免每个分支重复堆字典。"""
    return {
        "summary": summary,
        "score": score,
        "error": error,
        "quota_exhausted": quota_exhausted,
    }


def _wait_for_llm_slot() -> None:
    """限制相邻 LLM 请求间隔。"""
    global _last_llm_request_at

    elapsed = time.monotonic() - _last_llm_request_at
    if _last_llm_request_at and elapsed < LLM_REQUEST_INTERVAL:
        time.sleep(LLM_REQUEST_INTERVAL - elapsed)

    _last_llm_request_at = time.monotonic()


def _openrouter_error(response: requests.Response) -> Dict[str, str]:
    """提取 OpenRouter 错误字段；解析失败时安全返回空字符串。"""
    try:
        payload = response.json()
    except ValueError:
        payload = {}

    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    error = error if isinstance(error, dict) else {}

    metadata = error.get("metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}

    return {
        "message": str(error.get("message", "") or ""),
        "raw": str(metadata.get("raw", "") or ""),
        "provider": str(metadata.get("provider_name", "") or ""),
        "provider_error_code": str(metadata.get("provider_error_code", "") or ""),
        "limit_source": str(metadata.get("limit_source", "") or ""),
    }


def _is_shared_pool_429(response: requests.Response, error: Dict[str, str]) -> bool:
    return (
        response.status_code == 429
        and error["limit_source"] == "upstream_provider_shared_pool"
    )


def _is_account_quota_429(response: requests.Response, error: Dict[str, str]) -> bool:
    """只把明确的账号/日额度耗尽视为全局 quota exhausted。"""
    if response.status_code != 429 or _is_shared_pool_429(response, error):
        return False

    text = f'{error["message"]} {error["raw"]}'.lower()
    quota_markers = (
        "daily limit",
        "daily quota",
        "quota exhausted",
        "quota has been exhausted",
        "free-models-per-day",
        "requests per day",
    )
    return any(marker in text for marker in quota_markers)


def _retry_delay(response: requests.Response, retry_index: int) -> float:
    """优先尊重 Retry-After，同时不低于本地退避时间。"""
    delay = float(LLM_TRANSIENT_429_BACKOFF[retry_index])
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return delay

    try:
        return max(delay, float(retry_after))
    except ValueError:
        return delay


def _format_openrouter_error(response: requests.Response,
                             error: Dict[str, str]) -> str:
    """保留完整响应，方便 GitHub Actions 里直接定位问题。"""
    return (
        "OpenRouter API 请求失败\n"
        f"status={response.status_code} {response.reason}\n"
        f"request_url={response.request.url}\n"
        f"authorization_header_sent={bool(response.request.headers.get('Authorization'))}\n"
        f"openrouter_key_loaded={bool(LLM_API_KEY)}\n"
        f"openrouter_key_length={len(LLM_API_KEY)}\n"
        f"limit_source={error['limit_source'] or 'unknown'}\n"
        f"provider_name={error['provider'] or 'unknown'}\n"
        f"provider_error_code={error['provider_error_code'] or 'unknown'}\n"
        f"response_headers={json.dumps(dict(response.headers), ensure_ascii=False)}\n"
        f"response_body={response.text}"
    )


def _extract_message_text(data: Dict) -> str:
    """从 OpenRouter/OpenAI 兼容响应中提取最终文本；空响应返回空字符串。"""
    try:
        content = data["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""

    if isinstance(content, str):
        return content.strip()

    # 兼容部分 provider 返回 typed content blocks。
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()

    return ""


def _post_openrouter(payload: Dict, title: str) -> requests.Response:
    """
    发起 OpenRouter 请求。

    可恢复异常（共享池 429、HTTP 200 但正文为空）会退避重试；
    重试耗尽后只失败当前论文，不影响后续论文。
    """
    headers = _openrouter_headers()
    max_attempts = len(LLM_TRANSIENT_429_BACKOFF) + 1

    for attempt in range(max_attempts):
        _wait_for_llm_slot()
        logging.info(
            ">>> LLM 请求 %d/%d | model=%s | %s",
            attempt + 1,
            max_attempts,
            LLM_MODEL,
            title[:100],
        )

        response = requests.post(
            LLM_API_URL,
            headers=headers,
            json=payload,
            timeout=180,
            allow_redirects=False,
        )

        # 有些免费 provider 会返回 HTTP 200，但 choices[0].message.content=null。
        # 对 paper-daily 来说这不是成功，按临时上游异常重试。
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError:
                data = {}

            if _extract_message_text(data):
                return response

            if attempt == max_attempts - 1:
                logging.warning(
                    "<<< LLM 200 但正文为空 | 重试耗尽，放弃当前论文 | %s\n%s",
                    title[:100],
                    response.text,
                )
                return response

            delay = float(LLM_TRANSIENT_429_BACKOFF[attempt])
            logging.warning(
                "<<< LLM 200 但正文为空 | %.0fs 后重试 | %s\n%s",
                delay,
                title[:100],
                response.text,
            )
            time.sleep(delay)
            continue

        if response.status_code != 429:
            return response

        error = _openrouter_error(response)
        if not _is_shared_pool_429(response, error):
            return response

        if attempt == max_attempts - 1:
            logging.warning(
                "<<< LLM 429 | 上游共享池连续失败，放弃当前论文 | %s",
                title[:100],
            )
            return response

        delay = _retry_delay(response, attempt)
        logging.warning(
            "<<< LLM 429 | upstream shared pool | provider=%s | %.0fs 后重试\n%s",
            error["provider"] or "unknown",
            delay,
            response.text,
        )
        time.sleep(delay)

    raise RuntimeError("OpenRouter 重试流程异常结束")


def call_llm_for_summary(
    title: str,
    abstract: str,
    introduction: str,
    relate_work: str,
) -> Dict:
    """通过 OpenRouter 调用 LLM，并解析 1~5 分相关性评分。"""
    global llm_quota_exhausted

    if llm_quota_exhausted:
        return _llm_result(
            error="本次运行已确认账号级/日额度耗尽，停止继续请求",
            quota_exhausted=True,
        )

    if not LLM_API_KEY:
        error_msg = (
            "未读取到 OpenRouter API Key。请设置 OPENROUTER_API_KEY "
            "或兼容变量 LLM_API_KEY。"
        )
        logging.error(error_msg)
        return _llm_result(error=error_msg)

    system_prompt = LLM_PROMPT or (
        "请总结论文，并在开头使用固定格式“【相关性】分数：5分”，"
        "其中分数必须为1到5的整数。"
    )
    user_prompt = (
        f"标题：{title}\n"
        f"摘要：{abstract}\n"
        f"引言：{introduction}\n"
        f"相关工作：{relate_work}"
    )
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 500,
    }

    try:
        response = _post_openrouter(payload, title)

        if response.status_code == 200:
            data = response.json()
            summary = _extract_message_text(data)
            if not summary:
                error_msg = (
                    "OpenRouter 返回 HTTP 200，但最终正文为空。\n"
                    f"response_body={response.text}"
                )
                logging.error("大模型调用失败：\n%s", error_msg)
                return _llm_result(error=error_msg)

            score = parse_score_from_summary(summary)

            logging.info(
                "<<< LLM 成功 | score=%s | chars=%d | %s\n%s",
                score or "未解析",
                len(summary),
                title[:100],
                summary,
            )
            return _llm_result(summary=summary, score=score)

        error = _openrouter_error(response)
        if _is_account_quota_429(response, error):
            llm_quota_exhausted = True

        error_msg = _format_openrouter_error(response, error)
        logging.error("大模型调用失败：\n%s", error_msg)
        return _llm_result(
            error=error_msg,
            quota_exhausted=llm_quota_exhausted,
        )

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        logging.exception("大模型调用异常：%s", error_msg)
        return _llm_result(
            error=error_msg,
            quota_exhausted=llm_quota_exhausted,
        )


# -------------------------- Markdown 输出 --------------------------

def get_latest_data_dates(date_papers: Dict[str, List[Dict]], limit: int) -> List[str]:
    """
    不再用“今天往前数 N 个日历日”。
    直接取 JSON 中最近 N 个有数据的 arXiv 日期，周末也不会少显示。
    """
    valid = [
        date for date, papers in date_papers.items()
        if papers and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)
    ]
    valid.sort(reverse=True)
    return valid[:limit]


def get_first_author(authors_str: str) -> str:
    if not authors_str:
        return "未知作者"
    first_author = authors_str.split(",")[0].strip()
    return first_author if first_author else "未知作者"


def json_to_markdown(json_path: str, md_path: str) -> None:
    """生成 README Markdown。"""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            date_papers = json.load(f)
    except Exception as e:
        logging.error("读取 JSON 失败：%s", str(e))
        return

    if not date_papers:
        logging.warning("JSON 中无论文数据，无法生成 Markdown")
        return

    valid_dates = get_latest_data_dates(
        date_papers,
        RECENT_DISPLAY_DAYS,
    )

    if not valid_dates:
        logging.warning("无有效论文数据，无法生成 Markdown")
        return

    latest_valid_date = valid_dates[0]
    total_papers = sum(len(date_papers[d]) for d in valid_dates)

    md_title = f"# arXiv Robot 领域论文汇总（共{total_papers}篇）"
    md_intro = (
        "> 说明：仅显示最近五个有数据的 arXiv 日期；最新日期默认展开。\n"
        "> 相关性评分：基于LLM对机器人领域的相关性评定"
        "（1-5分，★越多相关性越高）\n\n"
    )

    nav_links = []
    for date in valid_dates:
        paper_count = len(date_papers[date])
        anchor_id = f"date-{date.replace('-', '')}"
        nav_links.append(
            f"- [{date}（{paper_count}篇论文）](#{anchor_id})"
        )

    md_nav = "## 日期导航\n" + "\n".join(nav_links) + "\n\n"

    md_table_header = """| Title | Author | Comment | PDF | Code | Relevance | Summary |
|----------|----|---|---|---|---|----------|"""

    date_sections = []

    for date in valid_dates:
        papers = date_papers[date]

        def effective_score(p):
            score = p.get("llm_score", 0)
            try:
                score = int(score)
            except (TypeError, ValueError):
                score = 0

            if not 1 <= score <= 5:
                score = parse_score_from_summary(
                    str(p.get("llm_summary", "") or "")
                )
            return score

        sorted_papers = sorted(
            papers,
            key=effective_score,
            reverse=True,
        )

        rows = []

        for paper in sorted_papers:
            title = (
                str(paper.get("title", "未知标题"))
                .replace("|", "\\|")
                .replace("\n", " ")
            )

            first_author = get_first_author(
                str(paper.get("authors", "未知作者"))
            )

            comment = (
                str(paper.get("comment", ""))
                .replace("|", "\\|")
                .replace("\n", "<br>")
            )
            comment_html = (
                f"<details><summary>detail</summary>{comment}</details>"
                if comment else ""
            )

            pdf_link = str(paper.get("pdf_link", ""))
            pdf_html = f"[PDF]({pdf_link})" if pdf_link else "-"

            code = str(paper.get("code", ""))
            if code:
                code_list = [
                    url.strip()
                    for url in code.split(",")
                    if url.strip()
                ]
                code_html = "<br>".join(
                    f"[code{i + 1}]({url})"
                    for i, url in enumerate(code_list)
                )
            else:
                code_html = "-"

            score = effective_score(paper)
            score_html = (
                "★" * score + "☆" * (5 - score)
                if 1 <= score <= 5 else "-"
            )

            llm_summary = (
                str(paper.get("llm_summary", "无"))
                .replace("|", "\\|")
                .replace("\n", "<br>")
            )
            llm_html = (
                f"<details><summary>总结</summary>{llm_summary}</details>"
                if llm_summary else "无"
            )

            rows.append(
                f"| {title} | {first_author} | {comment_html} | "
                f"{pdf_html} | {code_html} | {score_html} | {llm_html} |"
            )

        anchor_id = f"date-{date.replace('-', '')}"
        date_display = f"{date}（{len(papers)}篇论文）"

        if date == latest_valid_date:
            section = (
                f"## <a id='{anchor_id}'></a>{date_display}\n\n"
                f"{md_table_header}\n"
                + "\n".join(rows)
                + "\n"
            )
        else:
            section = (
                "<details>\n"
                f"<summary><a id='{anchor_id}'></a>{date_display}</summary>\n\n"
                f"{md_table_header}\n"
                + "\n".join(rows)
                + "\n\n</details>\n"
            )

        date_sections.append(section)

    md_content = (
        f"{md_title}\n\n"
        f"{md_intro}"
        f"{md_nav}"
        + "\n".join(date_sections)
    )

    try:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        logging.info("Markdown 已保存至：%s", md_path)
    except Exception as e:
        logging.error("保存 Markdown 失败：%s", str(e))


# -------------------------- 单篇处理 --------------------------

def build_paper_data(
    dt: BeautifulSoup,
    dd: BeautifulSoup,
    target_date: str,
) -> Optional[Dict]:
    """
    构建一篇论文的数据。
    已成功总结过的论文由上层直接跳过；这里主要处理新论文或失败重试。
    """
    arxiv_id = extract_arxiv_id_from_dt(dt)
    if not arxiv_id:
        logging.warning("无法提取 arXiv ID，跳过该条目")
        return None

    title, authors, subjects, comment, code = extract_list_metadata(dd)
    abs_link, html_link, pdf_link = extract_links(dt)

    # 没有 abs link 时自行构造
    if not abs_link:
        abs_link = f"https://arxiv.org/abs/{arxiv_id}"

    # 没有 pdf link 时自行构造
    if not pdf_link:
        pdf_link = f"https://arxiv.org/pdf/{arxiv_id}"

    content_soup = None

    # 优先 HTML：可以拿 abstract + introduction + related work
    if html_link:
        content_soup = get_arxiv_soup(html_link)

    # HTML 不存在或请求失败，则 fallback 到 abs 页面，至少拿 abstract
    if content_soup is None:
        logging.info(
            "论文 %s 无可用 HTML，fallback 到 abs 页面",
            arxiv_id,
        )
        content_soup = get_arxiv_soup(abs_link)

    if content_soup is not None:
        abstract = extract_abstract(content_soup)
        introduction = extract_introduction(content_soup)
        relate_work = extract_related_work(content_soup)
    else:
        abstract = "未获取到摘要"
        introduction = "未获取到引言"
        relate_work = "未获取到相关工作"

    llm_result = call_llm_for_summary(
        title,
        abstract,
        introduction,
        relate_work,
    )

    return {
        "arxiv_id": arxiv_id,
        "arxiv_date": target_date,
        "crawl_datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "title": title,
        "authors": authors,
        "subjects": subjects,
        "comment": comment,
        "pdf_link": pdf_link,
        "code": code,
        "arxiv_abs_link": abs_link,
        "arxiv_html_link": html_link,
        "abstract": abstract,
        "introduction": introduction,
        "related_work": relate_work,
        "llm_summary": llm_result["summary"],
        "llm_score": llm_result["score"],
        "llm_error": llm_result["error"],
    }


def refresh_existing_metadata(
    existing_paper: Dict,
    dt: BeautifulSoup,
    dd: BeautifulSoup,
    target_date: str,
) -> Dict:
    """
    已成功总结过的论文无需重新调用 LLM，
    但可以刷新标题/作者/资源链接，并确保 arxiv_date 正确。
    """
    paper = dict(existing_paper)

    arxiv_id = extract_arxiv_id_from_dt(dt) or paper_identity(existing_paper)
    title, authors, subjects, comment, code = extract_list_metadata(dd)
    abs_link, html_link, pdf_link = extract_links(dt)

    paper["arxiv_id"] = arxiv_id
    paper["arxiv_date"] = target_date

    if title != "未知标题":
        paper["title"] = title
    if authors != "未知作者":
        paper["authors"] = authors
    if subjects != "未知学科":
        paper["subjects"] = subjects

    paper["comment"] = comment
    paper["code"] = code

    if abs_link:
        paper["arxiv_abs_link"] = abs_link
    if html_link:
        paper["arxiv_html_link"] = html_link
    if pdf_link:
        paper["pdf_link"] = pdf_link

    # 旧记录 score=0 时直接从 summary 修复
    parsed_score = parse_score_from_summary(
        str(paper.get("llm_summary", "") or "")
    )
    if parsed_score:
        paper["llm_score"] = parsed_score

    return paper



# -------------------------- 失败论文补跑 --------------------------

def retry_failed_summaries_after_latest(
    latest_date: str,
) -> int:
    """
    只有“最新 arXiv 批次已经全部扫描完”之后，才使用剩余额度补跑失败论文。

    优先级：
      1. 最新日期中仍失败的论文
      2. 更早日期的失败论文，按日期从新到旧
      3. 同一天保持 JSON 中原有顺序

    重要：
    - 本函数不会在最新论文处理之前运行。
    - 一旦检测到 429（llm_quota_exhausted=True），立即停止补跑。
    - 补跑直接使用 JSON 中已经保存的 title / abstract /
      introduction / related_work，不再重新请求 arXiv 页面。
    """
    global llm_quota_exhausted

    if llm_quota_exhausted:
        logging.info(
            "最新批次处理阶段已经耗尽 LLM 额度，"
            "本次不补跑历史失败论文"
        )
        return 0

    # 日期从新到旧，因此永远优先补最近的失败论文。
    sorted_dates = sorted(
        all_papers_global.keys(),
        reverse=True,
    )

    # latest_date 正常情况下本身就是最大日期。
    # 这里显式把它放在最前，避免异常历史 key 干扰优先级。
    if latest_date in sorted_dates:
        sorted_dates.remove(latest_date)
        sorted_dates.insert(0, latest_date)

    candidates = []

    for date_key in sorted_dates:
        papers = all_papers_global.get(date_key, [])

        for idx, paper in enumerate(papers):
            if summary_is_successful(paper):
                continue

            candidates.append(
                (date_key, idx, paper)
            )

    if not candidates:
        logging.info(
            "最新批次处理完成，当前没有需要补跑的失败论文"
        )
        return 0

    logging.info("=" * 60)
    logging.info(
        "最新批次 %s 已全部扫描完成；"
        "开始使用剩余 LLM 额度补跑失败论文，共 %d 条",
        latest_date,
        len(candidates),
    )
    logging.info(
        "补跑顺序：最新日期优先，同日期保持原始论文顺序"
    )
    logging.info("=" * 60)

    success_count = 0

    for date_key, idx, paper in candidates:
        if llm_quota_exhausted:
            logging.info(
                "补跑阶段检测到 LLM 额度耗尽，立即停止"
            )
            break

        title = str(
            paper.get("title", "未知标题") or "未知标题"
        )
        abstract = str(
            paper.get("abstract", "未获取到摘要")
            or "未获取到摘要"
        )
        introduction = str(
            paper.get("introduction", "未获取到引言")
            or "未获取到引言"
        )
        related_work = str(
            paper.get("related_work", "未获取到相关工作")
            or "未获取到相关工作"
        )

        arxiv_id = paper_identity(paper)

        logging.info(
            "补跑失败论文：date=%s, arXiv=%s, title=%s",
            date_key,
            arxiv_id or "unknown",
            title[:100],
        )

        llm_result = call_llm_for_summary(
            title,
            abstract,
            introduction,
            related_work,
        )

        # 429：call_llm_for_summary 已设置全局标志。
        # 不覆盖原始内容之外的字段，保留历史 metadata。
        paper["llm_summary"] = llm_result["summary"]
        paper["llm_score"] = llm_result["score"]
        paper["llm_error"] = llm_result["error"]
        paper["last_retry_datetime"] = (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

        all_papers_global[date_key][idx] = paper

        # 每次补跑后立即保存，避免运行被中断时丢失已成功的结果。
        save_json()

        if summary_is_successful(paper):
            success_count += 1
            logging.info(
                "补跑成功：%s，score=%s",
                arxiv_id or title[:80],
                paper.get("llm_score", 0),
            )
        else:
            logging.warning(
                "补跑仍失败：%s；error=%s",
                arxiv_id or title[:80],
                paper.get("llm_error", ""),
            )

        if llm_quota_exhausted:
            logging.warning(
                "补跑时遇到 429，停止继续消耗请求；"
                "剩余失败论文留到下次运行"
            )
            break

    logging.info(
        "失败论文补跑结束：本次成功补回 %d 条",
        success_count,
    )

    return success_count



# -------------------------- 核心爬虫 --------------------------

def crawl_and_process_papers(
    initial_url: str,
    max_pages: Optional[int] = None,
) -> Dict[str, List[Dict]]:
    """
    只处理 arXiv recent 页“最新日期”的整批论文。

    关键点：
    1. 日期来自 arXiv 页面 h3，而不是 datetime.now()。
    2. 总论文数来自 h3，例如 133 entries。
    3. 自动使用 skip=100、200... 翻页。
    4. 当天 133 篇就只处理前 133 条，不会把下一天混进来。
    5. 失败重试按 arXiv ID 原地更新，不会 append 到 crawler 日期。
    """
    global all_papers_global
    global llm_quota_exhausted

    llm_quota_exhausted = False

    # 1. 载入历史数据
    try:
        with open(JSON_SAVE_PATH, "r", encoding="utf-8") as f:
            all_papers_global = json.load(f)
        logging.info(
            "已加载历史数据，包含 %d 个日期",
            len(all_papers_global),
        )
    except FileNotFoundError:
        logging.info("无历史 JSON，将创建新文件")
        all_papers_global = {}
    except Exception as e:
        logging.warning(
            "加载历史数据失败：%s，将从空数据开始",
            str(e),
        )
        all_papers_global = {}

    repaired = repair_historical_scores()
    if repaired:
        logging.info("已自动修复历史 llm_score：%d 条", repaired)
        save_json()

    # 2. 第一页：确定最新 arXiv 批次日期和总数
    first_soup = get_arxiv_soup(initial_url)
    if first_soup is None:
        raise RuntimeError("无法获取 arXiv recent 第一页")

    target_date, target_total = parse_latest_batch_info(first_soup)

    if target_total <= 0:
        logging.warning("arXiv 最新批次 %s 没有论文", target_date)
        return all_papers_global

    needed_pages = math.ceil(target_total / PAPERS_PER_PAGE)

    if max_pages is not None:
        actual_pages = min(needed_pages, max_pages)
    else:
        actual_pages = needed_pages

    logging.info("=" * 60)
    logging.info("arXiv 最新批次日期：%s", target_date)
    logging.info("该日期 arXiv entries：%d", target_total)
    logging.info("每页：%d", PAPERS_PER_PAGE)
    logging.info("理论所需页数：%d", needed_pages)
    logging.info("本次实际最多抓取页数：%d", actual_pages)
    logging.info("=" * 60)

    if actual_pages < needed_pages:
        logging.warning(
            "MAX_CRAWL_PAGES=%d 导致无法抓完整 %s："
            "需要 %d 页，本次仅抓 %d 页",
            max_pages,
            target_date,
            needed_pages,
            actual_pages,
        )

    all_papers_global.setdefault(target_date, [])

    processed_batch_entries = 0

    # 3. 逐页抓取，只取最新日期需要的前 target_total 条
    for page_idx in range(actual_pages):
        skip = page_idx * PAPERS_PER_PAGE

        if page_idx == 0:
            list_soup = first_soup
            page_url = initial_url
        else:
            page_url = build_recent_page_url(skip)
            list_soup = get_arxiv_soup(page_url)

        if list_soup is None:
            logging.error(
                "第 %d 页请求失败：%s",
                page_idx + 1,
                page_url,
            )
            break

        pairs = extract_article_pairs(list_soup)

        remaining = target_total - processed_batch_entries
        if remaining <= 0:
            break

        # recent 排序中最新日期一定在最前面。
        # page2 若包含 33 条最新日期 + 67 条上一日期，只取前 33 条。
        pairs = pairs[:remaining]

        logging.info(
            "=== 第 %d/%d 页：skip=%d，本页处理 %d 条 ===",
            page_idx + 1,
            actual_pages,
            skip,
            len(pairs),
        )

        if not pairs:
            logging.warning("本页未解析到论文条目，停止")
            break

        for idx, (dt, dd) in enumerate(pairs, 1):
            processed_batch_entries += 1

            arxiv_id = extract_arxiv_id_from_dt(dt)

            logging.info(
                "[%d/%d] page %d item %d: arXiv:%s",
                processed_batch_entries,
                target_total,
                page_idx + 1,
                idx,
                arxiv_id or "unknown",
            )

            if not arxiv_id:
                logging.warning("无法提取 arXiv ID，跳过")
                continue

            existing = find_existing_paper(arxiv_id)

            # 已成功总结：不重复消耗 LLM，只刷新 metadata 并确保日期正确
            if existing is not None:
                old_date, _, old_paper = existing

                if summary_is_successful(old_paper):
                    refreshed = refresh_existing_metadata(
                        old_paper,
                        dt,
                        dd,
                        target_date,
                    )
                    upsert_paper(target_date, refreshed)

                    if old_date != target_date:
                        logging.info(
                            "已将历史论文 %s 从 %s 移动到 arXiv 日期 %s",
                            arxiv_id,
                            old_date,
                            target_date,
                        )
                    else:
                        logging.info(
                            "论文已成功总结过，跳过 LLM：%s",
                            arxiv_id,
                        )
                    continue

                logging.info(
                    "检测到历史失败记录，重新处理并原地更新：%s",
                    arxiv_id,
                )

            paper_data = build_paper_data(
                dt,
                dd,
                target_date,
            )

            if paper_data is None:
                continue

            upsert_paper(target_date, paper_data)

        # 每页保存一次，避免中途失败丢数据
        save_json()
        logging.info(
            "第 %d 页处理完成并已保存 JSON",
            page_idx + 1,
        )

        if processed_batch_entries >= target_total:
            break

    # 4. 最新批次的所有页面已经扫描完。
    #    只有到这里，才允许使用剩余 LLM 额度补跑失败论文。
    #
    #    优先级严格为：
    #    最新论文 > 最新日期失败论文 > 更早日期失败论文。
    #
    #    如果最新论文阶段已经遇到 429，则这里不会再调用 LLM。
    retry_failed_summaries_after_latest(target_date)

    # 5. 最终去掉空日期 key
    all_papers_global = {
        date: papers
        for date, papers in all_papers_global.items()
        if papers
    }

    save_json()

    final_count = len(all_papers_global.get(target_date, []))

    logging.info("=" * 60)
    logging.info(
        "本次 arXiv 批次 %s：页面声明 %d entries，JSON 当前保存 %d 条",
        target_date,
        target_total,
        final_count,
    )

    if processed_batch_entries < target_total:
        logging.warning(
            "本次只扫描到 %d/%d 个最新批次条目",
            processed_batch_entries,
            target_total,
        )

    if llm_quota_exhausted:
        logging.warning(
            "本次运行确认账号级/日额度耗尽。后续论文已保留元数据并标记总结失败，"
            "下次运行会自动重试失败项。"
        )

    logging.info("=" * 60)

    return all_papers_global


# -------------------------- 程序入口 --------------------------

if __name__ == "__main__":
    if not LLM_API_KEY or LLM_API_KEY.startswith("sk-xxxx"):
        logging.error(
            "请通过环境变量 OPENROUTER_API_KEY（或兼容变量 LLM_API_KEY）"
            "配置 OpenRouter API Key"
        )
        sys.exit(1)

    logging.info("=" * 60)
    logging.info("arXiv cs.RO daily crawler")
    logging.info("LLM provider：OpenRouter")
    logging.info("开始检测当前可用的 free 模型...")

    if not select_available_free_model():
        logging.error("没有可用的 free LLM，终止本次任务，避免把论文批量写成总结失败")
        sys.exit(1)

    logging.info("LLM model：%s", LLM_MODEL)
    logging.info("初始页面：%s", INITIAL_ARXIV_URL)
    logging.info(
        "最大页数：%s",
        "自动抓完整最新批次"
        if MAX_CRAWL_PAGES is None
        else str(MAX_CRAWL_PAGES),
    )
    logging.info("JSON：%s", JSON_SAVE_PATH)
    logging.info("Markdown：%s", MD_SAVE_PATH)
    logging.info("=" * 60)

    try:
        all_papers = crawl_and_process_papers(
            initial_url=INITIAL_ARXIV_URL,
            max_pages=MAX_CRAWL_PAGES,
        )

        logging.info("=== 生成 Markdown ===")
        json_to_markdown(JSON_SAVE_PATH, MD_SAVE_PATH)

        total_count = sum(
            len(papers)
            for papers in all_papers.values()
        )

        logging.info("=" * 60)
        logging.info("任务完成")
        logging.info("日期数量：%d", len(all_papers))
        logging.info("历史论文总数：%d", total_count)
        logging.info("=" * 60)

    except Exception as e:
        error_msg = (
            f"任务运行异常：{str(e)}\n"
            f"{traceback.format_exc()}"
        )
        logging.error(error_msg)

        if all_papers_global:
            try:
                save_json()
                logging.info("异常中断前数据已保存")
            except Exception as save_e:
                logging.error(
                    "异常中断时保存失败：%s",
                    str(save_e),
                )

        sys.exit(1)
