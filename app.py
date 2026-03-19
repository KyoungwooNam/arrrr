import os
import json
import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from datetime import datetime

# 1. 환경 설정
client = OpenAI(api_key=os.environ.get("AI_API_KEY")) # GitHub Secrets에서 가져옴
DB_FILE = 'data.json'

def get_job_titles():
    """채용 사이트에서 최근 공고 제목들을 수집합니다."""
    print("데이터 수집 시작...")
    # 예시: 특정 채용 페이지 (실제 사이트 구조에 따라 selector 수정 필요)
    url = "https://www.wanted.co.kr/wdlist/518" # 개발 전체
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        # 실제 사이트의 공고 제목 태그를 찾아야 합니다.
        titles = [t.text for t in soup.select('strong.JobCard_title__dd9_M')[:20]] 
        return ", ".join(titles)
    except Exception as e:
        print(f"수집 중 오류 발생: {e}")
        return "Python, Docker, Kubernetes, React, AWS, FastAPI" # 실패 시 샘플 데이터

def analyze_with_ai(text):
    """OpenAI API를 사용하여 키워드를 추출하고 설명을 생성합니다."""
    print("AI 분석 중...")
    prompt = f"""
    다음은 최근 IT 채용 공고 제목들입니다: {text}
    이 중에서 가장 자주 등장하거나 중요한 기술 키워드 3개를 뽑아주세요.
    결과는 반드시 아래와 같은 JSON 형식으로만 응답하세요:
    [
        {{"word": "키워드1", "explanation": "이 기술이 왜 많이 쓰이는지에 대한 짧은 설명"}},
        {{"word": "키워드2", "explanation": "..."}}
    ]
    """
    
    response = client.chat.completions.create(
        model="gpt-3.5-turbo", # 또는 gpt-4
        messages=[{"role": "user", "content": prompt}],
        response_format={ "type": "json_object" }
    )
    
    # AI 응답 파싱
    result = json.loads(response.choices[0].message.content)
    # 결과가 리스트가 아닌 객체로 올 경우를 대비
    return result if isinstance(result, list) else result.get("keywords", [])

def update_db(new_keywords):
    """결과를 data.json에 누적 저장합니다."""
    if os.path.exists(DB_FILE):
        with shadow_open := open(DB_FILE, 'r', encoding='utf-8'):
            db = json.load(shadow_open)
    else:
        db = []

    new_entry = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "keywords": new_keywords
    }
    
    db.append(new_entry)
    
    # 최신 30일 데이터만 유지 (파일 크기 관리)
    db = db[-30:]
    
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    print("DB 업데이트 완료!")

def main():
    job_text = get_job_titles()
    keywords = analyze_with_ai(job_text)
    update_db(keywords)

if __name__ == "__main__":
    main()
