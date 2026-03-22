#!/usr/bin/env python3
# Copyright 2025
"""테크 관련 RSS 피드를 웹에서 수집한 뒤 OpenAI로 용어·의미를 추출해 JSON에 병합합니다.

주기 실행을 고려해 (1) 일자별 실행 기록·당일 추출 용어 목록,
(2) 용어별 누적 등장 횟수·관측 날짜를 함께 저장합니다.

외부 '용어 사전 API'는 쓰지 않고, 피드 URL로 공개 웹 콘텐츠를 가져옵니다.
기본 저장 경로는 GitHub Pages용 `docs/data/tech_terms.json` 한 곳입니다.
실행은 `python scripts/collect_tech_terms.py` 한 줄이며, 동작은 환경 변수·코드
기본값으로만 제어합니다(GitHub Actions와 로컬 동일).
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from dotenv import load_dotenv
from openai import OpenAI

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

logger = logging.getLogger(__name__)

# 기본 피드: Actions·로컬 모두 동일 (환경변수 TECH_TERM_FEEDS로 덮어쓰기 가능)
_DEFAULT_FEED_URLS: tuple[str, ...] = (
    "https://hnrss.org/frontpage",
    "https://lobste.rs/rss",
    "https://dev.to/feed",
    "https://github.blog/feed/",
    "https://kubernetes.io/feed.xml",
    "https://blog.rust-lang.org/feed.xml",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://www.phoronix.com/rss.php",
    "https://rss.slashdot.org/Slashdot/slashdotMain",
)

# 일부 피드(dev.to 등)가 비브라우저 UA를 403으로 막아, 일반 브라우저 문자열을 사용합니다.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def _utc_now_iso() -> str:
    """현재 시각을 UTC ISO8601 문자열로 반환합니다."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_date_str() -> str:
    """현재 날짜를 UTC 기준 YYYY-MM-DD로 반환합니다."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def normalize_term_key(term: str) -> str:
    """병합 시 중복 판별용 키(소문자·앞뒤 공백 제거)."""
    return term.strip().lower()


def _env_int(name: str, default: int) -> int:
    """정수 환경 변수를 읽고, 잘못된 값이면 기본값을 씁니다."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("환경 변수 %s=%r 은(는) 정수가 아니어서 기본값 %s 사용", name, raw, default)
        return default


def _feed_urls_from_env() -> list[str]:
    """TECH_TERM_FEEDS(쉼표 구분) 또는 기본 피드 목록을 반환합니다."""
    raw = os.environ.get("TECH_TERM_FEEDS", "").strip()
    if not raw:
        return list(_DEFAULT_FEED_URLS)
    return [u.strip() for u in raw.split(",") if u.strip()]


def _output_path() -> Path:
    """TECH_TERMS_OUTPUT 또는 docs/data/tech_terms.json 경로."""
    raw = os.environ.get("TECH_TERMS_OUTPUT", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (_ROOT / "docs" / "data" / "tech_terms.json").resolve()


def strip_html(text: str) -> str:
    """태그를 제거하고 HTML 엔티티를 풀어 평문에 가깝게 만듭니다."""
    if not text:
        return ""
    # 블록 구분을 위해 일부 태그는 줄바꿈으로 치환
    text = re.sub(r"(?i)</(p|div|br|li|tr)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_url_bytes(url: str, timeout_sec: int) -> bytes:
    """GET 요청으로 본문 바이트를 가져옵니다."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return resp.read()


def _xml_text(elem: Any) -> str:
    """ElementTree 노드의 직계 텍스트와 자식 tail을 이어 붙입니다."""
    if elem is None:
        return ""
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        parts.append(_xml_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def parse_rss_atom(xml_bytes: bytes, max_entries: int) -> list[dict[str, str]]:
    """RSS 2.0 / Atom entry에서 제목·요약·링크를 최대 max_entries개 추출합니다."""
    root = ElementTree.fromstring(xml_bytes)
    tag = lambda t: t.split("}")[-1] if "}" in t else t  # 네임스페이스 제거

    entries: list[dict[str, str]] = []

    root_name = tag(root.tag)
    if root_name == "rss":
        channel = root.find("channel")
        if channel is None:
            return entries
        items = channel.findall("item")[:max_entries]
        for item in items:
            title_el = item.find("title")
            desc_el = item.find("description")
            link_el = item.find("link")
            title = strip_html(_xml_text(title_el) if title_el is not None else "")
            summary = strip_html(_xml_text(desc_el) if desc_el is not None else "")
            link = (link_el.text or "").strip() if link_el is not None else ""
            if title or summary:
                entries.append({"title": title, "summary": summary, "link": link})
    elif root_name == "feed":
        # Atom
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns)[:max_entries]:
            title_el = entry.find("atom:title", ns)
            summary_el = entry.find("atom:summary", ns)
            if summary_el is None:
                summary_el = entry.find("atom:content", ns)
            link_el = entry.find("atom:link", ns)
            title = strip_html(_xml_text(title_el) if title_el is not None else "")
            summary = strip_html(_xml_text(summary_el) if summary_el is not None else "")
            link = ""
            if link_el is not None and link_el.get("href"):
                link = (link_el.get("href") or "").strip()
            if title or summary:
                entries.append({"title": title, "summary": summary, "link": link})

    return entries


def collect_corpus_from_feeds(
    urls: list[str],
    max_entries_per_feed: int,
    max_total_chars: int,
    timeout_sec: int,
) -> tuple[str, list[str], list[str]]:
    """피드를 순회해 하나의 본문 문자열과 성공 URL·실패 로그를 반환합니다."""
    chunks: list[str] = []
    ok_urls: list[str] = []
    errors: list[str] = []

    for url in urls:
        try:
            body = fetch_url_bytes(url, timeout_sec=timeout_sec)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            errors.append(f"{url}: {exc}")
            logger.warning("피드 요청 실패 %s — %s", url, exc)
            continue

        try:
            entries = parse_rss_atom(body, max_entries_per_feed)
        except ElementTree.ParseError as exc:
            errors.append(f"{url}: XML parse {exc}")
            logger.warning("피드 파싱 실패 %s — %s", url, exc)
            continue

        if not entries:
            errors.append(f"{url}: 항목 없음")
            continue

        ok_urls.append(url)
        for e in entries:
            line = e["title"]
            if e["summary"]:
                line = f"{line}\n{e['summary']}"
            if e["link"]:
                line = f"{line}\n{e['link']}"
            chunks.append(line.strip())

    corpus = "\n\n---\n\n".join(chunks)
    if len(corpus) > max_total_chars:
        corpus = corpus[:max_total_chars] + "\n\n[truncated]"

    return corpus, ok_urls, errors


def migrate_terms_row(row: dict[str, Any], fallback_date: str | None) -> dict[str, Any]:
    """구 스키마 행에 appearance_count·dates_seen을 채웁니다."""
    r = dict(row)
    if "appearance_count" not in r:
        r["appearance_count"] = 1
    ds = r.get("dates_seen")
    if not isinstance(ds, list) or not ds:
        added = r.get("added_at")
        if isinstance(added, str) and len(added) >= 10:
            r["dates_seen"] = [added[:10]]
        elif fallback_date:
            r["dates_seen"] = [fallback_date]
        else:
            r["dates_seen"] = []
    return r


def load_json(path: Path) -> dict[str, Any]:
    """기존 JSON 파일을 읽습니다. 없거나 비어 있으면 빈 구조를 반환합니다."""
    empty: dict[str, Any] = {
        "meta": {"schema_version": 2, "version": 1, "updated_at": None},
        "terms": [],
        "by_date": {},
    }
    if not path.is_file():
        return empty
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        logger.warning("JSON 손상으로 새 파일로 시작합니다: %s", path)
        return empty
    if "terms" not in data:
        data["terms"] = []
    if "meta" not in data:
        data["meta"] = {"version": 1, "updated_at": None}
    data["meta"].setdefault("schema_version", 2)
    data.setdefault("by_date", {})
    # 구 데이터 terms 마이그레이션
    fb = None
    lu = data["meta"].get("last_fetch_at") or data["meta"].get("updated_at")
    if isinstance(lu, str) and len(lu) >= 10:
        fb = lu[:10]
    data["terms"] = [migrate_terms_row(t, fb) for t in data["terms"] if isinstance(t, dict)]
    return data


def save_json(path: Path, data: dict[str, Any]) -> None:
    """JSON을 UTF-8로 예쁘게 저장합니다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build_extraction_prompt(corpus: str, max_terms: int) -> str:
    """수집 본문과 추출 상한을 넣은 사용자 메시지를 만듭니다."""
    return f"""아래 텍스트는 테크 관련 웹사이트의 RSS 피드에서 자동으로 모은
제목·요약·링크 모음입니다.

할 일:
1. 본문에 **실제로 등장**하거나, 본문이 다루는 주제와 **직접 연결**되는
   소프트웨어·인프라·보안·데이터·AI·하드웨어 등 **전문 테크 용어만** 고릅니다.
2. 본문과 무관한 일반 상식·유행어는 넣지 않습니다.
3. 서로 다른 용어를 최대 {max_terms}개까지 뽑습니다 (부족하면 있는 만큼만).

각 항목:
- term: 영문 표기 (약어는 그대로, 예: gRPC)
- meaning: 한국어로 작성. **IT 지식이 거의 없는 일반인**이 읽고도 이해할 수 있게
  **3~6문장 정도**로 자세히 설명합니다. 다음을 포함하세요:
  (1) 한 줄로 무엇인지,
  (2) 왜 쓰이거나 어디서 등장하는지(본문 맥락),
  (3) 익숙한 것에 비유하거나 일상·업무와 연결해 풀어쓰기.
  전문 용어를 쓸 때는 같은 문장 안에서 풀어서 설명하고, 영어 약어만 던지지 마세요.

반드시 JSON 객체만 출력하세요. 마크다운·코드펜스 금지.
{{"terms":[{{"term":"...","meaning":"..."}}]}}

--- 본문 시작 ---
{corpus}
--- 본문 끝 ---"""


def extract_terms_with_openai(
    client: OpenAI,
    model: str,
    corpus: str,
    max_terms: int,
) -> list[dict[str, str]]:
    """OpenAI로 본문에서 용어를 추출합니다.

    Args:
        client: OpenAI 클라이언트.
        model: 모델 이름.
        corpus: RSS에서 모은 본문.
        max_terms: 추출 상한.

    Returns:
        term, meaning 필드를 가진 dict 리스트.

    Raises:
        ValueError: 응답 형식이 잘못된 경우.
        RuntimeError: API 오류.
    """
    system = (
        "You extract technical glossary entries from the user's text. "
        "Output only a JSON object with key 'terms': array of objects with "
        "string fields 'term' (English) and 'meaning' (Korean). "
        "Each meaning must be a clear, layperson-friendly explanation in Korean "
        "(several sentences: what it is, why it matters in context, analogy or "
        "everyday hook; define any jargon you use). No markdown, no code fences."
    )
    user = build_extraction_prompt(corpus, max_terms)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0.4,
        )
    except Exception as exc:
        raise RuntimeError(f"OpenAI API 호출 실패: {exc}") from exc

    raw = response.choices[0].message.content
    if not raw:
        raise ValueError("모델 응답이 비어 있습니다.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 파싱 실패: {exc}") from exc

    terms = payload.get("terms")
    if not isinstance(terms, list):
        raise ValueError("응답에 'terms' 배열이 없습니다.")

    out: list[dict[str, str]] = []
    for item in terms:
        if not isinstance(item, dict):
            continue
        term = item.get("term")
        meaning = item.get("meaning")
        if isinstance(term, str) and isinstance(meaning, str):
            term, meaning = term.strip(), meaning.strip()
            if term and meaning:
                out.append({"term": term, "meaning": meaning})
    return out


def _merge_dates_seen(prev_dates: Any, day: str) -> list[str]:
    """dates_seen에 날짜 day를 넣고 정렬·중복 제거합니다."""
    base: list[str] = []
    if isinstance(prev_dates, list):
        base = [str(d) for d in prev_dates if isinstance(d, str) and len(d) >= 10]
    return sorted(set(base + [day]))


def dedupe_new_batch(new_items: list[dict[str, str]]) -> list[dict[str, str]]:
    """한 실행 안에서 같은 용어가 여러 번 나오면 마지막 의미만 남깁니다."""
    by_key: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for row in new_items:
        key = normalize_term_key(row["term"])
        if key not in by_key:
            order.append(key)
        by_key[key] = {"term": row["term"], "meaning": row["meaning"]}
    return [by_key[k] for k in order]


def merge_terms(
    existing: list[dict[str, Any]],
    new_items: list[dict[str, str]],
    run_day: str,
    now_iso: str,
) -> list[dict[str, Any]]:
    """기존 terms와 신규 항목을 병합하고 누적 등장 횟수·관측 날짜를 갱신합니다.

    이번 실행에서 같은 용어가 배치 안에 중복되면 등장은 1회로만 칩니다.

    Args:
        existing: 기존 용어 행 목록.
        new_items: 이번 실행에서 모델이 반환한 용어.
        run_day: UTC 기준 YYYY-MM-DD.
        now_iso: 갱신 시각 ISO8601.

    Returns:
        term 기준 정렬된 병합 결과.
    """
    by_key: dict[str, dict[str, Any]] = {}
    for row in existing:
        t = row.get("term")
        if isinstance(t, str) and t.strip():
            k = normalize_term_key(t)
            by_key[k] = migrate_terms_row(dict(row), run_day)

    batch = dedupe_new_batch(new_items)
    for row in batch:
        key = normalize_term_key(row["term"])
        prev = by_key.get(key, {})
        prev = migrate_terms_row(prev, run_day) if prev else {}
        prev_ac = int(prev.get("appearance_count", 0) or 0)
        by_key[key] = {
            "term": row["term"],
            "meaning": row["meaning"],
            "added_at": prev.get("added_at", now_iso),
            "updated_at": now_iso,
            "appearance_count": prev_ac + 1,
            "dates_seen": _merge_dates_seen(prev.get("dates_seen"), run_day),
        }
    return sorted(by_key.values(), key=lambda x: normalize_term_key(x["term"]))


def build_top_terms_by_appearance(terms: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """appearance_count가 큰 용어부터 요약 리스트를 만듭니다."""
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for t in terms:
        if not isinstance(t, dict):
            continue
        name = t.get("term")
        if not isinstance(name, str) or not name.strip():
            continue
        ac = int(t.get("appearance_count", 1) or 1)
        scored.append((ac, normalize_term_key(name), t))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out: list[dict[str, Any]] = []
    for ac, _, t in scored[:limit]:
        ds = t.get("dates_seen")
        last_d = ds[-1] if isinstance(ds, list) and ds else None
        out.append(
            {
                "term": t.get("term"),
                "appearance_count": ac,
                "last_seen_date": last_d,
            }
        )
    return out


def append_by_date_run(
    by_date: Any,
    day: str,
    run_at: str,
    term_names: list[str],
) -> dict[str, Any]:
    """by_date에 해당 일자의 실행 한 건을 추가합니다."""
    if not isinstance(by_date, dict):
        by_date = {}
    day_entry = by_date.get(day)
    if not isinstance(day_entry, dict):
        day_entry = {"runs": []}
    runs = day_entry.get("runs")
    if not isinstance(runs, list):
        runs = []
    runs.append(
        {
            "run_at": run_at,
            "extracted_terms": term_names,
        }
    )
    day_entry["runs"] = runs
    day_entry["last_run_at"] = run_at
    by_date[day] = day_entry
    return by_date


def run() -> None:
    """환경 변수·기본값으로 한 번 수집·추출·저장을 수행합니다."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.error("OPENAI_API_KEY가 비어 있습니다.")
        sys.exit(1)

    model = (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip()
    max_entries = _env_int("TECH_TERM_MAX_ENTRIES_PER_FEED", 20)
    max_chars = _env_int("TECH_TERM_MAX_INPUT_CHARS", 18000)
    max_terms = _env_int("TECH_TERM_MAX_EXTRACT", 25)
    timeout_sec = _env_int("TECH_TERM_HTTP_TIMEOUT_SEC", 30)
    feeds = _feed_urls_from_env()
    out_path = _output_path()

    corpus, ok_feeds, feed_errors = collect_corpus_from_feeds(
        feeds,
        max_entries_per_feed=max_entries,
        max_total_chars=max_chars,
        timeout_sec=timeout_sec,
    )
    if not corpus.strip():
        logger.error("피드에서 본문을 가져오지 못했습니다. %s", feed_errors)
        sys.exit(3)

    logger.info(
        "피드 %d곳 중 %d곳 성공, 본문 약 %d자 → OpenAI 추출 (상한 %d개)",
        len(feeds),
        len(ok_feeds),
        len(corpus),
        max_terms,
    )

    client = OpenAI(api_key=api_key)
    new_terms = extract_terms_with_openai(client, model, corpus, max_terms)
    if not new_terms:
        logger.warning("추출된 용어가 없습니다.")
        sys.exit(2)

    top_terms_limit = _env_int("TECH_TERM_TOP_FREQUENT_LIMIT", 40)

    data = load_json(out_path)
    run_day = _utc_date_str()
    now = _utc_now_iso()
    merged = merge_terms(data["terms"], new_terms, run_day, now)
    data["terms"] = merged

    batch_for_log = dedupe_new_batch(new_terms)
    term_names_run = [x["term"] for x in batch_for_log]
    data["by_date"] = append_by_date_run(
        data.get("by_date"),
        run_day,
        now,
        term_names_run,
    )

    data["meta"]["updated_at"] = now
    data["meta"]["last_fetch_at"] = now
    data["meta"]["feeds_ok"] = ok_feeds
    data["meta"]["feeds_errors"] = feed_errors
    data["meta"]["corpus_chars"] = len(corpus)
    data["meta"]["last_extract_count"] = len(new_terms)
    data["meta"]["last_run_day"] = run_day
    data["meta"]["model"] = model
    data["meta"]["top_terms_by_appearance"] = build_top_terms_by_appearance(
        merged,
        top_terms_limit,
    )

    save_json(out_path, data)
    logger.info(
        "저장 완료: %s (총 %d개 용어, 이번 실행 고유 용어 %d개, 일자 %s)",
        out_path,
        len(merged),
        len(batch_for_log),
        run_day,
    )


def main() -> None:
    """로깅 레벨만 환경 변수로 켠 뒤 run()을 호출합니다."""
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
