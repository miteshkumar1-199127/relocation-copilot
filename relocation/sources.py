"""Dated, bounded source retrieval. No model-generated URL is fetched automatically."""

from __future__ import annotations

import ipaddress
import socket
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .models import DOMAINS, Evidence


class SourceError(RuntimeError):
    pass


def _public_https(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SourceError("Only public HTTPS URLs without credentials are allowed")
    if parsed.port not in (None, 443):
        raise SourceError("Only HTTPS port 443 is allowed")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceError(f"Could not resolve source host: {exc}") from exc
    if not addresses:
        raise SourceError("Source host has no address")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise SourceError("Private or reserved source address is blocked")
    return url


def fetch_source(url: str, domain: str, source_id: str) -> Evidence:
    if domain not in DOMAINS:
        raise SourceError("Choose a valid source domain")
    _public_https(url)
    try:
        with httpx.Client(timeout=8, follow_redirects=False, headers={"User-Agent": "RelocationCopilot/1.0"}) as client:
            with client.stream("GET", url) as response:
                if 300 <= response.status_code < 400:
                    raise SourceError("Redirects require review; enter the final HTTPS URL")
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" not in content_type and "text/plain" not in content_type:
                    raise SourceError("Only HTML and plain-text pages are supported")
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        raise SourceError("Source exceeds the 2 MB limit")
                    chunks.append(chunk)
                page = b"".join(chunks).decode(response.charset_encoding or "utf-8", errors="replace")
            if "text/html" in content_type:
                soup = BeautifulSoup(page, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "form"]):
                    tag.decompose()
                title = soup.title.get_text(" ", strip=True) if soup.title else urlparse(url).hostname or url
                text = soup.get_text(" ", strip=True)
            else:
                title, text = urlparse(url).hostname or url, page
            text = " ".join(text.split())[:16000]
            if len(text) < 80:
                raise SourceError("Source has too little extractable text")
            return {
                "id": source_id, "domain": domain, "title": title[:200], "url": url,
                "retrieved_at": datetime.now(timezone.utc).isoformat(), "text": text,
            }
    except httpx.HTTPError as exc:
        raise SourceError(f"Source retrieval failed: {exc}") from exc


def search_tavily(query: str, key: str) -> list[dict[str, str]]:
    """Optional discovery; results still need explicit user selection and fetching."""
    if not key:
        raise SourceError("TAVILY_API_KEY is required for search; direct URL ingestion still works")
    try:
        response = httpx.post(
            "https://api.tavily.com/search", timeout=10,
            json={"api_key": key, "query": query[:300], "search_depth": "basic", "max_results": 5,
                  "include_answer": False, "include_raw_content": False},
        )
        response.raise_for_status()
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "snippet": x.get("content", "")[:500]} for x in response.json().get("results", [])]
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError(f"Search failed: {exc}") from exc


def search_you(query: str, key: str) -> list[dict[str, str]]:
    """Search You.com and normalize web results for specialist agents."""
    if not key:
        raise SourceError("YOU_API_KEY is required for web research")
    try:
        response = httpx.post(
            "https://ydc-index.io/v1/search", timeout=8,
            headers={"X-API-Key": key, "Content-Type": "application/json"},
            json={"query": query[:300], "count": 3, "safesearch": "moderate"},
        )
        response.raise_for_status()
        web = response.json().get("results", {}).get("web", [])
        normalized = []
        for item in web:
            snippets = item.get("snippets") or []
            text = " ".join(str(value) for value in snippets if value) or str(item.get("description", ""))
            normalized.append({"title": str(item.get("title", "")), "url": str(item.get("url", "")),
                               "snippet": text[:1400]})
        return normalized
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise SourceError(f"You.com search failed: {exc}") from exc


def discover_housing_sources(profile: dict, existing: list[Evidence], search_key: str) -> list[Evidence]:
    """Fetch current housing pages through search or a reviewed Seoul directory."""
    if profile.get("skip_auto_housing_search") or any(source["domain"] == "housing" for source in existing):
        return existing
    destination = str(profile["destination"])[:100]
    if not search_key:
        # A reviewed public directory keeps the Seoul relocation flow useful
        # without pretending that a general web search service is available.
        if "seoul" not in destination.lower():
            return existing
        try:
            return [*existing, fetch_source("https://seoulhomes.kr/en/properties/for-rent/",
                                            "housing", f"auto-housing-{uuid.uuid4().hex[:10]}")]
        except SourceError:
            return existing
    budget = profile.get("housing_budget_krw")
    query = f"{destination} apartment monthly rental listing price"
    if budget:
        query += f" under {int(budget)} KRW"
    try:
        results = search_you(query, search_key)
    except SourceError:
        return existing
    discovered = list(existing)
    seen = {source["url"] for source in existing}
    for result in results:
        url = str(result.get("url", "")).strip()
        if url and not urlparse(url).scheme:
            url = "https://" + url.lstrip("/")
        parsed = urlparse(url)
        if parsed.scheme == "http" and parsed.hostname:
            url = parsed._replace(scheme="https").geturl()
            parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or url in seen:
            continue
        try:
            source = fetch_source(url, "housing", f"auto-housing-{uuid.uuid4().hex[:10]}")
        except SourceError:
            continue
        discovered.append(source)
        seen.add(url)
        if sum(source["domain"] == "housing" for source in discovered) >= 2:
            break
    return discovered


def discover_stage_sources(profile: dict, domain: str, existing: list[Evidence], search_key: str) -> list[Evidence]:
    """Find and fetch a small set of current pages for one active journey stage."""
    if domain not in DOMAINS or not search_key:
        return existing
    destination = str(profile.get("destination", ""))[:100]
    origin = str(profile.get("origin", ""))[:100]
    queries = {
        "immigration": f"official {destination} immigration work visa types requirements",
        "housing": f"{destination} homes for rent listing near {profile.get('workplace') or destination} price",
        "finance": f"official {destination} newcomer bank account insurance money transfer guide",
        "logistics": f"international household movers {origin} to {destination} customer ratings",
        "travel": f"flights {origin} to {destination} {profile.get('move_date', '')} routes baggage",
    }
    try:
        results = search_you(queries.get(domain, f"{destination} relocation {domain}"), search_key)
    except SourceError:
        return existing
    # Refresh this stage rather than accumulating stale excerpts after retries.
    discovered = [item for item in existing if item["domain"] != domain]
    seen = {item["url"] for item in discovered}
    for result in results:
        url = str(result.get("url", "")).strip()
        if url and not urlparse(url).scheme:
            url = "https://" + url.lstrip("/")
        parsed = urlparse(url)
        if parsed.scheme == "http" and parsed.hostname:
            url = parsed._replace(scheme="https").geturl()
            parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or url in seen:
            continue
        # You.com already returns query-focused excerpts. Using them directly
        # avoids serial page downloads and keeps the customer action responsive.
        snippet = " ".join(str(result.get("snippet", "")).split())[:1400]
        if len(snippet) < 80:
            continue
        source = {"id": f"search-{domain}-{uuid.uuid4().hex[:10]}", "domain": domain,
                  "title": str(result.get("title") or urlparse(url).hostname or "Search result")[:200],
                  "url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(),
                  "text": "You.com search excerpt: " + snippet}
        discovered.append(source)
        seen.add(url)
        if sum(item["domain"] == domain for item in discovered) >= 2:
            break
    return discovered
