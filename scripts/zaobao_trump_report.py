#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests
from playwright.sync_api import BrowserContext, sync_playwright

from app_common import NotificationClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "output"
STATE_DIR = PROJECT_ROOT / "state"
REPORT_RETENTION_DAYS = 30
BLOCKLIST_PHRASES = {
    "This is a modal window.",
    "打开对话窗口。Escape键将取消并关闭对话窗口。",
    "结束对话窗口",
    "播放视频",
    "播放 API 请求失败，原因未知",
    "错误代码: VIDEO_CLOUD_ERR_UNKNOWN",
    "技术细节:",
    "确定",
    "关闭弹窗",
    "延伸阅读",
    "设为谷歌新闻首选来源",
    "小",
    "标准",
    "中",
    "大",
}
BLOCKLIST_SUBSTRINGS = (
    "小 标准 中 大",
    "上一篇",
    "下一篇",
    "购买此文章",
    "推荐购买",
    "立即购买",
    "所有商品均由新报业媒体购物团队严选",
    "最新",
    "热门",
    "更多消息",
)


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str


@dataclass(frozen=True)
class TrumpSpeechConfig:
    scrape_time: str
    search_query: str
    search_url: str
    max_results: int
    max_articles: int
    playwright_headless: bool
    playwright_timeout_seconds: int
    browser_executable_path: str
    llm: LLMConfig
    notification_exe: str
    notification_channel: str

    @classmethod
    def from_env(cls) -> "TrumpSpeechConfig":
        search_query = os.getenv("SEARCH_QUERY", "特朗普").strip() or "特朗普"
        search_url = os.getenv("SEARCH_URL", "").strip()
        if not search_url:
            search_url = f"https://www.zaobao.com.sg/sitesearch?r={quote(search_query)}"
        return cls(
            scrape_time=os.getenv("SCRAPE_TIME", "19:00").strip(),
            search_query=search_query,
            search_url=search_url,
            max_results=max(1, int(os.getenv("MAX_RESULTS", "8"))),
            max_articles=max(1, int(os.getenv("MAX_ARTICLES", "6"))),
            playwright_headless=os.getenv("PLAYWRIGHT_HEADLESS", "true").strip().lower() != "false",
            playwright_timeout_seconds=max(30, int(os.getenv("PLAYWRIGHT_TIMEOUT_SECONDS", "120"))),
            browser_executable_path=os.getenv("PLAYWRIGHT_EXECUTABLE_PATH", "").strip(),
            llm=LLMConfig(
                api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
                base_url=os.getenv("DASHSCOPE_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1").strip(),
                model=os.getenv("DASHSCOPE_MODEL", "qwen3.5-plus").strip(),
            ),
            notification_exe=os.getenv("PICOCLAW_EXE", "").strip(),
            notification_channel=os.getenv("PICOCLAW_CHANNEL", "feishu").strip(),
        )


class TrumpSpeechReporter:
    def __init__(self, config: TrumpSpeechConfig, logger) -> None:
        self.config = config
        self.logger = logger
        self.notification = NotificationClient(config.notification_exe, config.notification_channel)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def run_once(self, send_notification: bool = True) -> dict:
        report = self._build_report()
        self._cleanup_old_files()
        self._save_report(report)
        if send_notification:
            self._send_report(report)
        return report

    def run_testsend(self, use_existing: bool) -> dict:
        if use_existing:
            report = self.load_latest_report()
            if not report:
                raise Exception("未找到可复用的 latest_report.json，请先执行 testsend-live 或等待定时任务跑完。")
        else:
            report = self.run_once(send_notification=False)
        self._send_report(report)
        return report

    def load_latest_report(self) -> dict | None:
        path = STATE_DIR / "latest_report.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _build_report(self) -> dict:
        if not self.config.llm.api_key:
            raise Exception("DASHSCOPE_API_KEY 未配置，无法执行分析。")

        results, articles = self._scrape_zaobao()
        if not results:
            raise Exception("搜索结果为空，未抓到任何候选文章。")
        if not articles:
            raise Exception(f"没有找到包含“{self.config.search_query}”的正文文章。")

        digests = self._summarize_articles(articles)
        for article, digest in zip(articles, digests):
            article["llm_digest"] = digest

        report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        analysis = self._call_analysis_llm(self._build_analysis_input(articles)).strip()
        notification_message = self._format_notification_message(articles)

        return {
            "generated_at": report_time,
            "search_query": self.config.search_query,
            "search_url": self.config.search_url,
            "result_count": len(results),
            "article_count": len(articles),
            "results": results,
            "articles": articles,
            "analysis": analysis,
            "notification_message": notification_message,
            "config": {
                "max_results": self.config.max_results,
                "max_articles": self.config.max_articles,
                "search_query": self.config.search_query,
                "model": self.config.llm.model,
                "browser_executable_path": self._resolve_browser_executable() or "",
            },
        }

    def _scrape_zaobao(self) -> tuple[list[dict], list[dict]]:
        self.logger.log(f"开始抓取搜索页: {self.config.search_url}")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.config.playwright_headless,
                executable_path=self._resolve_browser_executable(),
            )
            context = browser.new_context()
            try:
                results = self._collect_search_results(context)
                selected = results
                articles = [self._extract_article(context, item) for item in selected]
                articles = [item for item in articles if item]
                return results, articles
            finally:
                context.close()
                browser.close()

    def _collect_search_results(self, context: BrowserContext) -> list[dict]:
        page = context.new_page()
        page.set_default_timeout(self.config.playwright_timeout_seconds * 1000)
        page.goto(self.config.search_url, wait_until="domcontentloaded")
        page.wait_for_function(
            """() => Array.from(document.querySelectorAll("main a[href*='/story']")).some(
                (a) => {
                    const href = a.getAttribute('href') || '';
                    const text = (a.innerText || '').trim();
                    return !href.includes('ref=sidebar') && text.includes('\\n');
                }
            )""",
        )
        raw_items = page.evaluate(
            """() => Array.from(document.querySelectorAll("main a[href*='/story']")).map((a) => ({
                href: a.href,
                rawHref: a.getAttribute('href') || '',
                text: (a.innerText || '').trim(),
                className: a.className || ''
            }))"""
        )
        results: list[dict] = []
        seen: set[str] = set()
        for item in raw_items:
            href = (item.get("href") or "").split("?")[0]
            raw_href = item.get("rawHref") or ""
            text = item.get("text") or ""
            class_name = item.get("className") or ""
            if not href or href in seen or "zaobao.com.sg" not in href:
                continue
            if "ref=sidebar" in raw_href:
                continue
            if "max-lg:min-h-[136px]" not in class_name:
                continue
            if "\n" not in text:
                continue
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if not lines:
                continue
            title = lines[0]
            published = lines[-1] if len(lines) > 1 else ""
            summary = "\n".join(lines[1:-1]).strip() if len(lines) > 2 else ""
            if len(title) < 6:
                continue
            seen.add(href)
            results.append(
                {
                    "title": title,
                    "summary": summary,
                    "published_hint": published,
                    "url": href,
                }
            )
            if len(results) >= self.config.max_results:
                break
        page.close()
        return results

    def _extract_article(self, context: BrowserContext, item: dict) -> dict | None:
        self.logger.log(f"抓取正文: {item['title']}")
        page = context.new_page()
        page.set_default_timeout(self.config.playwright_timeout_seconds * 1000)
        try:
            page.goto(item["url"], wait_until="domcontentloaded")
            page.locator("h1").first.wait_for()
            data = page.evaluate(
                """() => {
                    const title = document.querySelector('h1')?.innerText?.trim() || '';
                    const main = document.querySelector('main');
                    const metaTexts = Array.from(main?.querySelectorAll('button,div,span') || [])
                        .map(el => (el.innerText || '').trim())
                        .filter(Boolean);
                    const paragraphs = Array.from(main?.querySelectorAll('p') || [])
                        .map(p => (p.innerText || '').trim())
                        .filter(Boolean);
                    return { title, metaTexts, paragraphs };
                }"""
            )
            paragraphs = self._clean_paragraphs(data.get("paragraphs", []), data.get("title") or item["title"])
            if not paragraphs:
                return None
            article = {
                "title": data.get("title") or item["title"],
                "summary": item.get("summary", ""),
                "published_hint": item.get("published_hint", ""),
                "url": item["url"],
                "published_at": self._extract_meta(data.get("metaTexts", []), "发布"),
                "updated_at": self._extract_meta(data.get("metaTexts", []), "更新"),
                "content": "\n\n".join(paragraphs),
            }
            return article
        finally:
            page.close()

    def _clean_paragraphs(self, paragraphs: list[str], title: str) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for paragraph in paragraphs:
            value = " ".join(paragraph.split())
            if not value or value in seen:
                continue
            if value in BLOCKLIST_PHRASES:
                continue
            if value == title:
                continue
            if value.startswith("会话 ID：") or value.startswith("播放器元素 ID："):
                continue
            if any(token in value for token in BLOCKLIST_SUBSTRINGS):
                break
            if len(value) < 18:
                continue
            seen.add(value)
            cleaned.append(value)
        return cleaned

    def _extract_meta(self, values: list[str], prefix: str) -> str:
        for value in values:
            if value.startswith(f"{prefix}/"):
                return value
        return ""

    def _build_analysis_input(self, articles: list[dict]) -> str:
        parts: list[str] = []
        for index, article in enumerate(articles, start=1):
            parts.append(
                "\n".join(
                    [
                        f"文章 {index}",
                        f"标题：{article['title']}",
                        f"链接：{article['url']}",
                        f"发布时间：{article.get('published_at') or article.get('published_hint')}",
                        f"搜索摘要：{article.get('summary', '')}",
                        "正文：",
                        article["content"],
                    ]
                )
            )
        return "\n\n".join(parts)

    def _build_digest_input(self, articles: list[dict]) -> str:
        parts: list[str] = []
        for index, article in enumerate(articles, start=1):
            parts.append(
                "\n".join(
                    [
                        f"[文章{index}]",
                        f"标题：{article['title']}",
                        f"发布时间：{article.get('published_at') or article.get('published_hint')}",
                        f"搜索摘要：{article.get('summary', '')}",
                        "正文：",
                        article["content"],
                    ]
                )
            )
        return "\n\n".join(parts)

    def _resolve_browser_executable(self) -> str | None:
        if self.config.browser_executable_path:
            return self.config.browser_executable_path
        base = Path.home() / ".cache" / "ms-playwright"
        candidates = sorted(base.glob("chromium-*/chrome-linux64/chrome"), reverse=True)
        if candidates:
            return str(candidates[0])
        return None

    def _summarize_articles(self, articles: list[dict]) -> list[str]:
        prompt = self._build_digest_input(articles)
        response_text = self._call_llm(
            system_prompt=(
                "你是新闻摘要编辑。请阅读每篇文章内容，按输入顺序输出一个 JSON 数组。"
                "数组每个元素是对应文章的一句话中文摘要。"
                "要求："
                "1. 必须基于正文，不要照搬标题。"
                "2. 每条摘要 30 到 120 个汉字，尽量点出动作、表态或影响。"
                "3. 不要编号，不要项目名，不要 Markdown，不要解释。"
                "4. 只返回 JSON 数组。"
            ),
            user_content=prompt,
            max_tokens=1800,
        )
        digests = self._parse_json_array(response_text)
        normalized: list[str] = []
        for index, article in enumerate(articles):
            if index < len(digests):
                digest = self._normalize_digest(str(digests[index]))
                if digest:
                    normalized.append(digest)
                    continue
            normalized.append(self._fallback_digest(article))
        return normalized

    def _call_analysis_llm(self, content: str) -> str:
        return self._normalize_markdown(
            self._call_llm(
                system_prompt=(
                    "你是一位政治与国际新闻分析编辑，负责整理特朗普近期讲话、表态和政策动作。"
                    "请只基于用户提供的文章内容，输出简洁中文 Markdown。"
                    "输出必须包含：今日讲话重点、核心表态、政策信号、市场与地缘影响、观察。"
                    "不要输出 #、##、### 标题；如果使用标题，最高只允许 ####。"
                    "不要编造未出现的信息。"
                ),
                user_content=content,
                max_tokens=4000,
            )
        )

    def _call_llm(self, system_prompt: str, user_content: str, max_tokens: int) -> str:
        payload = {
            "model": self.config.llm.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.config.llm.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.config.llm.base_url.rstrip('/')}/chat/completions"
        session = requests.Session()
        session.trust_env = False
        response = session.post(url, headers=headers, json=payload, timeout=180)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()

    def _parse_json_array(self, text: str) -> list[str]:
        candidates = [text.strip()]
        fence_match = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", text)
        if fence_match:
            candidates.insert(0, fence_match.group(1).strip())
        bare_match = re.search(r"(\[[\s\S]*\])", text)
        if bare_match:
            candidates.insert(0, bare_match.group(1).strip())
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, list):
                return [str(item).strip() for item in data]
        return []

    def _normalize_markdown(self, text: str) -> str:
        lines: list[str] = []
        for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = raw_line.rstrip()
            match = re.match(r"^\s{0,3}(#+)\s*(.*)$", line)
            if match:
                title = match.group(2).strip()
                lines.append(f"#### {title}" if title else "####")
            else:
                lines.append(line)
        return "\n".join(lines).strip()

    def _format_notification_message(self, articles: list[dict]) -> str:
        lines: list[str] = []
        for index, article in enumerate(articles, start=1):
            lines.append(f"{index}. {article['title']}")
            lines.append("")
            lines.append(self._article_digest(article))
            if index != len(articles):
                lines.append("")
        return "\n".join(lines).strip()

    def _article_digest(self, article: dict) -> str:
        llm_digest = self._normalize_digest(article.get("llm_digest", ""))
        if llm_digest:
            return llm_digest
        return self._fallback_digest(article)

    def _fallback_digest(self, article: dict) -> str:
        summary = self._condense_text(article.get("summary") or "")
        if summary:
            return summary
        return self._condense_text(article.get("content") or "")

    def _normalize_digest(self, text: str) -> str:
        value = " ".join(str(text).split()).strip()
        if not value:
            return ""
        value = re.sub(r"^[\-*•\d\.\)\s]+", "", value)
        value = value.replace("摘要：", "").replace("总结：", "").strip()
        if len(value) <= 120:
            return value
        return value[:120].rstrip() + "..."

    def _condense_text(self, text: str) -> str:
        value = " ".join(text.split()).strip()
        if not value:
            return ""
        parts = re.split(r"(?<=[。！？!?])\s*", value, maxsplit=1)
        first_sentence = parts[0].strip() if parts else value
        if len(first_sentence) <= 120:
            return first_sentence
        return first_sentence[:120].rstrip() + "..."

    def _save_report(self, report: dict) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        markdown = report.get("notification_message", "").strip() + "\n"
        (STATE_DIR / "latest_report.json").write_text(payload, encoding="utf-8")
        (STATE_DIR / f"report-{timestamp}.json").write_text(payload, encoding="utf-8")
        (OUTPUT_DIR / "latest_report.md").write_text(markdown, encoding="utf-8")
        (OUTPUT_DIR / f"report-{timestamp}.md").write_text(markdown, encoding="utf-8")
        (OUTPUT_DIR / f"search-results-{timestamp}.json").write_text(
            json.dumps(report.get("results", []), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (OUTPUT_DIR / f"article-details-{timestamp}.json").write_text(
            json.dumps(report.get("articles", []), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _send_report(self, report: dict) -> None:
        message = report.get("notification_message", "").strip()
        if not message:
            raise Exception("报告为空，无法发送通知。")
        if not self.notification.enabled:
            raise Exception("PICOCLAW_EXE 未配置，无法发送通知。")
        if not self.notification.send(message):
            raise Exception(f"通知发送失败: {self.notification.last_error or '未知错误'}")

    def _cleanup_old_files(self) -> None:
        cutoff = datetime.now() - timedelta(days=REPORT_RETENTION_DAYS)
        for path in STATE_DIR.glob("report-*.json"):
            stamp = path.stem.replace("report-", "")
            try:
                file_time = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            if file_time < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass
        for pattern, prefix in (
            ("report-*.md", "report-"),
            ("search-results-*.json", "search-results-"),
            ("article-details-*.json", "article-details-"),
        ):
            for path in OUTPUT_DIR.glob(pattern):
                stamp = path.stem.replace(prefix, "")
                try:
                    file_time = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
                except ValueError:
                    continue
                if file_time < cutoff:
                    try:
                        path.unlink()
                    except OSError:
                        pass
