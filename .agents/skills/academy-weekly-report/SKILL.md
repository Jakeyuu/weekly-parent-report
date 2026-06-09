---
name: academy-weekly-report
description: "국어학원 주간 학부모 성취 리포트용 엑셀 입력파일을 분석해 학생별 정답률, 반평균 비교, 숙제 수행률, 클리닉 참여, 영역/난이도별 성취율, 종합상태, 학부모용 코멘트 초안을 JSON으로 생성할 때 사용한다. HTML 리포트 편집기, 주간 결과 엑셀 저장, 월간/학기/연간 상위 리포트의 기초 데이터 생성을 지원한다."
---

# Academy Weekly Report

## Purpose

Convert a weekly Korean academy input workbook into structured report data for an HTML-based parent report editor. The skill is an analysis engine, not the final renderer: it reads the input Excel, computes metrics, drafts parent-facing comments, and writes JSON that the app can preview, edit, save, and export as PNG/PDF.

Default input is the weekly template with:

- `학생요약`: one row per student
- `문항분석`: one row per student/question

Do not require phone numbers or student IDs in the MVP. Match `문항분석` to `학생요약` by `이름`; if duplicate names appear, mark `warnings`.

## Default Workflow

1. Read workbook sheets `학생요약` and `문항분석`.
2. Validate required columns and normalize values.
3. Compute student metrics:
   - course type (`내신` or `정규`)
   - score accuracy
   - class-average accuracy
   - difference from class average
   - homework average
   - clinic participation
   - regular-class mock exam homework status, score, and grade
   - incorrect/partial/missing question counts
   - domain accuracy
   - difficulty accuracy
4. Decide `overallStatus`.
5. Draft parent-facing report comments:
   - `summary`
   - `strength`
   - `weakness`
   - `nextAction`
   - `homeGuide`
   - `clinicComment`
6. Save output JSON for the HTML app.

Use the bundled script when possible:

```bash
python scripts/analyze_weekly_report.py input.xlsx output.json \
  --academy "정승효 국어 연구소" \
  --report-title "경기외고2 내신 성취 분석 리포트" \
  --period-type "주간" \
  --period "2026년 5월 4주차" \
  --class-name "경기외고2 내신반" \
  --course-name "경기외고2 내신 수업" \
  --teacher "정승효" \
  --test-name "260523 경기외고2 내신 진단평가" \
  --test-date "2026.05.23"
```

If the user wants to revise business rules or JSON shape, update `SKILL.md` and the script together.

## Input Contract

See `references/input-output-schema.md` for the exact input/output schema.

Core `학생요약` columns:

- `이름`
- `학교`
- `학년`
- `수업유형`
- `진단평가_맞은개수`
- `진단평가_총문항수`
- `반평균_맞은개수`
- `숙제1_제목`
- `숙제1_수행률`
- `숙제2_제목`
- `숙제2_수행률`
- `클리닉참여`
- `모의고사숙제`
- `모의고사점수`
- `모의고사등급`
- `교사메모`

Core `문항분석` columns:

- `이름`
- `문항번호`
- `정오`
- `영역`
- `난이도`
- `단원`
- `보완포인트`

Value rules:

- Homework rates are numbers from `0` to `100`, without `%`.
- `수업유형`: `내신`, `정규`. The app's selected mode (`--course-type`) is authoritative; this column is kept for template readability and future filtering only.
- `클리닉참여`: `참여`, `미참여`, `해당없음`.
- `모의고사숙제`: `O`, `X`, `-`. Used only when the selected report mode is `정규`; ignored in `내신` mode even if values are present.
- `모의고사점수`, `모의고사등급`: optional; in `정규` mode, display as `점수(등급)` when `모의고사숙제=O`. Ignored in `내신` mode.
- `정오`: `O`, `X`, `△`, `-`.
- Recommended `영역`: `독해`, `문학`, `문법`, `어휘`, `쓰기`, `기타`.
- Recommended `난이도`: `하`, `중`, `상`.

## Status Rules v0.1

Let:

- `accuracy = 진단평가_맞은개수 / 진단평가_총문항수`
- `classAccuracy = 반평균_맞은개수 / 진단평가_총문항수`
- `homeworkAverage = average(숙제 수행률 values)`, 0 to 100

Initial `overallStatus`:

- `우수`: `accuracy >= 0.8` and `homeworkAverage >= 80`
- `양호`: `accuracy >= classAccuracy` and `homeworkAverage >= 70`
- `보통`: `accuracy >= classAccuracy - 0.10`
- `집중 관리`: `accuracy < 0.4` or `homeworkAverage < 30`
- `관리 필요`: all other cases

If rules conflict, prefer the more urgent status in this order:

`집중 관리` > `관리 필요` > `보통` > `양호` > `우수`

## Comment Style

Write in Korean, parent-facing, concise, and non-alarming. Use objective phrases:

- `확인됨`
- `필요합니다`
- `권장합니다`
- `진행하겠습니다`

Avoid:

- blaming the student or parent
- overly definitive diagnoses
- casual slang
- saying AI generated the comment

Mention clinic participation when useful:

- `참여`: "이번 주 클리닉에 참여하여 오답 보완 기회를 확보했습니다."
- `미참여`: "이번 주 클리닉 미참여로 오답 보완 기회가 부족했습니다."
- `해당없음`: omit or write neutral guidance.

For `정규` reports, use the clinic slot as a compact `모의고사 숙제` section instead:

- `O`: show O and include score/grade if present.
- `X`: show X and guide mock-exam completion/review.
- `-`: show neutral confirmation-needed guidance.

Use `교사메모` as feedback guidance, not as part of the overall evaluation summary:

- Do not append `교사메모` to `comments.summary`.
- Reflect `교사메모` in `comments.nextAction` or another 학습 피드백 field when it gives a concrete learning direction.
- Keep the wording parent-facing, e.g. "담당 메모를 반영해 ... 부분을 함께 점검하겠습니다."

## Output Contract

The output JSON must be stable for an HTML editor:

```json
{
  "reportType": "weekly",
  "metadata": {},
  "classSummary": {},
  "students": [
    {
      "name": "강준서",
      "metrics": {},
      "questions": [],
      "analysis": {},
      "comments": {},
      "warnings": []
    }
  ],
  "warnings": []
}
```

The HTML app owns rendering, comment editing, local/server save, PNG/PDF export, and whole-class export. This skill only produces analysis data and draft comments.

## Quality Check

Before finalizing:

- Confirm all required sheets and columns exist.
- Confirm all student names in `문항분석` exist in `학생요약`; otherwise add warnings.
- Confirm `students.length` matches `학생요약` row count.
- Confirm score and homework values are numeric and bounded.
- Confirm duplicate names are warned.
- Confirm all students have `overallStatus`, `summary`, `strength`, `weakness`, `nextAction`, and `homeGuide`.
- Confirm JSON is UTF-8 and `ensure_ascii=false` style when possible.
