#!/usr/bin/env python3
# Copyright 2025
"""테크 도메인 맥락을 프롬프트로 제시하고 OpenAI로 용어·의미를 추출해 JSON에 병합 저장합니다.

웹 스크래핑이나 외부 용어 API 없이, 모델이 알고 있는 테크 생태계 지식만으로
배치 단위로 용어를 생성합니다. GitHub Actions에서 주기 실행하거나 로컬에서
`.env`의 `OPENAI_API_KEY`로 테스트할 수 있습니다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

# 프로젝트 루트 기준으로 .env 로드 (스크립트 위치와 무관하게 동작)
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

logger = logging.getLogger(__name__)

# 프롬프트에 넣을 "테크 맥락" — 실제 HTTP 요청 없이 모델이 참고할 주제만 나열
_DEFAULT_THEMES = (
    "Hacker News, Stack Overflow, GitHub, MDN Web Docs, "
    "Kubernetes·클라우드(AWS/GCP/Azure) 문서, Linux·시스템 프로그래밍, "
    "데이터베이스·SQL, 머신러닝·MLOps, 보안(CVE, OWASP), "
    "프론트엔드·백엔드 프레임워크 생태계"
)


def _utc_now_iso() -> str:
    """현재 시각을 UTC ISO8601 문자열로 반환합니다."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_term_key(term: str) -> str:
    """병합 시 중복 판별용 키(소문자·앞뒤 공백 제거)."""
    return term.strip().lower()


def load_json(path: Path) -> dict[str, Any]:
    """기존 JSON 파일을 읽습니다. 없거나 비어 있으면 빈 구조를 반환합니다."""
    if not path.is_file():
        return {"meta": {"version": 1, "updated_at": None}, "terms": []}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        logger.warning("JSON 손상으로 새 파일로 시작합니다: %s", path)
        return {"meta": {"version": 1, "updated_at": None}, "terms": []}
    if "terms" not in data:
        data["terms"] = []
    if "meta" not in data:
        data["meta"] = {"version": 1, "updated_at": None}
    return data


def save_json(path: Path, data: dict[str, Any]) -> None:
    """JSON을 UTF-8로 예쁘게 저장합니다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build_user_prompt(batch_size: int, themes: str) -> str:
    """모델에게 줄 사용자 메시지(한국어 의미·JSON만 출력 요구)."""
    return f"""다음은 실제로 웹을 조회하지 않고, 당신이 알고 있는 지식만으로
답해야 하는 주제 범위입니다: {themes}

위 맥락에서 소프트웨어·인프라·보안·데이터·AI 등 **전문 테크 용어**를
서로 겹치지 않게 {batch_size}개 뽑아 주세요.

각 항목에 대해:
- term: 영문 표기(필요하면 약어 병기, 예: "gRPC")
- meaning: 한국어로 한 문단 이내로 정의·용도 설명

반드시 아래 JSON 객체만 출력하세요. 다른 텍스트·코드펜스는 금지입니다.
{{"terms":[{{"term":"...","meaning":"..."}},...]}}"""


def fetch_terms_from_openai(
    client: OpenAI,
    model: str,
    batch_size: int,
    themes: str,
) -> list[dict[str, str]]:
    """OpenAI Chat Completions로 용어 배치를 받아 파싱합니다.

    Args:
        client: OpenAI 클라이언트.
        model: 모델 이름.
        batch_size: 이번에 추가할 용어 개수.
        themes: 프롬프트에 넣을 테크 맥락 설명.

    Returns:
        term, meaning 키를 가진 dict 리스트.

    Raises:
        ValueError: 응답 JSON이 기대 스키마와 다를 때.
        RuntimeError: API 오류 시.
    """
    system = (
        "You are a precise technical glossary assistant. "
        "Output only valid JSON object with key 'terms' (array of objects "
        "with string fields 'term' and 'meaning'). No markdown."
    )
    user = build_user_prompt(batch_size, themes)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0.7,
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


def merge_terms(
    existing: list[dict[str, Any]],
    new_items: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """기존 terms와 신규 항목을 term 기준으로 병합(같은 키는 새 meaning으로 덮어씀)."""
    by_key: dict[str, dict[str, Any]] = {}
    for row in existing:
        t = row.get("term")
        if isinstance(t, str) and t.strip():
            by_key[normalize_term_key(t)] = dict(row)

    now = _utc_now_iso()
    for row in new_items:
        key = normalize_term_key(row["term"])
        prev = by_key.get(key, {})
        by_key[key] = {
            "term": row["term"],
            "meaning": row["meaning"],
            "added_at": prev.get("added_at", now),
            "updated_at": now,
        }
    # 정렬: term 기준 안정 정렬
    return sorted(by_key.values(), key=lambda x: normalize_term_key(x["term"]))


def run(
    output_path: Path,
    batch_size: int,
    themes: str,
    model: str | None,
) -> None:
    """환경 변수에서 API 키를 읽고 한 배치를 수집해 파일에 반영합니다."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.error("OPENAI_API_KEY가 비어 있습니다. .env를 설정하세요.")
        sys.exit(1)

    resolved_model = (model or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip()
    client = OpenAI(api_key=api_key)

    logger.info("모델=%s, 배치=%d건 수집 시작", resolved_model, batch_size)
    new_terms = fetch_terms_from_openai(client, resolved_model, batch_size, themes)
    if not new_terms:
        logger.warning("수집된 용어가 없습니다. 종료합니다.")
        sys.exit(2)

    data = load_json(output_path)
    merged = merge_terms(data["terms"], new_terms)
    data["terms"] = merged
    data["meta"]["updated_at"] = _utc_now_iso()
    data["meta"]["last_batch_size"] = len(new_terms)
    data["meta"]["model"] = resolved_model

    save_json(output_path, data)
    logger.info("저장 완료: %s (총 %d개 용어)", output_path, len(merged))


def parse_args() -> argparse.Namespace:
    """CLI 인자를 파싱합니다."""
    p = argparse.ArgumentParser(
        description="OpenAI로 테크 용어를 생성해 JSON에 병합 저장합니다.",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=_ROOT / "data" / "tech_terms.json",
        help="저장할 JSON 경로 (기본: data/tech_terms.json)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=15,
        help="이번 실행에서 추가할 용어 개수 (기본: 15)",
    )
    p.add_argument(
        "--themes",
        type=str,
        default=_DEFAULT_THEMES,
        help="프롬프트에 넣을 테크 맥락 설명 문자열",
    )
    p.add_argument(
        "--model",
        type=str,
        default=None,
        help="모델명 (미지정 시 환경변수 OPENAI_MODEL 또는 gpt-4o-mini)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="디버그 로그 출력",
    )
    return p.parse_args()


def main() -> None:
    """진입점: 로깅 설정 후 run 호출."""
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    run(
        output_path=args.output.resolve(),
        batch_size=args.batch_size,
        themes=args.themes,
        model=args.model,
    )


if __name__ == "__main__":
    main()
