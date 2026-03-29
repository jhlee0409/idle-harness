# Multi-Agent Coding Harness Design Spec

## Overview

GAN(적대적 생성 신경망) 구조에서 영감을 받은 3-에이전트 자율 코딩 시스템. 사람의 개입 없이 장기적이고 복잡한 애플리케이션을 자율적으로 개발한다.

**참고:** [Anthropic - Harness Design for Long-Running Apps](https://www.anthropic.com/engineering/harness-design-long-running-apps)

## Architecture

### Agents

| Agent | Role | Input | Output |
|-------|------|-------|--------|
| Planner | 사용자 프롬프트 → 제품 사양서 확장 | 1~4문장 프롬프트 | `comms/spec.md` |
| Generator | 사양서 기반 코드 구현 | `spec.md`, `feature_contract.md`, `evaluation.md` | 코드 (`output/`), `feature_contract.md`, git commits |
| Evaluator | 실행 중인 앱을 동적 테스트 | `feature_contract.md`, 실행 앱 (chrome-devtools) | `evaluation.md`, 스크린샷 |

### Communication

에이전트 간 직접 통신 없음. 모든 소통은 `comms/` 디렉터리의 파일 읽기/쓰기.

### Execution

각 에이전트는 `claude --print` CLI 서브프로세스로 호출. 호출마다 독립 컨텍스트.

## Directory Structure

```
harness/
├── orchestrator.py          # 메인 오케스트레이터
├── config.py                # 설정 (임계값, MCP 권한, 서버 명령)
├── agents/
│   ├── planner.md           # Planner 시스템 프롬프트
│   ├── generator.md         # Generator 시스템 프롬프트
│   └── evaluator.md         # Evaluator 시스템 프롬프트
├── comms/                   # 에이전트 간 통신 (런타임 생성)
│   ├── spec.md              # Planner → Generator (제품 사양서)
│   ├── feature_contract.md  # Generator ↔ Evaluator (기능 계약)
│   ├── evaluation.md        # Evaluator → Generator (평가 결과)
│   ├── screenshots/         # Evaluator 증거 스크린샷
│   └── status.json          # 전체 상태 추적
└── output/                  # Git 초기화된 애플리케이션 코드
```

## Orchestration Flow

```
1. 사용자 프롬프트 입력
       ↓
2. Planner 호출 → spec.md 생성
       ↓
3. spec.md에서 기능 목록 추출
       ↓
┌──── 기능 루프 (feature N) ────────────────────┐
│  4. Generator → feature_contract.md 작성 (제안)│
│       ↓                                        │
│  5. Evaluator → feature_contract.md 검토 (협상)│
│       합의 실패? → 4로 (최대 2회)               │
│       ↓                                        │
│  6. Generator → 코드 구현 + 자체 검증 + commit  │
│       ↓                                        │
│  7. 오케스트레이터 → dev server 기동            │
│       ↓                                        │
│  8. Evaluator → 앱 동적 테스트 + evaluation.md  │
│       오케스트레이터 → dev server 종료          │
│       ↓                                        │
│     FAIL? → 6으로 (최대 3회)                    │
│     PASS? → 다음 기능으로                       │
│     3회 연속 FAIL → 사용자에게 판단 위임         │
└────────────────────────────────────────────────┘
       ↓
9. 최종 통합 평가 (Evaluator, 최대 2회 재시도)
       ↓
10. 완료 리포트 출력
```

## Agent Prompts Design

### Planner Agent

**목표:** 짧은 프롬프트를 완전한 제품 사양서로 확장.

**제약:**
- 제품의 목적, 타겟 사용자, 핵심 기능 목록, UX 흐름에 집중
- 기술 스택이나 구현 방법은 절대 명시하지 않음 (연쇄 오류 방지)
- AI 기능 통합 기회를 적극 탐색하여 명시
- 각 기능에 우선순위(P0/P1/P2) 부여
- `## Features` 섹션 아래 파싱 가능한 형식으로 작성

**출력 형식:** `comms/spec.md`
```markdown
# [Product Name]
## Vision
## Target Users
## Features
### P0: [Feature Name]
- Description: ...
- User Story: ...
- Acceptance Criteria: ...
### P1: [Feature Name]
...
## AI Integration Opportunities
## UX Flow
```

### Generator Agent

**목표:** 사양서 기반으로 기능 단위 코드 구현.

**제약:**
- 코드 작성 전 반드시 `feature_contract.md`에 계약 제안 작성
- 기술 스택은 스스로 분석/결정 (기존 코드가 있으면 그에 맞춤)
- 구현 후 자체 1차 검증 (빌드 성공, 기본 동작 확인)
- 기능 단위로 의미 있는 git commit
- 평가 피드백 수신 시 Required Changes 중심 수정, 관련 코드 변경 허용, 스코프 외 추가 금지
- 첫 기능 구현 완료 시 `comms/dev_server.json`에 서버 시작/종료 명령 기록 (오케스트레이터가 읽어서 config에 반영)

**feature_contract.md 형식:**
```markdown
## Feature: [Name]
## Generator Proposal
### Deliverables
- ...
### Testable Checklist
- [ ] [구체적이고 검증 가능한 항목]
### Tech Approach (brief)
- ...
## Evaluator Review
### Status: AGREED / NEEDS_REVISION
### Modifications: ...
```

### Evaluator Agent

**목표:** 실행 중인 앱을 동적 테스트하고 비판적 평가.

**제약:**
- chrome-devtools MCP로 앱을 직접 조작하며 테스트
- 코드를 읽지 않음 — 실행 중인 앱만 테스트 (GAN 원칙)
- 계약 검토 시: 검증 가능한 기준인지, 누락된 엣지 케이스 비판적 검토
- 체크리스트 통과율이 1차 판정 기준
- 스크린샷 증거 필수 (`comms/screenshots/`)
- FAIL 시 구체적 Required Changes + 수정 맥락 제공

**evaluation.md 형식:**
```markdown
## Feature: [Name]
## Attempt: N/3

### Contract Checklist
- [x] 항목 설명
- [ ] 실패 항목 ← FAIL

### Checklist Pass Rate: X/Y (Z%)

### Design Assessment
| 기준 | 판정 | 근거 | 증거 |
|------|------|------|------|
| 디자인 품질 | PASS/FAIL | ... | screenshots/... |
| 독창성 | PASS/FAIL | ... | screenshots/... |
| 완성도 | PASS/FAIL | ... | screenshots/... |
| 기능성 | PASS/FAIL | ... | screenshots/... |

### Verdict: PASS / FAIL
### Required Changes (if FAIL)
1. ...
### Context for Generator
- ...
```

## Agent Tool Permissions

| Agent | Allowed Tools |
|-------|--------------|
| Planner | `Read`, `Write` (comms/spec.md) |
| Generator | `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep` (output/ + git) |
| Evaluator | `Read`, `Write` (comms/evaluation.md, comms/screenshots/), `chrome-devtools` MCP 전체 |

## Config

`config.py`에 포함되는 설정:

```python
CONFIG = {
    "max_contract_negotiations": 2,
    "max_implementation_attempts": 3,
    "dev_server_start_cmd": None,    # Generator가 첫 기능 구현 시 자동 감지하여 설정
    "dev_server_stop_cmd": None,    # (예: package.json의 "dev" script → "npm run dev")
    "dev_server_url": "http://localhost:5173",
    "dev_server_startup_wait": 5,    # seconds
    "output_dir": "output",
    "comms_dir": "comms",
    "mcp_tool": "chrome-devtools",   # 향후 playwright로 교체 가능
}
```

## status.json

```json
{
  "phase": "implementing",
  "current_feature_index": 1,
  "features": [
    {
      "name": "사용자 인증",
      "priority": "P0",
      "contract_status": "agreed",
      "implementation_status": "passed",
      "attempts": 1
    },
    {
      "name": "대시보드",
      "priority": "P0",
      "contract_status": "pending",
      "implementation_status": "pending",
      "attempts": 0
    }
  ],
  "integration_test": {
    "status": "pending",
    "attempts": 0
  }
}
```

## Key Design Decisions

1. **tech_guide.md 제거** — Planner의 과도한 기술 개입 방지. 연쇄 오류 차단.
2. **기능 단위 평가** — 인위적 스프린트 대신 자연스러운 기능 경계로 평가 루프 구성.
3. **체크리스트 기반 평가** — 자의적 점수 대신 계약 체크리스트 통과율 + PASS/FAIL 판정.
4. **스크린샷 증거** — Evaluator의 주장에 시각적 근거 첨부.
5. **오케스트레이터의 서버 관리** — dev server 기동/종료를 오케스트레이터가 담당.
6. **Git 통합** — output/ 자동 git init, Generator에게 commit 권한.
7. **사용자 판단 위임** — 3회 연속 FAIL 시 조기 종료 대신 사용자에게 질문.
