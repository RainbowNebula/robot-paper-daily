import requests
from bs4 import BeautifulSoup
import json
import time
import signal
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import logging
import re
import http.client
import traceback


# -------------------------- 全局变量与信号处理 --------------------------
all_papers_global: Dict[str, List[Dict]] = {}  # 按日期组织的论文数据 {date: [papers]}

def signal_handler(sig, frame):
    """处理Ctrl+C信号，确保中断时保存数据"""
    logging.info("\n检测到手动中断（Ctrl+C），正在保存当前数据...")
    try:
        if all_papers_global:
            with open(JSON_SAVE_PATH, "w", encoding="utf-8") as f:
                json.dump(all_papers_global, f, ensure_ascii=False, indent=4)
            logging.info(f"已保存数据到 JSON 文件，包含 {len(all_papers_global)} 个日期的数据")
            json_to_markdown(JSON_SAVE_PATH, MD_SAVE_PATH)
        else:
            logging.info("当前无爬取数据，无需保存")
    except Exception as e:
        logging.error(f"中断时保存数据失败：{str(e)}")
    finally:
        sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)


# -------------------------- 基础配置 --------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("arxiv_crawler.log"), logging.StreamHandler()]
)

ARXIV_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.8,en-US;q=0.5,en;q=0.3",
    "Connection": "keep-alive"
}

# LLM配置
# 新代码（从环境变量获取）
import os
LLM_API_KEY = os.getenv("LLM_API_KEY")  # 从环境变量读取密钥
LLM_API_HOST = "api.chatanywhere.tech"
LLM_API_ENDPOINT = "/v1/chat/completions"
LLM_MODEL = "gpt-4o-mini"

# 爬取配置
REQUEST_INTERVAL = 1.2
PAPERS_PER_PAGE = 100
MAX_CRAWL_PAGES = 1  # 最大爬取页数，None表示无限制
INITIAL_ARXIV_URL = "https://arxiv.org/list/cs.RO/recent?show=100"  # cs.RO领域最新论文

# 存储配置
JSON_SAVE_PATH = "arxiv_cs_ro_papers_final.json"
CURRENT_DATE = datetime.now().strftime("%Y-%m-%d")
# MD_SAVE_PATH = f"{CURRENT_DATE}_papers.md"
MD_SAVE_PATH = f"README.md"

# 工具正则
URL_PATTERN = re.compile(r'https?://\S+|www\.\S+')
PDF_LINK_PATTERN = re.compile(r'pdf', re.IGNORECASE)  # 匹配含PDF的链接
LLM_SCORE_PATTERN = re.compile(r'分数：(\d+)分')  # 提取LLM返回的1-5分评分


# -------------------------- 工具函数 --------------------------
def get_arxiv_soup(url: str) -> Optional[BeautifulSoup]:
    """获取arxiv页面的BeautifulSoup对象"""
    try:
        response = requests.get(
            url=url,
            headers=ARXIV_HEADERS,
            proxies=None,
            timeout=20
        )
        response.raise_for_status()  # 触发HTTP错误（4xx/5xx）
        time.sleep(REQUEST_INTERVAL)  # 反爬间隔
        return BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        logging.error(f"arXiv 页面请求失败（{url}）：{str(e)}")
        return None


def extract_abstract(soup: BeautifulSoup) -> str:
    """提取论文摘要"""
    abstract_container = soup.find("div", class_="ltx_abstract")
    if not abstract_container:
        logging.warning("摘要容器缺失")
        return "未获取到摘要"
    abstract_p = abstract_container.find("p", class_="ltx_p")
    return abstract_p.get_text(strip=False).strip().replace("\xa0", " ") if abstract_p else "未获取到摘要"


def extract_introduction(soup: BeautifulSoup) -> str:
    """提取论文引言（S1章节）"""
    intro_section = soup.find("section", id="S1")
    if not intro_section:
        logging.warning("引言容器缺失")
        return "未获取到引言"
    # 移除分页、隐藏按钮等无关元素
    for tag in intro_section.find_all(["div", "button"], class_=["ltx_pagination", "sr-only button"]):
        tag.decompose()
    contents = []
    # 提取段落内容
    for para_div in intro_section.find_all("div", class_="ltx_para"):
        para_p = para_div.find("p", class_="ltx_p")
        if para_p:
            contents.append(para_p.get_text(strip=False).strip().replace("\xa0", " "))
    # 提取列表内容（如研究点列表）
    for ul in intro_section.find_all("ul", class_="ltx_itemize"):
        for idx, li in enumerate(ul.find_all("li", class_="ltx_item"), 1):
            li_p = li.find("div", class_="ltx_para").find("p", class_="ltx_p")
            if li_p:
                # contents.append(f"{idx}. {li_p.get_text(strip=False).strip().replace('\xa0', ' ')}")
                text = li_p.get_text(strip=False).strip().replace('\xa0', ' ')
                contents.append(f"{idx}. {text}")
    return "\n\n".join(contents) if contents else "未获取到引言"


def process_comment_and_code(comment_tag: BeautifulSoup) -> Tuple[str, str]:
    """处理评论和代码链接"""
    if not comment_tag:
        return "", ""
    
    # 提取所有链接（优先代码链接）
    a_tags = comment_tag.find_all("a", href=True)
    urls = set()
    for tag in a_tags:
        url = tag["href"].strip()
        if url.startswith(("/", "http://", "https://")):
            full_url = f"https://arxiv.org{url}" if url.startswith("/") else url
            urls.add(full_url)
    code = ", ".join(urls) if urls else ""
    
    # 清理评论（移除链接）
    raw_text = comment_tag.get_text(strip=True).replace("Comments:", "").strip()
    clean_comment = URL_PATTERN.sub("", raw_text).strip()
    clean_comment = re.sub(r'[,; ]+$', '', clean_comment)
    
    return clean_comment, code


def extract_pdf_link(dt_tag: BeautifulSoup) -> str:
    """从dt标签提取所有链接，筛选含"pdf"的链接"""
    all_a_tags = dt_tag.find_all("a", href=True)
    if not all_a_tags:
        logging.warning("dt标签中无链接，无法提取PDF")
        return ""
    
    # 筛选含"pdf"关键词的链接（不区分大小写）
    for a_tag in all_a_tags:
        href = a_tag["href"].strip()
        if PDF_LINK_PATTERN.search(href):
            # 补全相对路径
            if href.startswith("/"):
                return f"https://arxiv.org{href}"
            elif href.startswith(("http://", "https://")):
                return href
    
    logging.warning("dt标签中无含'pdf'的链接")
    return ""


def get_first_author(authors_str: str) -> str:
    """提取第一作者"""
    if not authors_str:
        return "未知作者"
    first_author = authors_str.split(",")[0].strip()
    return first_author if first_author else "未知作者"


def call_llm_for_summary(title: str, abstract: str, introduction: str) -> Dict:
    """调用LLM生成总结，并提取1-5分相关性评分"""
    system_prompt = """你是具身智能领域研究员，主要研究3D计算机视觉、vison-language、主动感知、robot maniuplation，不关心硬件相关创新，基于论文标题、摘要和引言，按以下格式输出：
【关注的是什么问题】
1. 核心问题1（≤40词）
2. 核心问题2（≤40词）
....
n. 核心问题n（≤40词）
【用了什么创新的方案】
核心解决方案（1-8句话）
【相关性评分】
分数：x分（x为1-5的整数，1分最低，5分最高，需基于你的研究领域进行相关性评定, 不用给出理由）
为了逻辑通顺保留英文关键词，不要额外内容，严格按格式输出。"""""

    user_prompt = f"标题：{title}\n摘要：{abstract}\n引言：{introduction}"
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 500
    })
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        conn = http.client.HTTPSConnection(LLM_API_HOST, timeout=30)
        conn.request("POST", LLM_API_ENDPOINT, payload, headers)
        res = conn.getresponse()
        
        if res.status != 200:
            raise Exception(f"API 状态码异常：{res.status}，响应：{res.read().decode('utf-8')}")
        
        data = json.loads(res.read().decode("utf-8"))
        conn.close()
        
        # 提取总结内容
        summary = data["choices"][0]["message"]["content"].strip()
        # 提取1-5分评分（默认0分表示提取失败）
        score_match = LLM_SCORE_PATTERN.search(summary)
        score = int(score_match.group(1)) if score_match and 1 <= int(score_match.group(1)) <= 5 else 0
        
        return {
            "summary": summary,
            "score": score,  # 新增评分字段（1-5或0）
            "error": ""
        }
    except Exception as e:
        error_msg = str(e)
        logging.error(f"大模型调用失败：{error_msg}")
        return {
            "summary": "大模型总结失败",
            "score": 0,  # 调用失败默认0分
            "error": error_msg
        }
    

def get_recent_dates(limit: int = 3) -> List[str]:
    """获取最近的日期列表（含今天），格式YYYY-MM-DD"""
    dates = []
    for i in range(limit):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        dates.append(date)
    return dates


def json_to_markdown(json_path: str, md_path: str) -> None:
    """生成Markdown表格，最近三天数据，当天展开，其他日期折叠"""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            date_papers = json.load(f)
        if not date_papers:
            logging.warning("JSON 中无论文数据，无法生成 Markdown")
            return
    except Exception as e:
        logging.error(f"读取 JSON 失败：{str(e)}")
        return
    
    # 获取最近三天日期（按从新到旧排序）
    recent_dates = get_recent_dates(3)
    # 筛选出有数据的日期，最多保留三天
    valid_dates = [date for date in recent_dates if date in date_papers and len(date_papers[date]) > 0]
    
    if not valid_dates:
        logging.warning("无有效论文数据，无法生成 Markdown")
        return
    
    # 基础信息
    total_papers = sum(len(date_papers[date]) for date in valid_dates)
    md_title = f"# {CURRENT_DATE} arXiv Robot 领域论文汇总（共{total_papers}篇）"
    md_intro = "> 说明：仅显示最近三天数据，当天论文默认展开，其他日期点击标题可展开/折叠\n"
    md_intro += "> 相关性评分：基于LLM对机器人领域的相关性评定（1-5分，★越多相关性越高）\n\n"
    
    # 表格表头
    md_table_header = """| Title | Author | Comment | PDF | Code | LLM Summary |  |
|----------|----------|---------|-----|------|--------|----------|"""
    
    # 按日期生成内容（当天展开，其他日期折叠）
    date_sections = []
    for date in valid_dates:
        papers = date_papers[date]
        paper_count = len(papers)
        date_display = f"{date}（{paper_count}篇论文）"
        
        # 生成表格行
        table_rows = []
        for paper in papers:
            # 1. 文章标题（转义特殊字符）
            title = paper.get("title", "未知标题").replace("|", "\\|").replace("\n", " ")
            
            # 2. 第一作者
            first_author = get_first_author(paper.get("authors", "未知作者"))
            
            # 3. Comment（折叠长内容）
            comment = paper.get("comment", "").replace("|", "\\|").replace("\n", "<br>")
            comment_html = f"<details><summary>detail</summary>{comment}</details>" if comment else ""
            
            # 4. PDF链接（可点击）
            pdf_link = paper.get("pdf_link", "")
            pdf_html = f"[PDF]({pdf_link})" if pdf_link else "-"
            
            # 5. Code链接（多链接分行）
            code = paper.get("code", "")
            if not code:
                code_html = "-"
            else:
                code_list = [url.strip() for url in code.split(",") if url.strip()]
                code_html = "<br>".join([f"[code{i+1}]({url})" for i, url in enumerate(code_list)])
            
            # 6. 相关性评分（1-5个星星）
            score = paper.get("llm_score", 0)
            if 1 <= score <= 5:
                score_html = "★" * score + "☆" * (5 - score)
            else:
                score_html = "-"
            
            # 7. LLM总结（折叠展示）
            llm_summary = paper.get("llm_summary", "无").replace("|", "\\|").replace("\n", "<br>")
            llm_html = f"<details><summary>查看总结</summary>{llm_summary}</details>" if llm_summary else "无"
            
            # 拼接表格行
            row = f"| {title} | {first_author} | {comment_html} | {pdf_html} | {code_html} | {score_html} | {llm_html} |"
            table_rows.append(row)
        
        # 组装日期区块（当天展开，其他折叠）
        if date == CURRENT_DATE:
            # 当天内容不折叠
            section = f"## {date_display}\n\n{md_table_header}\n" + "\n".join(table_rows) + "\n"
        else:
            # 其他日期内容折叠
            table_content = f"{md_table_header}\n" + "\n".join(table_rows)
            section = f"<details>\n<summary>## {date_display}</summary>\n\n{table_content}\n\n</details>\n"
        
        date_sections.append(section)
    
    # 合并所有内容
    md_content = f"{md_title}\n\n{md_intro}" + "\n".join(date_sections)
    
    # 保存Markdown
    try:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        logging.info(f"Markdown 表格已保存至：{md_path}")
    except Exception as e:
        logging.error(f"保存 Markdown 失败：{str(e)}")


# -------------------------- 核心函数 --------------------------
def crawl_and_process_papers(initial_url: str, max_pages: Optional[int] = None) -> Dict[str, List[Dict]]:
    """爬取arxiv论文列表，按日期组织论文数据"""
    global all_papers_global
    # 加载历史数据（按日期组织）
    try:
        with open(JSON_SAVE_PATH, "r", encoding="utf-8") as f:
            all_papers_global = json.load(f)
        logging.info(f"已加载历史数据，包含 {len(all_papers_global)} 个日期的数据")
    except FileNotFoundError:
        logging.info("无历史数据，将新建 JSON 文件")
        all_papers_global = {}
    except Exception as e:
        logging.warning(f"加载历史数据失败：{str(e)}，将重新爬取")
        all_papers_global = {}
    
    # 计算当前已爬取的论文总数
    total_papers = sum(len(papers) for papers in all_papers_global.values())
    current_page = total_papers // PAPERS_PER_PAGE + 1  # 当前页码
    current_date = datetime.now().strftime("%Y-%m-%d")  # 今日日期
    
    # 确保当前日期在字典中存在
    if current_date not in all_papers_global:
        all_papers_global[current_date] = []
    
    while True:
        # 终止条件：达到最大页数
        if max_pages and current_page > max_pages:
            logging.info(f"已达最大爬取页数 {max_pages}，停止爬取")
            break
        
        logging.info(f"=== 开始爬取第 {current_page} 页：{initial_url} ===")
        # 1. 获取列表页Soup
        list_soup = get_arxiv_soup(initial_url)
        if not list_soup:
            logging.error(f"第 {current_page} 页列表页爬取失败，跳过")
            break
        
        # 2. 提取论文列表（dt=链接信息，dd=元数据）
        articles_dl = list_soup.find("dl", id="articles")
        if not articles_dl:
            logging.error(f"第 {current_page} 页无论文数据，跳过")
            break
        
        dt_list = articles_dl.find_all("dt")
        dd_list = articles_dl.find_all("dd")
        # 处理数据不匹配情况
        if len(dt_list) != len(dd_list):
            min_len = min(len(dt_list), len(dd_list))
            dt_list, dd_list = dt_list[:min_len], dd_list[:min_len]
            logging.warning(f"第 {current_page} 页数据不匹配，截取前 {min_len} 篇论文")
        
        # 3. 处理每篇论文
        crawl_datetime = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for idx, (dt, dd) in enumerate(zip(dt_list, dd_list), 1):
            logging.info(f"第 {current_page} 页 - 处理第 {idx}/{len(dt_list)} 篇论文")
            
            # 3.1 提取HTML详情页链接
            html_link_tag = dt.find("a", title="View HTML")
            if not html_link_tag or "href" not in html_link_tag.attrs:
                logging.warning("论文无HTML详情页链接，跳过")
                continue
            html_link = html_link_tag["href"].strip()
            html_link = f"https://arxiv.org{html_link}" if html_link.startswith("/") else html_link
            
            # 检查是否已爬取（避免重复）
            is_duplicate = False
            for papers in all_papers_global.values():
                for paper in papers:
                    if paper.get("arxiv_html_link") == html_link:
                        is_duplicate = True
                        break
                if is_duplicate:
                    break
            if is_duplicate:
                logging.info(f"论文已爬取，跳过：{html_link}")
                continue
            
            # 3.2 提取PDF链接
            pdf_link = extract_pdf_link(dt)
            
            # 3.3 获取详情页Soup
            paper_soup = get_arxiv_soup(html_link)
            if not paper_soup:
                logging.warning(f"论文详情页爬取失败（{html_link}），跳过")
                continue
            
            # 3.4 提取元数据（标题、作者、学科等）
            meta_div = dd.find("div", class_="meta")
            if not meta_div:
                logging.warning(f"论文无基本元数据（{html_link}），跳过")
                continue
            
            # 标题
            title_tag = meta_div.find("div", class_="list-title")
            title = title_tag.get_text(strip=True).replace("Title:", "").strip() if title_tag else "未知标题"
            # 作者
            authors_tag = meta_div.find("div", class_="list-authors")
            authors = authors_tag.get_text(strip=True).replace("Authors:", "").strip() if authors_tag else "未知作者"
            # 学科
            subjects_tag = meta_div.find("div", class_="list-subjects")
            subjects = subjects_tag.get_text(strip=True).replace("Subjects:", "").strip() if subjects_tag else "未知学科"
            # 评论和代码链接
            comment_tag = meta_div.find("div", class_="list-comments")
            comment, code = process_comment_and_code(comment_tag)
            # 摘要链接（备用）
            abs_link_tag = dt.find("a", title="Abstract")
            abs_link = ""
            if abs_link_tag and "href" in abs_link_tag.attrs:
                abs_link = abs_link_tag["href"].strip()
                abs_link = f"https://arxiv.org{abs_link}" if abs_link.startswith("/") else abs_link
            
            # 3.5 提取摘要和引言
            abstract = extract_abstract(paper_soup)
            introduction = extract_introduction(paper_soup)
            
            # 3.6 调用LLM生成总结和评分
            llm_result = call_llm_for_summary(title, abstract, introduction)
            
            # 3.7 组装论文数据
            paper_data = {
                "crawl_datetime": crawl_datetime,  # 更详细的时间戳
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
                "llm_summary": llm_result["summary"],
                "llm_score": llm_result["score"],
                "llm_error": llm_result["error"]
            }
            # 添加到当前日期的列表中
            all_papers_global[current_date].append(paper_data)
            logging.info(f"第 {current_page} 页 - 完成第 {idx} 篇论文：{title[:30]}...")
        
        # 4. 保存当前页数据到JSON
        try:
            with open(JSON_SAVE_PATH, "w", encoding="utf-8") as f:
                json.dump(all_papers_global, f, ensure_ascii=False, indent=4)
            logging.info(f"第 {current_page} 页数据已保存至 JSON：{JSON_SAVE_PATH}")
        except Exception as e:
            logging.error(f"保存第 {current_page} 页数据失败：{str(e)}")
        
        # 5. 获取下一页链接
        next_page_tag = list_soup.find("a", string=lambda x: x and "next" in x.lower() and ">" in x)
        if not next_page_tag or "href" not in next_page_tag.attrs:
            logging.info("无下一页链接，爬取任务结束")
            break
        
        next_page_href = next_page_tag["href"].strip()
        initial_url = f"https://arxiv.org{next_page_href}" if next_page_href.startswith("/") else next_page_href
        current_page += 1

    return all_papers_global


# -------------------------- 程序入口 --------------------------
if __name__ == "__main__":
    # 1. 前置检查：LLM API Key是否配置
    if not LLM_API_KEY or LLM_API_KEY.startswith("sk-xxxx"):
        logging.error("请先替换 LLM_API_KEY 为真实有效的 API Key！")
        sys.exit(1)
    
    # 2. 初始化日志
    logging.info("="*60)
    logging.info("          arXiv cs.RO 领域论文爬取与LLM总结程序          ")
    logging.info("="*60)
    logging.info(f"配置信息：")
    logging.info(f"- 初始爬取页：{INITIAL_ARXIV_URL}")
    logging.info(f"- 最大爬取页数：{MAX_CRAWL_PAGES if MAX_CRAWL_PAGES else '无限制'}")
    logging.info(f"- JSON保存路径：{JSON_SAVE_PATH}")
    logging.info(f"- Markdown保存路径：{MD_SAVE_PATH}")
    logging.info("="*60)
    
    # 3. 启动爬取任务
    try:
        all_papers = crawl_and_process_papers(
            initial_url=INITIAL_ARXIV_URL,
            max_pages=MAX_CRAWL_PAGES
        )
        
        # 4. 生成Markdown报告
        logging.info("\n=== 开始生成 Markdown 报告 ===")
        json_to_markdown(JSON_SAVE_PATH, MD_SAVE_PATH)
        
        # 5. 任务完成总结
        total_count = sum(len(papers) for papers in all_papers.values())
        logging.info("\n" + "="*60)
        logging.info("          任务全部完成！          ")
        logging.info(f"- 日期数量：{len(all_papers)} 个")
        logging.info(f"- 最终爬取论文总数：{total_count} 篇")
        logging.info(f"- JSON原始数据：{JSON_SAVE_PATH}")
        logging.info(f"- Markdown报告：{MD_SAVE_PATH}")
        logging.info("="*60)
    
    except Exception as e:
        # 异常处理：保存已爬取数据
        error_msg = f"任务运行异常：{str(e)}\n{traceback.format_exc()}"
        logging.error(error_msg)
        if all_papers_global:
            try:
                with open(JSON_SAVE_PATH, "w", encoding="utf-8") as f:
                    json.dump(all_papers_global, f, ensure_ascii=False, indent=4)
                total_count = sum(len(papers) for papers in all_papers_global.values())
                logging.info(f"已保存异常中断前的 {total_count} 篇论文数据")
            except Exception as save_e:
                logging.error(f"异常中断时保存数据失败：{str(save_e)}")
        sys.exit(1)
