# Idle Harness

GAN(적대적 생성 신경망) 구조에서 영감을 받은 3-에이전트 자율 코딩 시스템. 짧은 프롬프트 하나로 풀스택 애플리케이션을 자동 생성합니다.

> Based on [Anthropic's harness design for long-running apps](https://www.anthropic.com/engineering/harness-design-long-running-apps)

## Architecture

```
User Prompt (1-4 sentences)
    ↓
┌─────────┐     ┌───────────┐     ┌───────────┐
│ Planner │ ──→ │ Generator │ ←─→ │ Evaluator │
│         │     │           │     │           │
│ Spec    │     │ React     │     │ Browser   │
│ Design  │     │ Vite      │     │ Testing   │
│ Language│     │ FastAPI   │     │ Screenshot│
│         │     │ SQLite    │     │ Grading   │
└─────────┘     └───────────┘     └───────────┘
                      ↕
              Build → Evaluate → Feedback Loop (max 3 rounds)
```

### Agents

| Agent | Role | Key Behavior |
|-------|------|-------------|
| **Planner** | 프롬프트 → 제품 사양서 확장 | 비주얼 디자인 언어 정의, AI 기능 기회 탐색, 기술 세부사항 배제 |
| **Generator** | 사양서 기반 풀스택 구현 | React+Vite+FastAPI+SQLite, 자체 검증 후 핸드오프 |
| **Evaluator** | 실행 중인 앱을 브라우저로 테스트 | 소스 코드 안 읽음 (GAN 원칙), 스크린샷 증거, 4가지 기준 평가 |

### Evaluation Criteria

| Criterion | Weight | Description |
|-----------|--------|-------------|
| Design Quality | High | 전체가 조화로운 하나로 느껴지는가? |
| Originality | High | 템플릿 기본값이 아닌 의도적 디자인 선택이 보이는가? |
| Craft | Normal | 타이포, 간격, 색상 조화, 명암비 |
| Functionality | Normal | 핵심 인터랙션이 end-to-end로 작동하는가? |

## Quick Start

```bash
# Setup
pip install -r requirements.txt

# Run (requires Claude CLI login)
python3 orchestrator.py "카드 뽑기 애니메이션과 AI 해석이 있는 타로 리딩 웹 앱"
```

결과물은 `output/{product-name}/`에 생성됩니다.

## Prerequisites

- Python 3.11+
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) (OAuth 로그인 완료)
- Node.js 18+ (Generator가 프론트엔드 빌드에 사용)
- Playwright MCP (Evaluator가 브라우저 테스트에 사용)

## Project Structure

```
idle-harness/
├── orchestrator.py      # 메인 오케스트레이션 루프
├── cli.py               # Claude Agent SDK 래퍼
├── config.py            # 설정값 (모드, 서버, 제한)
├── state.py             # 상태 관리 (status.json)
├── server.py            # dev server 기동/종료
├── sprint.py            # 스프린트 파싱
├── agents/
│   ├── planner.md       # Planner 시스템 프롬프트
│   ├── generator.md     # Generator 시스템 프롬프트
│   └── evaluator.md     # Evaluator 시스템 프롬프트
├── tests/               # pytest 테스트
└── output/              # 생성된 애플리케이션들
```

## How It Works

1. **Plan** — Planner가 프롬프트를 제품 사양서로 확장 (비주얼 디자인 언어 포함)
2. **Negotiate** — Generator와 Evaluator가 스프린트 계약 협상
3. **Build** — Generator가 풀스택 앱을 구현 (연속 세션으로 컨텍스트 유지)
4. **Evaluate** — Evaluator가 Playwright로 실행 중인 앱 테스트, 스크린샷 증거 수집
5. **Iterate** — FAIL 시 피드백과 함께 Generator에게 반환, 최대 3라운드 반복

## Configuration

`config.py`에서 수정 가능:

| Setting | Default | Description |
|---------|---------|-------------|
| `mode` | `full` | `full` (스프린트+계약+반복) / `simple` (단일 빌드+평가) |
| `max_build_attempts` | `3` | Build→Evaluate 최대 반복 횟수 |
| `max_negotiation_rounds` | `3` | 계약 협상 최대 라운드 |
| `generator_max_turns` | `200` | Generator 최대 턴 수 |
| `dev_server_url` | `http://localhost:5173` | 프론트엔드 서버 URL |
| `mcp_tool` | `playwright` | Evaluator 브라우저 테스트 도구 |

## License

MIT
