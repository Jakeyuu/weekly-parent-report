# Input / Output Schema

## App Metadata

These values are entered in the HTML app, not repeated in the uploaded Excel:

| field | example |
|---|---|
| academy | 정승효 국어 연구소 |
| reportTitle | 경기외고2 내신 성취 분석 리포트 |
| periodType | 주간 |
| courseType | 내신 |
| period | 2026년 5월 4주차 |
| className | 경기외고2 내신반 |
| courseName | 경기외고2 내신 수업 |
| teacher | 정승효 |
| testName | 260523 경기외고2 내신 진단평가 |
| testDate | 2026.05.23 |

## Sheet: 학생요약

Student-level input. One row per student.

| column | required | type | notes |
|---|---:|---|---|
| 이름 | yes | string | key for MVP matching |
| 학교 | yes | string | used for display and later matching |
| 학년 | yes | string | used for display and later matching |
| 수업유형 | yes | enum | 내신 / 정규. The app-selected mode overrides this column for report rendering |
| 진단평가_맞은개수 | yes | number | 0 to total |
| 진단평가_총문항수 | yes | number | positive integer |
| 반평균_맞은개수 | yes | number | same scale as score |
| 숙제1_제목 | yes | string | assignment title |
| 숙제1_수행률 | yes | number | 0 to 100 |
| 숙제2_제목 | yes | string | assignment title |
| 숙제2_수행률 | yes | number | 0 to 100 |
| 클리닉참여 | yes | enum | 참여 / 미참여. Blank, `-`, and old `해당없음` values are normalized to 미참여 |
| 모의고사숙제 | conditional | enum | 정규 mode only: O / X. Blank, `-`, and old `해당없음` values are normalized to X / 미참여. Ignored in 내신 mode |
| 모의고사점수 | no | number/string | 정규 mode only: optional score. Ignored in 내신 mode |
| 모의고사등급 | no | number/string | 정규 mode only: optional grade. Ignored in 내신 mode |
| 교사메모 | no | string | reference for comments |

## Sheet: 문항분석

Question-level input. One row per student/question.

| column | required | type | notes |
|---|---:|---|---|
| 이름 | yes | string | must match `학생요약.이름` |
| 문항번호 | yes | number/string | displayed in report |
| 정오 | yes | enum | O / X / △ / - |
| 영역 | yes | string | 독해 / 문학 / 문법 / 어휘 / 쓰기 / 기타 recommended |
| 난이도 | yes | enum | 하 / 중 / 상 |
| 단원 | yes | string | work/text/unit |
| 보완포인트 | yes | string | short parent-facing phrase |

## Output JSON

Top-level:

```json
{
  "reportType": "weekly",
  "metadata": {
    "academy": "정승효 국어 연구소",
    "reportTitle": "경기외고2 내신 성취 분석 리포트",
    "periodType": "주간",
    "courseType": "내신",
    "period": "2026년 5월 4주차",
    "className": "경기외고2 내신반",
    "courseName": "경기외고2 내신 수업",
    "teacher": "정승효",
    "testName": "260523 경기외고2 내신 진단평가",
    "testDate": "2026.05.23"
  },
  "classSummary": {
    "studentCount": 30,
    "averageScore": 19.1,
    "averageAccuracy": 68.2,
    "averageHomework": 54.5,
    "clinicParticipationCount": 14
  },
  "students": [],
  "warnings": []
}
```

Student object:

```json
{
  "name": "강준서",
  "school": "경기외고",
  "grade": "고2",
  "metrics": {
    "score": 8,
    "total": 28,
    "accuracy": 28.6,
    "classAverageScore": 19.1,
    "classAverageAccuracy": 68.2,
    "differenceFromClassAverage": -39.6,
    "homeworkAverage": 6.5,
    "clinicParticipation": "미참여",
    "courseType": "내신",
    "mockExamHomework": {"status": "-", "score": "", "grade": ""},
    "incorrectCount": 18,
    "partialCount": 1,
    "missingCount": 0,
    "domainAccuracy": {"독해": 42.9},
    "difficultyAccuracy": {"상": 18.2}
  },
  "questions": [
    {
      "number": 1,
      "result": "O",
      "domain": "독해",
      "difficulty": "중",
      "unit": "이생규장전",
      "point": "내용 일치 판단"
    }
  ],
  "analysis": {
    "overallStatus": "집중 관리",
    "priorityQuestions": [9, 10, 12, 13, 14],
    "weakDomains": ["문학", "문법"],
    "strongDomains": ["어휘"],
    "weakDifficulties": ["상", "중"],
    "homeworkStatus": "보완 필요",
    "clinicStatus": "미참여"
  },
  "comments": {
    "summary": "...",
    "strength": "...",
    "weakness": "...",
    "nextAction": "...",
    "homeGuide": "...",
    "clinicComment": "..."
  },
  "teacherMemo": "핵심 지문 이해와 선택지 판단 보완 필요",
  "warnings": []
}
```
