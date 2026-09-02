import asyncio
import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from crawl4ai.content_scraping_strategy import LXMLWebScrapingStrategy
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy

OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

GENERIC_SERVICE_PATHS = {
    "services",
    "service",
    "practice-areas",
    "practiceareas",
    "areas-of-practice",
    "legal-services",
    "our-services",
    "what-we-do",
}

SKIP_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".zip", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".mp3",
    ".mp4", ".avi", ".mov", ".wmv", ".css", ".js", ".xml", ".json",
}


def host_key(host: str) -> str:
    host = (host or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def normalize_url(url: str) -> str:
    p = urlparse(url)
    scheme = (p.scheme or "https").lower()
    host = (p.hostname or "").lower()
    if not host:
        return url
    netloc = host
    if p.port and not ((scheme == "http" and p.port == 80) or (scheme == "https" and p.port == 443)):
        netloc = f"{host}:{p.port}"
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", p.query, ""))


def is_same_site(url: str, root_host: str) -> bool:
    try:
        return host_key(urlparse(url).hostname or "") == root_host
    except Exception:
        return False


def is_htmlish(url: str) -> bool:
    path = urlparse(url).path.lower()
    return not any(path.endswith(ext) for ext in SKIP_EXTENSIONS)


def slug_tokens(text: str):
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]


def phrase_match(service: str, text: str) -> bool:
    service_tokens = slug_tokens(service)
    text_tokens = set(slug_tokens(text))
    return bool(service_tokens) and all(t in text_tokens for t in service_tokens)


def generic_service_page(url: str) -> bool:
    parts = [p for p in urlparse(url).path.lower().strip("/").split("/") if p]
    return len(parts) == 1 and parts[0] in GENERIC_SERVICE_PATHS


def extract_h1(html: str) -> str:
    if not html:
        return ""
    m = re.search(r"<h1\b[^>]*>(.*?)</h1>", html, flags=re.I | re.S)
    if not m:
        return ""
    return re.sub(r"<[^>]+>", " ", m.group(1)).strip()


def candidate_score(service: str, url: str, title: str, h1: str, inbound_texts: list[str]):
    path_text = urlparse(url).path.replace("-", " ").replace("_", " ")
    title_h1 = f"{title} {h1}"
    inbound = " ".join(inbound_texts)
    score = 0
    evidence = []
    if phrase_match(service, path_text):
        score += 5
        evidence.append("service phrase in URL path")
    if phrase_match(service, title_h1):
        score += 3
        evidence.append("service phrase in title/H1")
    if phrase_match(service, inbound):
        score += 2
        evidence.append("service phrase in inbound anchor text")
    if generic_service_page(url):
        score -= 5
        evidence.append("generic Services/Practice Areas page")
    return score, evidence


async def crawl_site(crawler: AsyncWebCrawler, target: dict):
    start_url = target["website"]
    root_host = host_key(urlparse(start_url).hostname or "")
    config = CrawlerRunConfig(
        deep_crawl_strategy=BFSDeepCrawlStrategy(
            max_depth=6,
            include_external=False,
            max_pages=300,
        ),
        scraping_strategy=LXMLWebScrapingStrategy(),
        cache_mode=CacheMode.BYPASS,
        exclude_external_links=True,
        exclude_social_media_links=True,
        check_robots_txt=True,
        page_timeout=45000,
        verbose=False,
    )

    result_obj = await crawler.arun(url=start_url, config=config)
    try:
        results = list(result_obj)
    except TypeError:
        results = [result_obj]

    pages = {}
    edges = []
    errors = []

    for result in results:
        page_url = normalize_url(getattr(result, "url", start_url))
        if not is_same_site(page_url, root_host):
            continue
        success = bool(getattr(result, "success", False))
        status_code = getattr(result, "status_code", None)
        metadata = getattr(result, "metadata", {}) or {}
        title = metadata.get("title") or ""
        html = getattr(result, "cleaned_html", "") or getattr(result, "html", "") or ""
        h1 = extract_h1(html)
        depth = metadata.get("depth", 0)
        pages[page_url] = {
            "url": page_url,
            "title": title,
            "h1": h1,
            "depth": depth,
            "status_code": status_code,
            "success": success,
        }
        if not success:
            errors.append({"url": page_url, "error": getattr(result, "error_message", "crawl failed")})

        links = getattr(result, "links", {}) or {}
        internal = links.get("internal", []) if isinstance(links, dict) else []
        for link in internal:
            href = (link or {}).get("href") or ""
            text = ((link or {}).get("text") or "").strip()
            if not href:
                continue
            absolute = normalize_url(urljoin(page_url, href))
            if not is_same_site(absolute, root_host) or not is_htmlish(absolute):
                continue
            edges.append({
                "source": page_url,
                "anchor_text": text,
                "destination": absolute,
            })

    # Include internally discovered destinations even if robots/max_pages prevented crawling them.
    for edge in edges:
        pages.setdefault(edge["destination"], {
            "url": edge["destination"],
            "title": "",
            "h1": "",
            "depth": None,
            "status_code": None,
            "success": None,
        })

    inbound = defaultdict(list)
    for edge in edges:
        if edge["anchor_text"]:
            inbound[edge["destination"]].append(edge["anchor_text"])

    candidates = []
    for service in target["target_services"]:
        service_matches = []
        for url, page in pages.items():
            score, evidence = candidate_score(
                service,
                url,
                page.get("title", ""),
                page.get("h1", ""),
                inbound.get(url, []),
            )
            if score >= 2:
                service_matches.append({
                    "service": service,
                    "url": url,
                    "title": page.get("title", ""),
                    "h1": page.get("h1", ""),
                    "inbound_anchor_text": sorted(set(inbound.get(url, []))),
                    "score": score,
                    "generic_service_page": generic_service_page(url),
                    "evidence": evidence,
                })
        service_matches.sort(key=lambda x: (-x["score"], x["url"]))
        candidates.append({"service": service, "matches": service_matches})

    return {
        "row": target["row"],
        "company": target["company"],
        "website": start_url,
        "target_services": target["target_services"],
        "pages_crawled": sum(1 for p in pages.values() if p.get("success") is not None),
        "unique_internal_urls": len(pages),
        "pages": sorted(pages.values(), key=lambda x: x["url"]),
        "edges": sorted(edges, key=lambda x: (x["source"], x["destination"], x["anchor_text"])),
        "service_page_candidates": candidates,
        "errors": errors,
    }


async def main():
    targets = json.loads(Path("targets.json").read_text())
    browser = BrowserConfig(headless=True, java_script_enabled=True, verbose=False)
    all_results = []

    async with AsyncWebCrawler(config=browser) as crawler:
        for target in targets:
            print(f"\n=== Crawling row {target['row']}: {target['company']} ===", flush=True)
            try:
                site = await asyncio.wait_for(crawl_site(crawler, target), timeout=1500)
            except asyncio.TimeoutError:
                site = {
                    "row": target["row"],
                    "company": target["company"],
                    "website": target["website"],
                    "target_services": target["target_services"],
                    "pages_crawled": 0,
                    "unique_internal_urls": 0,
                    "pages": [],
                    "edges": [],
                    "service_page_candidates": [],
                    "errors": [{"url": target["website"], "error": "site crawl timed out after 25 minutes"}],
                }
            except Exception as exc:
                site = {
                    "row": target["row"],
                    "company": target["company"],
                    "website": target["website"],
                    "target_services": target["target_services"],
                    "pages_crawled": 0,
                    "unique_internal_urls": 0,
                    "pages": [],
                    "edges": [],
                    "service_page_candidates": [],
                    "errors": [{"url": target["website"], "error": repr(exc)}],
                }
            all_results.append(site)
            print(
                f"Found {site['unique_internal_urls']} unique internal URLs; "
                f"crawled {site['pages_crawled']} pages; errors={len(site['errors'])}",
                flush=True,
            )

    (OUTPUT_DIR / "crawl_results.json").write_text(json.dumps(all_results, indent=2, ensure_ascii=False))

    with (OUTPUT_DIR / "internal_links.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["row", "company", "source", "anchor_text", "destination"])
        writer.writeheader()
        for site in all_results:
            for edge in site["edges"]:
                writer.writerow({"row": site["row"], "company": site["company"], **edge})

    with (OUTPUT_DIR / "pages.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["row", "company", "url", "title", "h1", "depth", "status_code", "success"],
        )
        writer.writeheader()
        for site in all_results:
            for page in site["pages"]:
                writer.writerow({"row": site["row"], "company": site["company"], **page})

    lines = ["# Crawl summary", "", "No Google Search or sitemap input was used. Discovery came from links reachable from each supplied website.", ""]
    for site in all_results:
        lines += [
            f"## Row {site['row']} — {site['company']}",
            f"- Website: {site['website']}",
            f"- Crawled pages: {site['pages_crawled']}",
            f"- Unique internal URLs discovered: {site['unique_internal_urls']}",
        ]
        if site["errors"]:
            lines.append(f"- Crawl errors: {len(site['errors'])}")
        for group in site["service_page_candidates"]:
            matches = group["matches"][:8]
            lines.append(f"- Target `{group['service']}`: {len(group['matches'])} candidate page(s)")
            for m in matches:
                lines.append(f"  - score {m['score']}: {m['url']}")
        lines.append("")
    (OUTPUT_DIR / "summary.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
