"""
job_trend_analyzer.py

채용 공고 데이터를 기반으로 기술 트렌드를 분석하는 스크립트입니다.
- 채용 리스트 페이지 크롤링
- 상세 페이지에서 기술 스택 및 요구사항 추출
- 통계 기반 키워드 추출
- OpenAI API를 활용한 키워드 설명 생성
- JSON 파일로 결과 저장

Author: ChatGPT
Date: 2026-03-19
"""

import os
import re
import json
import requests
from collections import Counter
from datetime import datetime
from typing import List, Dict

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
# 1. 채용 공고 리스트 수집
# ==============================
def get_job_links(limit: int = 10) -> List[str]:
    """
    채용 리스트 페이지에서 상세 페이지 링크를 수집합니다.

    Args:
        limit (int): 가져올 공고 수

    Returns:
        List[str]: 공고 상세 페이지 URL 리스트
    """
    url = "https://www.wanted.co.kr/wdlist/518"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")

        # TODO: selector는 실제 페이지 구조에 따라 수정 필요
        links = [
            BASE_URL + a["href"]
            for a in soup.select("a.JobCard_link__")[:limit]
            if a.get("href")
        ]

        return links

    except Exception as e:
        print(f"[ERROR] 링크 수집 실패: {e}")
        return []


# ==============================
# 2. 상세 페이지 크롤링
# ==============================
def get_job_detail(url: str) -> str:
    """
    채용 상세 페이지에서 텍스트 정보를 추출합니다.

    Args:
        url (str): 상세 페이지 URL

    Returns:
        str: 공고 텍스트 (title + description + requirement)
    """
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")

        # TODO: selector 실제 구조에 맞게 수정 필요
        title = soup.select_one("h1")
        description = soup.select_one("div.JobDescription_JobDescription__")

        text = ""

        if title:
            text += title.text + "\n"

        if description:
            text += description.text

        return text

    except Exception as e:
        print(f"[ERROR] 상세 페이지 실패: {e}")
        return ""


# ==============================
# 3. 텍스트 수집
# ==============================
def collect_job_texts(limit: int = 10) -> str:
    """
    여러 채용 공고에서 텍스트를 수집하여 하나로 합칩니다.

    Args:
        limit (int): 수집할 공고 수

    Returns:
        str: 전체 텍스트
    """
    links = get_job_links(limit)

    texts = []
    for link in links:
        detail_text = get_job_detail(link)

        if detail_text:
            texts.append(detail_text)

    return "\n".join(texts)


# ==============================
# 4. 키워드 추출 (통계 기반)
# ==============================
def extract_keywords(text: str, top_n: int = 10) -> List[str]:
    """
    텍스트에서 기술 키워드를 추출합니다.

    Args:
        text (str): 입력 텍스트
        top_n (int): 상위 키워드 개수

    Returns:
        List[str]: 키워드 리스트
    """
    # 단어 추출 (영문 + 특수문자 일부)
    words = re.findall(r"[A-Za-z+#]+", text)

    # 불용어 제거
    stopwords = {
        "the", "and", "for", "with", "you", "are",
        "this", "that", "from", "have", "will"
    }

    filtered = [
        w.lower() for w in words
        if len(w) > 2 and w.lower() not in stopwords
    ]

    counter = Counter(filtered)

    # TODO: python vs Python 같은 normalization 추가 가능
    keywords = [word for word, _ in counter.most_common(top_n)]

    return keywords


# ==============================
# 5. LLM 설명 생성
# ==============================
def generate_explanations(keywords: List[str]) -> List[Dict]:
    """
    키워드에 대한 설명을 생성합니다.

    Args:
        keywords (List[str]): 키워드 리스트

    Returns:
        List[Dict]: 설명 포함 결과
    """
    prompt = f"""
다음 기술 키워드 각각에 대해 왜 현재 많이 사용되는지 간단히 설명하세요.

키워드:
{keywords}

JSON 형식으로 응답:
[
  {{"word": "", "explanation": ""}}
]
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
        )

        result = json.loads(response.choices[0].message.content)
        return result

    except Exception as e:
        print(f"[ERROR] LLM 실패: {e}")

        # fallback
        return [{"word": k, "explanation": ""} for k in keywords]


# ==============================
# 6. DB 저장
# ==============================
def update_db(data: List[Dict]) -> None:
    """
    결과를 JSON DB에 저장합니다.

    Args:
        data (List[Dict]): 저장할 데이터
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

    # 최신 30일 유지
    db = db[-30:]

    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

    print("[INFO] DB 업데이트 완료")


# ==============================
# 메인 실행
# ==============================
def main():
    """
    전체 파이프라인 실행
    """
    print("[INFO] 채용 데이터 수집 시작")

    text = collect_job_texts(limit=10)

    if not text:
        print("[WARN] 수집된 텍스트 없음")
        return

    print("[INFO] 키워드 추출")
    keywords = extract_keywords(text, top_n=5)

    print(f"[INFO] 키워드: {keywords}")

    print("[INFO] 설명 생성")
    result = generate_explanations(keywords)

    update_db(result)


if __name__ == "__main__":
    main()
