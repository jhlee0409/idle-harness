# Multi-Agent Coding Harness

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
cd harness
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run (requires Claude CLI login)
python3 orchestrator.py "카드 뽑기 애니메이션과 AI 해석이 있는 타로 리딩 웹 앱"
```

결과물은 `output/{product-name}/`에 생성됩니다.

## Prerequisites

- Python 3.11+
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) (OAuth 로그인 완료)
- Node.js 18+ (Generator가 프론트엔드 빌드에 사용)
- Chrome (Evaluator가 chrome-devtools MCP로 테스트)

## Project Structure

```
harness/
├── orchestrator.py      # 메인 오케스트레이션 루프
├── cli.py               # Claude Agent SDK 래퍼
├── config.py            # 설정값 (평가 기준 임계값, 서버 설정)
├── state.py             # 상태 관리 (status.json)
├── server.py            # dev server 기동/종료 (프로세스 그룹 관리)
├── agents/
│   ├── planner.md       # Planner 시스템 프롬프트
│   ├── generator.md     # Generator 시스템 프롬프트
│   └── evaluator.md     # Evaluator 시스템 프롬프트
├── tests/               # pytest 테스트
└── output/              # 생성된 애플리케이션들 (gitignored)
```

## How It Works

1. **Plan** — Planner가 프롬프트를 제품 사양서로 확장 (비주얼 디자인 언어 포함)
2. **Build** — Generator가 전체 풀스택 앱을 한 세션에서 구현
3. **Evaluate** — Evaluator가 브라우저로 실행 중인 앱을 테스트, 스크린샷 증거 수집
4. **Iterate** — FAIL 시 피드백과 함께 Generator에게 반환, 최대 3라운드 반복

## Configuration

`config.py`에서 수정 가능:

| Setting | Default | Description |
|---------|---------|-------------|
| `max_build_attempts` | 3 | Build→Evaluate 최대 반복 횟수 |
| `dev_server_url` | `http://localhost:5173` | 프론트엔드 서버 URL |
| `mcp_tool` | `chrome-devtools` | Evaluator 브라우저 테스트 도구 |

## License

MIT
