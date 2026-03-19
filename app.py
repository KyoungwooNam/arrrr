"""
job_trend_llm_pipeline.py

Wanted API를 사용하여 채용 공고 URL을 자동 수집하고,
각 URL을 LLM으로 분석하여 기술 키워드를 추출 후 집계하는 스크립트

- URL 자동 수집 (API 기반)
- 상세 페이지 크롤링
- URL 단위 LLM 키워드 추출
- 전체 키워드 집계
- JSON DB 저장

Author: ChatGPT
Date: 2026-03-19
"""

import os
import json
import time
import requests
from typing import List, Dict
from datetime import datetime
from collections import Counter

from bs4 import BeautifulSoup
from openai import OpenAI


# ==============================
# 환경 설정
# ==============================
API_KEY = os.environ.get("AI_API_KEY")
client = OpenAI(api_key=API_KEY)

DB_FILE = "data.json"
BASE_URL = "https://www.wanted.co.kr"


# ==============================
# 1. 채용 URL 자동 수집 (API)
# ==============================
def get_job_urls(total: int = 40, step: int = 20) -> List[str]:
    """
    Wanted API에서 채용 공고 URL 자동 수집

    Args:
        total (int): 총 수집 개수
        step (int): 한 번에 가져올 개수

    Returns:
        List[str]: URL 리스트
    """
    all_urls = []

    for offset in range(0, total, step):
        url = "https://www.wanted.co.kr/api/v4/jobs"

        params = {
            "country": "kr",
            "job_sort": "job.latest_order",
            "limit": step,
            "offset": offset,
        }

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }

        try:
            res = requests.get(url, params=params, headers=headers, timeout=10)
            jobs = res.json().get("data", [])

            urls = [
                f"{BASE_URL}/wd/{job['id']}"
                for job in jobs if job.get("id")
            ]

            all_urls.extend(urls)

        except Exception as e:
            print(f"[ERROR] API 실패: {e}")

        time.sleep(0.5)  # rate limit 대응

    # 중복 제거
    return list(set(all_urls))


# ==============================
# 2. 상세 페이지 텍스트 추출
# ==============================
def fetch_job_text(url: str) -> str:
    """
    채용 공고 페이지에서 텍스트 추출

    Args:
        url (str): 공고 URL

    Returns:
        str: 텍스트
    """
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        res = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, "html.parser")

        title = soup.select_one("h1")
        description = soup.select_one("div")

        text = ""

        if title:
            text += title.text + "\n"

        if description:
            text += description.text

        return text[:4000]  # 토큰 제한

    except Exception as e:
        print(f"[ERROR] 크롤링 실패: {url} | {e}")
        return ""


# ==============================
# 3. LLM 키워드 추출
# ==============================
def extract_keywords_llm(text: str) -> List[str]:
    """
    LLM을 사용하여 기술 키워드 추출

    Args:
        text (str): 채용 텍스트

    Returns:
        List[str]: 키워드 리스트
    """
    prompt = f"""
다음 채용 공고에서 기술 스택/프레임워크/도구만 추출하세요.

조건:
- soft skill 제외 (협업, 커뮤니케이션 등)
- 일반 단어 제외
- 최대 5개
- 영어 기준

텍스트:
{text}

JSON 배열 형식으로 출력:
["python", "aws", "docker"]
"""

    try:
        response = client.chat.completions.create(
            model="gpt-5.4-nano",
            messages=[{"role": "user", "content": prompt}],
        )

        result = json.loads(response.choices[0].message.content)
        return list(set(result))  # 중복 제거

    except Exception as e:
        print(f"[ERROR] LLM 실패: {e}")
        return []


# ==============================
# 4. 키워드 집계
# ==============================
def aggregate_keywords(all_keywords: List[List[str]]) -> Dict[str, int]:
    """
    전체 키워드 빈도 집계

    Args:
        all_keywords (List[List[str]])

    Returns:
        Dict[str, int]
    """
    counter = Counter()

    for keywords in all_keywords:
        counter.update([k.lower() for k in keywords])

    return dict(counter.most_common())


# ==============================
# 5. DB 저장
# ==============================
def update_db(data: Dict[str, int]) -> None:
    """
    JSON 파일에 결과 저장

    Args:
        data (Dict[str, int])
    """
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
    else:
        db = []

    new_entry = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "keywords": data
    }

    db.append(new_entry)
    db = db[-30:]

    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

    print("[INFO] DB 저장 완료")


# ==============================
# 메인 실행
# ==============================
def main():
    """
    전체 파이프라인 실행
    """
    print("[INFO] URL 수집 시작")

    urls = get_job_urls(total=40)

    print(f"[INFO] 수집된 URL 수: {len(urls)}")

    all_keywords = []

    for i, url in enumerate(urls, 1):
        print(f"[INFO] ({i}/{len(urls)}) 처리 중: {url}")

        text = fetch_job_text(url)

        if not text:
            continue

        keywords = extract_keywords_llm(text)

        print(f"[INFO] 키워드: {keywords}")

        all_keywords.append(keywords)

        time.sleep(1)  # rate limit

    aggregated = aggregate_keywords(all_keywords)

    print("[INFO] 최종 결과:")
    print(aggregated)

    update_db(aggregated)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
