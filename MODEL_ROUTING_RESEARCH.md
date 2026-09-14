# Codex·Claude 모델/노력 수준 자동 선택 조사

확인: 2026-09-10 09:17 KST. 작업 폴더: `C:\Users\dsaka\Desktop\Segye Casefile`.

사용자 요청은 코딩 도구의 모델 라우팅 조사와 적용 검토다. Segye Casefile의 기사 생성 모델 교체나 보류 중인 모델 비교 실행 요청으로 해석하지 않았다. 공식 문서와 도구 제작자의 저장소/소스를 조사하고 로컬 버전·선택된 설정 필드·Codex 프로토콜 스키마만 확인했다. 설치, 업데이트, 사용자 설정 변경, 서비스 조작, 실제 모델 평가 호출은 수행하지 않았다.

**판단**

기억한 “설계 Opus, 구현 Sonnet”은 Claude Code 공식 `opusplan`에 해당한다. 최신에는 필요한 순간만 상위 모델에게 자문하는 `advisor`도 있다. 질문마다 주 모델을 바꾸는 라우터, 작업 단계에 따른 전환, 하위 에이전트 분담, 모델 내부의 적응형 추론은 서로 다른 기능이다. 적용 목표를 구분해야 효과를 판단할 수 있다.

우선순위는 Claude 공식 단계/역할 분담 → Codex 공식 하위 에이전트 분담 → 필요하면 CCR의 제한된 프로필 시험이다. LiteLLM/OpenRouter는 여러 API를 묶어 쓰려는 경우의 후보다. 단순 권고를 위해 매번 입력을 막는 훅은 사용자의 중단·재입력 최소화 요구에 맞지 않는다. 이는 이번 조사에 따른 제안이며 사용자 확정이나 적용 완료가 아니다.

**현재 PC에서 확인한 범위**

| 항목 | 관찰 |
|---|---|
| Codex CLI | `codex --version`: `0.153.4` |
| Codex 사용자 기본값 | `C:\Users\dsaka\.codex\config.toml`: `gpt-6-astra`, effort `ultra`, `multi_agent = true` |
| Claude Code CLI | `claude --version`: `2.1.114` |
| Claude 사용자 기본값 | `C:\Users\dsaka\.claude\settings.json`: `claude-opus-4-8`, effort `xhigh` |
| 기존 Claude 훅 | `PostToolUse`, `UserPromptSubmit`; 활성 플러그인 목록에 Ponytail |
| Codex 현재 버전의 프로토콜 | 생성한 `TurnStartParams.json`에 `model`, `effort` 재정의가 있고 이후 턴에도 적용된다는 설명이 있음. `TurnSteerParams.json`에는 두 필드가 없음 |

위 모델은 파일에 저장된 기본값이다. 별도의 실행 중 Claude 세션이나 모든 작업의 실제 모델을 조회한 결과는 아니다. 인증 파일·키 값은 읽지 않았다. 최신 Claude 공식 문서는 Opus 4.8에 v2.1.154 이상이 필요하다고 설명하므로 저장된 모델과 CLI 버전 사이에 점검할 차이가 있다. 실제 호출 오류를 재현한 것은 아니다. [Claude 모델 설정](https://code.claude.com/docs/en/model-config)

**도구별 확인과 한계**

| 방법 | 확인된 기능 | 적용 판단 |
|---|---|---|
| Claude `opusplan` | Plan Mode는 Opus, 실행은 Sonnet | 기억한 패턴을 가장 직접적으로 제공. 문장 난이도를 매번 분류하는 기능은 아님 |
| Claude `advisor` | 주 모델이 작업하다 필요한 시점에 상위 모델 자문 | Sonnet 작업 + Opus 판단에 적합한 실험 기능. 로컬 버전/계정 지원 확인 필요 |
| Claude/Codex subagent | 역할에 모델·effort 지정, 주 에이전트가 위임 | 별도 게이트웨이 없이 자동 작업 분담. 주 대화 모델 자체는 유지 |
| `claude-model-router-hook` | 규칙/Haiku 분류, 권고·설정 기록, 하위 호출 라우팅 | 현재 입력을 막고 재전송을 요구할 수 있어 우선 추천 제외 |
| Claude Code Router(CCR) | 요청 조건·모델/파라미터 변경·사용자 분류·fallback | Windows와 Codex 앱 지원을 명시. 실제 PC 연결 검증 전 후보 |
| LiteLLM Auto Routing | 휴리스틱/LLM/키워드/의미 기반 분류, 모델·effort tier | API 기반 운영에 적합. 현재 Beta |
| OpenRouter Auto | 작업 유형 분류 후 비용 tier·허용 모델·사용량 순위로 선택 | 관리형 API 후보. 구독 안에서 자동 절약하는 훅과 다름 |

Claude `opusplan`은 `claude --model opusplan`처럼 선택한다. Plan Mode 진입·종료를 따라 모델이 바뀐다. `/effort auto`는 모델 기본값으로 돌아가는 명령이며 질문별 effort 분류기를 켜는 명령이 아니다. Adaptive reasoning은 설정된 effort 안에서 사고량을 조절한다. [모델·effort 공식 문서](https://code.claude.com/docs/en/model-config)

Claude `advisor`의 공식 예시는 `claude --model sonnet --advisor opus`다. 접근법 결정, 반복 오류, 완료 점검에서 Claude가 자문 시점을 선택한다. 구독 계정과 API 결제 계정 모두 지원하며 구독은 사용 한도를 소모한다. 자문 모델은 전체 대화를 읽고 자문 간 자체 캐시를 재사용하지 않아 항상 더 저렴하다고 보장할 수 없다. 최신 문서의 desktop/headless `/advisor` 명령은 v2.1.260 이상을 요구한다. advisor 전체 도입 최소 버전은 여기서 확정하지 않았고 로컬 2.1.114에서 실행하지 않았다. `--advisor`는 원래 help에 숨겨져 있으므로 help에 없다는 사실만으로 미지원이라고 단정하지 않는다. [Advisor 공식 문서](https://code.claude.com/docs/en/advisor)

역할별 하위 에이전트는 Claude의 `model`·`effort` frontmatter, Codex의 `agents.<name>.config_file` 및 모델/effort 설정을 이용한다. Claude가 역할 설명에 따라 위임하도록 구성할 수 있다. 기존 Ponytail 훅에 내용을 중복 주입하지 않고 역할 정책만 별도로 명확히 하는 편이 간단하다. 전체 대화 모델을 바꾸는 기능은 아니므로 주 모델 비용까지 모두 줄지는 않는다. [Claude subagents](https://code.claude.com/docs/en/sub-agents), [Codex 설정](https://learn.chatgpt.com/docs/config-file/config-reference)

Codex 공식 `UserPromptSubmit` 출력은 문맥 추가·입력 차단을 제공하며, 현재 문서의 공개 계약에는 주 모델/effort 교체 필드가 없다. 반면 App Server의 `turn/start`는 `model`·`effort`를 받으며 같은 작업의 이후 턴에도 유지한다. 따라서 입력을 받는 별도 클라이언트가 분류 후 이 값을 지정하면 질문별 전환을 구현할 수 있다. 진행 중 턴에 입력을 추가하는 `turn/steer`에는 이 재정의가 없다. 문서와 설치 버전의 생성 스키마를 모두 대조했다. 일반 훅이나 AGENTS.md의 “쉬우면 저렴한 모델 사용” 한 줄이 앱의 주 모델을 자동 교체한다고 보장할 수 없다. [Codex hooks](https://learn.chatgpt.com/docs/hooks#userpromptsubmit), [App Server](https://learn.chatgpt.com/docs/app-server)

tzachbon 훅의 최신 README는 autoswitch가 새 세션의 설정에 영향을 준다고 설명한다. 실제 `user_prompt_submit.py`는 전환이 필요할 때 warn/autoswitch 양쪽에서 exit 2로 입력을 중단하고 `/model`, `/effort` 후 재전송을 안내한다. 현재 기본 정책도 구현·디버깅·설계 대부분을 Opus로 보내고 effort를 달리한다. 애매한 요청에는 추가 Haiku 분류 호출이 있을 수 있다. 저장소의 지원 표시는 macOS/Linux이고 Windows는 미확인이다. 최신 변경 기록은 2.1.0/2026-08-28이다. [저장소](https://github.com/tzachbon/claude-model-router-hook), [실제 훅 소스](https://github.com/tzachbon/claude-model-router-hook/blob/main/plugins/claude-model-router-hook/hooks/user_prompt_submit.py), [변경 기록](https://github.com/tzachbon/claude-model-router-hook/blob/main/CHANGELOG.md)

CCR는 최신 확인 릴리스 v3.0.22/2026-08-24에서 Windows 배포와 Codex CLI/APP 프로필을 제공한다. 요청 본문/헤더 규칙, 모델·파라미터 변경, Node.js 분류 스크립트를 조합한다. 설치만으로 한국어 질문의 최적 모델을 알아서 보장하는 제품은 아니다. Codex 통합 문서는 `Only opened from CCR` 범위를 제공하므로 시험한다면 이 범위를 우선 검토한다. 제3자 도구가 명시한 지원이며 현재 Codex 앱에서 시험한 결과는 아니다. 선택한 upstream의 인증·과금 경로를 확인해야 하고 구독 유지나 API 별도 과금을 일괄 단정하지 않는다. [릴리스](https://github.com/musistudio/claude-code-router/releases/tag/v3.0.22), [라우팅](https://ccrdesk.top/en/routing/), [Codex 통합](https://ccrdesk.top/en/configuration/agents/codex/), [공급자 설정](https://ccrdesk.top/en/guides/provider/)

LiteLLM의 2026-09-08 공식 글에는 휴리스틱 가중치 조정, NON_REASONING tier, 모델별 `reasoning_effort`가 실제로 있다. 휴리스틱은 분류 API 호출이 없고 LLM/임베딩 방식은 추가 호출이 생긴다. `lite autoroute` preview는 기존 LiteLLM proxy 앞에 로컬 proxy를 만들고 Claude 설정을 패치·복구한다. README는 Claude Code만 지원한다고 명시하고 Windows 동작은 확인하지 않았다. [공식 Auto Routing](https://docs.litellm.ai/docs/proxy/auto_routing), [사용자가 인용한 블로그](https://docs.litellm.ai/blog/auto-router-heuristic-tuning), [CLI](https://docs.litellm.ai/docs/learn/autorouter_cli), [CLI README](https://github.com/BerriAI/litellm/blob/litellm_internal_staging/litellm/proxy/client/cli/README.md)

OpenRouter Auto는 약 30개 작업 유형으로 분류하고 최근 7일의 유형별 모델 지출 점유율 순위에 비용 tier·허용 모델 등을 적용한다. 라우터 추가요금은 없지만 선택 모델을 OpenRouter credits로 결제한다. `ANTHROPIC_BASE_URL` 변경만으로 auto가 켜지는 것은 아니며 라우터 모델도 선택해야 한다. 요청은 OpenRouter와 최종 공급자를 거친다. [Auto Router](https://openrouter.ai/docs/guides/routing/routers/auto-router), [Claude 연결](https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration), [데이터 정책](https://openrouter.ai/docs/guides/privacy/data-collection)

**권고하는 적용 순서 — 미적용 제안**

1. Claude는 버전·사용 가능한 모델을 먼저 맞추고 `opusplan`을 기준선으로 삼는다. 이후 Sonnet + Opus advisor를 비교한다. 두 기능을 처음부터 중첩해 어느 쪽 효과인지 알 수 없게 만들지 않는다.
2. Codex 앱은 우선 역할별 위임을 구성한다. 읽기/정리에는 Luna low~medium 후보, 일반 구현에는 Terra 또는 Sol medium~high 후보, 설계·원인 불명 오류·중요 검토에는 Astra high 이상 후보를 둔다. 이는 시험할 조합이며 품질 우열을 검증한 결과가 아니다. 전체 대화 상속 방식에 따라 개별 모델 재정의 제약이 있으므로 적용할 CLI/앱의 실제 spawn 계약을 확인한다.
3. 질문마다 주 모델까지 자동 변경해야 한다면, 기존 기능 후보인 CCR의 독립 프로필을 먼저 검토한다. 구독 인증을 유지하는 직접 통합이 필수라면 Codex App Server 기반 입력 클라이언트가 대안이다. 이는 별도 개발이며 기본 앱 입력창에 꽂는 간단한 훅이라고 소개하지 않는다.
4. 라우팅은 모델명보다 작업 맥락을 먼저 정한다. 명백한 단순 편집/조회, 범위가 확정된 구현, 설계·복합 오류·중요 검증의 세 단계로 시작한다. “응 진행해”, “아까대로”처럼 짧은 후속 입력은 직전 작업 등급을 이어받는다. 사용자의 명시적 모델·effort 지정이 최우선이다. 불확실하면 무리하게 낮추지 않는다.
5. 실제 적용 전 대표 한국어 요청 약 20개로 선택 결과부터 확인한다. 이어 같은 과제의 기존 방식과 후보 방식에서 완료시간, 실제 모델/effort, 사용량, 재시도·사람 수정, 필수 검증 통과를 비교한다. 주 모델·분류기·하위 에이전트·자문·캐시 재처리를 모두 포함해 계산한다. 토큰 감소만으로 채택하지 않는다. 이번에는 이 평가를 실행하지 않았다.

현재 Astra/ultra 또는 Opus/xhigh 고정값을 일괄 낮추기보다 검증된 단순 작업부터 분담하는 안이다. 단계 전환 때마다 새 대화를 만드는 방식은 사용자의 현재 세션 유지 요구와 맞지 않아 권고하지 않는다. 기사 생성 API의 라우팅과 이 개발 도구 설정은 별개의 작업으로 유지한다.

**검증·보존**

- 실행 명령: `codex --version`, `claude --version`, 두 CLI의 `--help`, 선택한 설정 필드 읽기, `codex app-server generate-json-schema --out .local\model-routing-research\schema`.
- 스키마 생성 exit 0. 근거: `.local/model-routing-research/schema/v2/TurnStartParams.json`, `TurnSteerParams.json`, 요약 `local-capabilities.json`.
- 새 App Server 세션/모델 턴을 실행하지 않았다. 앱 연결, 라우팅 정확도, 실제 모델 품질·지연·사용량·절감률은 미검증이다.
- 앱 코드 변경이 없어 pytest는 이번 조사에서 실행하지 않았다. 다른 작업의 최신 테스트 결과는 해당 HANDOFF 체크포인트가 기준이며 이 조사 결과로 바꾸지 않는다.
- 브랜치 `main`, 작업 시작 시 HEAD `4d767ac13fe50dc9361138751cc5dbdb1e74bff3`; 기존 다수 미커밋 변경 보존. 커밋/설치/설정 변경/서비스 재시작/발행 없음.
- 조사 중 다른 작업이 HANDOFF/CONTINUE의 제작실 체크포인트를 갱신한 사실을 확인했다. 기존 내용을 덮어쓰지 않고 이 조사 기록만 덧붙인다.

추가 확인 시각: 2026-09-10 09:23:49 +09:00

**추가 확인 — 직접 만든 Codex 하위 에이전트 라우팅 훅**

사용자가 앞선 역할별 모델 분담 제안에 대해 “이걸 훅으로 내가 직접 구현할 수는 없나?”라고 질문했다. 구현 가능성과 구체 구조를 확인한 요청이며 전역 설치·설정 변경은 실행하지 않았다.

공식 Hooks의 Tool coverage는 `spawn_agent`가 `Agent` 별칭과도 매칭된다고 명시한다. `PreToolUse`의 `permissionDecision: allow` + `updatedInput`으로 생성 호출의 전체 인수를 바꿀 수 있으므로, 하위 작업의 모델·effort를 정하는 직접 훅은 가능하다. 기존 문서의 “주 모델 변경 필드 없음”은 주 대화에 대한 설명이고 하위 에이전트 생성 인수 변경까지 불가능하다는 뜻이 아니다. [Tool coverage](https://learn.chatgpt.com/docs/hooks#tool-coverage), [PreToolUse](https://learn.chatgpt.com/docs/hooks#pretooluse)

최소 제안은 UserPromptSubmit에서 주 에이전트에 역할 분류/위임 정책을 전달하고, PreToolUse에서 생성 요청을 읽어 명확한 역할에 모델·effort를 배정하는 구조다. 주 에이전트가 이미 읽는 맥락을 이용하므로 별도 분류 API는 불필요하다. 읽기=Luna, 일반 구현=Terra/Sol, 설계/중요 검토=Astra를 시험할 수 있다. 단순 키워드로 난도를 추정하기보다 주 에이전트가 명시한 역할을 고정 표에 매핑하고, 애매하면 기존 모델을 보존한다. 사용자 명시 모델/effort는 우선한다. 원래 작업 지시·권한·기타 인수를 보존하며 무조건 모든 질문을 하위 에이전트로 보내지는 않는다.

SubagentStart는 문맥 추가 단계이므로 모델 교체 지점으로 쓰지 않는다. 역할별 TOML의 모델/effort가 명시 spawn 인수보다 우선하므로 직접 인수 변경과 역할 파일의 정책을 중복·충돌시키지 않는다. 현재 노출된 collaboration 도구는 fork_turns=all에서 부모 모델을 상속하고 모델 재정의를 허용하지 않는다. 다른 모델을 쓸 경우 처음부터 독립/부분 문맥과 충분한 작업 지시를 구성해야 하며 훅이 몰래 전체 상속을 제거하면 안 된다. [SubagentStart](https://learn.chatgpt.com/docs/hooks#subagentstart), [Custom agents](https://learn.chatgpt.com/docs/agent-configuration/subagents#custom-agents)

문서상 일부 특수 도구 경로는 훅을 생략할 수 있다. 현재 데스크톱의 collaboration 생성 호출이 실제 PreToolUse로 전달되는지는 미검증이다. 따라서 동작 완료를 주장하지 않고 실제 이벤트 포착과 합성 입력의 정책 보존 검사를 구현 시 먼저 수행한다. 주 대화 Astra/ultra는 이 방식으로 바뀌지 않는다. 총사용량 절감도 아직 측정하지 않았다. 코드·훅 파일·사용자 설정 변경 및 실제 라우팅 호출은 이번 추가 조사에서 0이다.

추가 확인: 2026-09-10 09:53:39 +09:00
최신 역할 배정은 SUBAGENT_ROUTING_POLICY.md, Claude 전달 지시는 CLAUDE_ROUTING_HANDOFF_PROMPT.md를 따른다. 일반 구현 Sol medium, 정형 읽기 Luna medium, 중요 검토 Sol high, 어려운 설계 Astra의 미적용 시험안이다. 실제 사용자 환경 최적값/절감률은 아직 미검증이다.

## Codex Model Router 구현 완료 — 2026-09-10 10:24:33 +09:00
사용자 요청으로 독립 폴더 C:\Users\dsaka\Desktop\Codex Model Router에 훅·README·설치/제거·검사·HANDOFF/CONTINUE를 생성하고 Codex 사용자 훅 3개를 등록·신뢰했다. 상세 최신 상태는 새 폴더 HANDOFF.md가 기준이다. 이 기록은 Casefile 제품 작업 상태를 대체하지 않는다.
중요한 실제 발견: 0.153.4 V2 생성은 PreToolUse를 통과하지 않아, UserPromptSubmit이 본체에게 역할별 model/reasoning_effort를 직접 지정하게 하는 방식을 주 경로로 구현했다. PreToolUse는 지원 경로 보완, SubagentStart는 실제 모델 관측이다. 오프라인 7검사 통과, 실제 Luna 시작/합성 정답 확인. 공개 이벤트의 실제 effort와 절감률/품질은 미검증. config 원본과 비교하여 라우터 신뢰 3항목 이외의 설정 완전 동일 확인. 본체 Astra/ultra·Ponytail·권한·Casefile 앱 코드와 서버 지도 설정 보존. 대규모 API 평가/기사 생성/커밋/사용자 서비스 종료 없음.
추가 사용자 지시: axweb-001 경로·로그·DB·nginx 검색 전 전역 AGENTS.md 50~53줄이 가리키는 C:\Users\dsaka\.claude\projects\C--Users-dsaka-Desktop\memory\server-axweb-paths.md를 먼저 읽는다. 기존 참조와 실제 파일 확인만 했고 새 설치/설정 수정 금지. 메시지의 dsaka.claude는 dsaka\.claude의 구분자 누락이었다. 서버 실시간 상태 검증은 하지 않았다. 현재 세션 유지.
