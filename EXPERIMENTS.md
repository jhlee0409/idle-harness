# Harness Quality Experiments

하네스 출력 품질을 극한으로 끌어올리기 위한 실험 목록.
각 실험은 독립적으로 실행 가능하며, 결과를 비교해서 어떤 변경이 실제로 품질을 올리는지 측정한다.

---

## 1. Evaluator 보정 (Calibration) — 거짓 PASS 잡기

Evaluator가 제대로 FAIL을 내리는지 검증. 의도적으로 품질이 낮은 앱을 만들어 Evaluator에게 넘긴다.

```bash
# Step 1: 간단한 앱을 빌드 (1회만, 재시도 없이)
python orchestrator.py "Build a simple todo app with categories and due dates"

# Step 2: 빌드된 앱에서 핵심 기능을 의도적으로 망가뜨림
cd output/*/
# 예: API 엔드포인트 삭제, CSS 전부 제거, DB 연결 끊기
git stash  # 원본 보관

# Step 3: Evaluator만 다시 돌려서 FAIL이 나오는지 확인
# (orchestrator에 evaluate-only 모드 추가 필요 — 아래 Experiment 7 참조)
```

**측정**: Evaluator가 의도적 결함을 몇 % 잡는지. 목표 >95%.

---

## 2. 난이도별 벤치마크 스위트

5개 난이도의 앱을 순차적으로 빌드하여 하네스 한계를 찾는다.

```bash
# Level 1: Static (프론트만, DB 없음)
python orchestrator.py "Build a personal portfolio site with project gallery, about page, and contact form with form validation"

# Level 2: CRUD (기본 풀스택)
python orchestrator.py "Build a recipe sharing platform where users can create, browse, and save recipes with ingredients, steps, and photos"

# Level 3: Real-time + AI
python orchestrator.py "Build a collaborative mood board app where multiple users can add images, text, and colors to shared boards with AI-powered palette suggestions"

# Level 4: Complex State (다중 엔티티, 관계)
python orchestrator.py "Build a project management tool like a mini Linear with issues, sprints, kanban board, timeline view, and team member assignment"

# Level 5: Full Product (인증 + 실시간 + AI + 복합 UI)
python orchestrator.py "Build a music production DAW in the browser with a multi-track timeline, synthesizer with ADSR envelope, drum machine with step sequencer, mixer with EQ and effects, and AI-powered melody generation"
```

**측정**: 각 레벨에서 첫 PASS까지 걸린 attempt 수, 총 비용($), 총 시간, 최종 criteria pass rate.

---

## 3. Evaluator Criteria 품질 실험

같은 spec에 대해 criteria 생성을 3회 반복하여 일관성과 깊이를 비교.

```bash
# 먼저 spec만 생성
python -c "
import anyio
from orchestrator import Orchestrator
orch = Orchestrator()
orch.setup()
anyio.run(orch.plan, 'Build an expense tracker with receipt scanning, budget categories, and monthly reports')
"

# criteria 3회 생성 (각각 다른 파일에 저장)
for i in 1 2 3; do
  cp comms/testable_criteria.md "comms/criteria_run_${i}.md" 2>/dev/null
  python -c "
import anyio
from orchestrator import Orchestrator
orch = Orchestrator()
orch._spec = open('comms/spec.md').read()
orch.output_dir = 'output/test'
import os; os.makedirs(orch.output_dir, exist_ok=True)
criteria_path = anyio.run(orch.generate_criteria)
import shutil; shutil.copy(criteria_path, 'comms/criteria_run_${i}.md')
print(f'Run ${i}: $(grep -c \"^\- \[\" comms/criteria_run_${i}.md) criteria')
"
done

# 비교
diff comms/criteria_run_1.md comms/criteria_run_2.md | head -50
wc -l comms/criteria_run_*.md
```

**측정**: criteria 수 분산, 겹치는 criteria 비율, edge case 포함 여부.

---

## 4. Simple vs Full 모드 품질 대결

동일 프롬프트로 두 모드를 돌려 비교.

```bash
# Simple mode (기본)
python orchestrator.py "Build a kanban board with drag-and-drop, multiple boards, card labels, and due date reminders"
mv output/ output_simple/

# Full mode
# config.py에서 mode를 "full"로 변경 후:
sed -i '' 's/"mode": "simple"/"mode": "full"/' config.py
python orchestrator.py "Build a kanban board with drag-and-drop, multiple boards, card labels, and due date reminders"
mv output/ output_full/

# 원복
sed -i '' 's/"mode": "full"/"mode": "simple"/' config.py

# 비교: 파일 수, 커밋 수, 코드 라인 수, 비용
echo "=== Simple ===" && find output_simple/ -name "*.ts" -o -name "*.tsx" -o -name "*.py" | xargs wc -l | tail -1
echo "=== Full ===" && find output_full/ -name "*.ts" -o -name "*.tsx" -o -name "*.py" | xargs wc -l | tail -1
```

**측정**: 코드량, attempt 수, 비용, 최종 평가 점수, 디자인 품질.

---

## 5. Design Refinement 효과 측정

디자인 정제 루프가 실제로 품질을 올리는지 검증.

```bash
# config에서 max_design_iterations를 0으로 → 디자인 루프 비활성화
# 같은 프롬프트로 2회 실행 (with/without design refinement)

# Without design refinement
python -c "
from config import CONFIG
CONFIG['max_design_iterations'] = 0
" # (실제로는 config.py 수정 필요)

python orchestrator.py "Build a tarot card reading app with mystical dark theme, card animations, and AI-powered interpretations"

# 결과 저장
cp -r output/ output_no_design/
cp -r comms/ comms_no_design/

python orchestrator.py clean

# With design refinement (기본값 10)
python orchestrator.py "Build a tarot card reading app with mystical dark theme, card animations, and AI-powered interpretations"
cp -r output/ output_with_design/
cp -r comms/ comms_with_design/

# 평가 비교
diff comms_no_design/sprints/sprint-1/evaluation.md comms_with_design/sprints/sprint-1/evaluation.md
```

**측정**: Design Quality/Originality PASS 비율, iteration 수, 추가 비용 대비 품질 향상.

---

## 6. Generator 프롬프트 A/B 테스트

Generator 시스템 프롬프트의 핵심 섹션을 변경하여 출력 품질 비교.

```bash
# Variant A: 현재 프롬프트 (baseline)
cp agents/generator.md agents/generator_baseline.md
python orchestrator.py "Build a weather dashboard with 5-day forecast, interactive map, and severe weather alerts"
mv output/ output_variant_a/

python orchestrator.py clean

# Variant B: 디자인 지시를 더 구체적으로 강화
# agents/generator.md의 Rule 9 (Design with intention) 섹션을 수정
# 예: "Before writing ANY CSS, write a 5-line design brief..."
python orchestrator.py "Build a weather dashboard with 5-day forecast, interactive map, and severe weather alerts"
mv output/ output_variant_b/

# 원복
cp agents/generator_baseline.md agents/generator.md
```

**측정**: 디자인 PASS 비율, 첫 attempt PASS 비율, 코드 품질.

---

## 7. [구현 필요] evaluate-only 모드

기존 빌드를 재평가할 수 있는 모드. Evaluator 보정과 회귀 테스트에 필수.

```bash
# 제안 커맨드:
python orchestrator.py eval output/my-app/
# → 서버 시작 → Evaluator 실행 → 결과 출력 (빌드 없이)
```

**구현**: orchestrator.py에 `_cmd_eval(output_dir)` 추가.
- spec을 `output_dir/.harness/spec.md`에서 읽음
- criteria를 `output_dir/.harness/testable_criteria.md`에서 읽음
- 서버 시작 → evaluate() 호출 → 결과 출력

---

## 8. [구현 필요] 자동 벤치마크 러너

위 실험들을 자동화하는 스크립트.

```bash
# 제안 커맨드:
python orchestrator.py benchmark --suite levels
# → Level 1~5 순차 실행
# → 각 결과를 benchmarks/{timestamp}/ 에 저장
# → 최종 리포트: attempt 수, 비용, 시간, pass rate 테이블

python orchestrator.py benchmark --suite design
# → 같은 프롬프트 3회 실행, 디자인 점수 분산 측정
```

**구현**: benchmarks/ 디렉토리에 결과 누적, 시계열 비교 가능.

---

## 9. Evaluator 엄격도 튜닝

automation-limited 비율 임계값(현재 10%)을 조정하여 최적값 찾기.

```bash
# 현재: >10% automation-limited → PASS 오버라이드를 FAIL
# 실험: 5%, 10%, 15%, 20%로 변경하여 각각 실행

for threshold in 5 10 15 20; do
  echo "=== Threshold: ${threshold}% ==="
  # config.py 또는 orchestrator.py의 0.10을 변경
  # python orchestrator.py "complex app prompt"
  # 결과 기록
done
```

**측정**: 각 임계값에서 거짓 PASS/거짓 FAIL 비율.

---

## 10. [구현 필요] Cost-Quality 프론티어

비용 vs 품질의 파레토 프론티어를 그린다.

```bash
# 변수: max_build_attempts (1, 3, 5, 10), generator_max_turns (50, 100, 200)
# 같은 프롬프트로 조합별 실행

for attempts in 1 3 5 10; do
  for turns in 50 100 200; do
    echo "attempts=${attempts} turns=${turns}"
    # config 수정 후 실행
    # 비용, 시간, 최종 pass rate 기록
  done
done

# 결과를 CSV로 → 비용 대비 품질 그래프
```

**목표**: "이 앱은 $X에 Y% 품질로 빌드 가능" 예측 모델.

---

## 실험 결과

### Experiment A: Sonnet Evaluator (2026-04-07)

대상 빌드: `mise-en-place-a-recipe-sharing-platform` (162 criteria)

| 항목 | Opus (baseline, eval 4) | Sonnet |
|---|---|---|
| 시간 | 35m 14s | ~39m 20s (크래시) |
| Turns | 188 | 390+ |
| Criteria tested | 162 | 155 |
| Pass rate | PASS | 148/155 (95.5%) PASS |
| Evidence Rules 위반 | 0 | **12건** |
| 결과 | 정상 종료 | MCP 크래시 (exit code 1) |

**결론**: Evaluator는 Opus 유지. Sonnet은:
- Turn당 빠르지만 2x 더 많은 turn → 총 시간 동일 또는 더 느림
- Evidence Rules (banned phrases) 무시 — `confirmed via JS`, `verified by agent` 등 12회 사용
- 39분 후 MCP Playwright 크래시

**조치**: `model_evaluator: None` (Opus fallback). Planner만 Sonnet 유지.

---

### Experiment B: Opus Evaluator + 강화된 프롬프트 (2026-04-07~08)

대상 빌드: `mise-en-place-a-recipe-sharing-platform` (동일 빌드)
목적: 아티클 원칙 검증 — Verifier 미들웨어 없이 Evaluator만으로 품질 보장 가능한지

| 항목 | 이전 Opus (eval 4, PASS) | Sonnet (PASS) | **이번 Opus (FAIL)** |
|---|---|---|---|
| Verdict | PASS | PASS (크래시) | **FAIL** |
| Banned phrases | 미측정 | 12건 | **0건** |
| Feature pass rate | 미측정 | 148/155 (95.5%) | **89/111 (80.2%)** |
| AI stub 판정 | PASS | PASS | **FAIL** |
| 시간 | 35m 14s | 39m (크래시) | **47m 9s** |
| Turns | 188 | 390 | **322** |
| 비용 | $18.99 | 미측정 | **$16.95** |

감지된 문제 6개 (이전 PASS에서 놓친 것들):
1. 허브 그린 dietary tags 누락
2. 쿠킹 모드 재료 패널 누락
3. 노이즈 텍스처 오버레이 누락
4. 폼 submit 로딩 상태 누락
5. AI 어시스턴트 미완성
6. 빈 상태 페이지 누락

**결론: 아티클 원칙이 맞다.**
- Opus + 엄격한 프롬프트 = banned phrases 0건, AI stub FAIL 감지
- 같은 앱이 이전엔 PASS, 이제는 80.2%로 FAIL → 프롬프트 품질이 핵심
- Verifier 미들웨어 불필요. `verifier_enabled: False`로 비활성화.
- Evaluator 프롬프트 반복 개선이 가장 높은 레버리지.

---

### Experiment C: GAN 루프 수렴 테스트 (2026-04-08)

대상 빌드: `mise-en-place-a-recipe-sharing-platform` (Experiment B에서 FAIL 받은 동일 빌드)
목적: FAIL 피드백 → Generator 수정 → 재평가를 반복하면 PASS에 도달하는지

| Eval | Build | Pass Rate | Verdict | Build 비용 | Eval 비용 | Eval 시간 |
|------|-------|-----------|---------|-----------|-----------|-----------|
| #6 | - | 89/111 (80.2%) | FAIL | - | $16.95 | 47m |
| #7 | #5 (15m, $7.64) | 103/113 (91.2%) | FAIL | $7.64 | $16.35 | 50m |
| #8 | #6 (?, $?) | ? | FAIL | ~$7 | $13.61 | 53m |
| **#9** | **#7** | **107/120 (89%)** | **PASS** | **~$8** | **$17.47** | **40m** |

**FAIL → PASS까지 3 사이클.** 80.2% → 91.2% → FAIL → **PASS (89%)**

PASS 평가 품질:
- Banned phrases: **0건**
- Screenshot 증거: **107/120 criteria**
- Automation-limited: **2건** (file_upload 패턴, 정당한 예외)
- AI 기능: **실제 동작 확인** (구조화된 레시피 생성 + APPLY TO FORM)
- Evidence Audit 섹션: **포함됨**

**결론:**
1. GAN 루프가 동작한다. Evaluator FAIL 피드백이 Generator를 개선시킨다.
2. 3사이클 (build 15-20min + eval 40-50min = ~60min/cycle) × 3 = ~3시간으로 PASS 도달.
3. 아티클 원칙 최종 확인: 엄격한 Evaluator 프롬프트 + Opus = 미들웨어 없이 품질 보장.
4. Verifier 미들웨어는 불필요. `verifier_enabled: False` 유지.

---

### Experiment D: wax-music-playlist-curator 실패 분석 + 피드백 루프 강화 (2026-04-08)

대상: `wax-music-playlist-curator` (Level 5, 149 criteria)
결과: **10회 FAIL, $146, 7시간.** Build 10에서 AgentError 크래시.

#### 근본 원인 6개

| # | 원인 | 영향 |
|---|------|------|
| 1 | **evaluation.md 덮어쓰기 버그** | Evaluator가 Write 도구로 상세 평가를 디스크에 작성 → 오케스트레이터가 에이전트 텍스트 응답(요약)으로 덮어씀. 4/9 평가에서 상세 피드백 손실 |
| 2 | **Generator 컨텍스트 포화** | 연속 세션 9회 → build_9는 14분 1턴 $7.02. 실질적 작업 불가 |
| 3 | **Generator 자기평가 거짓말** | 매번 149/149 (100%) 주장. Evaluator는 10~105개 실패 발견 |
| 4 | **회귀 감지 없음** | build_4가 앱 완전 크래시 (빈 화면). 스모크 테스트 있었으면 즉시 감지 |
| 5 | **테스트 불가 기준** | 애니메이션 타이밍, 블러 전환은 Playwright로 검증 불가 |
| 6 | **Evaluator 불일치** | 매번 다른 수의 기준 테스트 (84, 146, 148, 94개) |

#### 수정 사항 (5건)

1. **evaluation.md 보존 로직** — 에이전트 응답보다 디스크 버전이 더 상세하면(criteria 수 비교) 디스크 버전 유지
2. **스모크 테스트** — 빌드 후 HTTP 헬스체크. 실패 시 Evaluator 스킵, 크래시 피드백 직접 전달
3. **회귀 감지 + 세션 리셋** — eval 점수 추적. 최고점 대비 >20pp 하락 또는 3연속 하락 시 Generator 세션 리셋
4. **자기평가 정직성 검증** — 자기평가 vs 마지막 Evaluator 점수 교차 비교. 불일치 시 로그 경고 + Generator 프롬프트에 점수 히스토리 주입
5. **eval 점수 히스토리** — state.json에 점수 이력 저장. Generator 프롬프트에 포함시켜 자기기만 방지

#### 테스트 추가 (12건)

- `test_parse_eval_score_*` (3): 점수 파싱
- `test_evaluate_preserves_disk_eval_*` (2): 디스크 평가 보존
- `test_smoke_test_fail_skips_eval` (1): 스모크 테스트
- `test_regression_resets_generator_session` (1): 회귀 감지
- `test_no_regression_keeps_session` (1): 정상 시 세션 유지
- `test_downward_trend_resets_session` (1): 3연속 하락
- `test_state_eval_score_tracking` (1): 상태 추적
- `test_self_eval_discrepancy_logged` (1): 정직성 검증
- `test_build_prompt_includes_score_history` (1): 프롬프트 주입

**기대 효과**: wax-music 시나리오에서:
- 원인 1 (덮어쓰기): eval_2 후 build_3 퇴보 방지 → 초기 수렴 가속
- 원인 2 (컨텍스트 포화): build_7 이후 세션 리셋 → 실질적 작업 재개
- 원인 4 (크래시): eval_4 빈 화면 → 스모크 테스트로 즉시 감지, Evaluator $3.23 절약
- 보수적 추정: 10회 → 5~6회 FAIL 후 PASS, $146 → ~$80

---

## 실행 우선순위

| 순위 | 실험 | 이유 | 예상 비용 |
|------|------|------|-----------|
| 1 | #2 벤치마크 스위트 | 현재 한계를 가장 빨리 파악 | $50-100 |
| 2 | #7 eval-only 모드 구현 | 다른 실험의 기반 인프라 | 코드만 |
| 3 | #1 Evaluator 보정 | 거짓 PASS가 가장 큰 품질 리스크 | $10-20 |
| 4 | #5 Design refinement 효과 | 비용 대비 효과 검증 | $30-50 |
| 5 | #4 Simple vs Full | 모드 선택 근거 확보 | $40-80 |
