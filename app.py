import os
import json
import datetime

def update_database():
    db_file = 'data.json'
    
    # 1. 기존 데이터 불러오기 (없으면 빈 리스트)
    if os.path.exists(db_file):
        with open(db_file, 'r', encoding='utf-8') as f:
            db = json.load(f)
    else:
        db = []

    # 2. 데이터 수집 및 AI 분석 (여기선 예시 데이터)
    # 실제 구현 시 여기서 requests로 채용공고를 긁고 AI API를 호출합니다.
    today_data = {
        "date": str(datetime.date.today()),
        "keywords": [
            {"word": "Docker", "desc": "컨테이너화 기술로 채용 공고 점유율 15% 상승"},
            {"word": "FastAPI", "desc": "최근 파이썬 백엔드에서 급부상 중인 프레임워크"}
        ]
    }

    # 3. 데이터 누적
    db.append(today_data)

    # 4. 저장 (최신 30일치만 유지하는 등의 로직 추가 가능)
    with open(db_file, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    update_database()
