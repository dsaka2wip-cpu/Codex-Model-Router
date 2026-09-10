# Codex Model Router

Codex가 하위 작업의 종류에 맞춰 모델과 노력 수준을 선택하게 하는 사용자 훅입니다. Python 표준 라이브러리만 사용하며, 분류용 API나 프록시 서버가 없습니다.

## 배정표

| 역할 | 작업 | 모델 | 노력 수준 |
|---|---|---|---|
| `lookup` | 정확한 위치 검색, 지정 항목 추출, 서식 정리 | GPT-5.6 Luna | medium |
| `implementation` | 호출 흐름 분석, 일반 구현·UI·테스트 작성 | GPT-5.6 Sol | medium |
| `critical_review` | 깊은 디버깅, 보안·무결성·의미 검증 | GPT-5.6 Sol | high |
| `hard_problem` | 독립된 어려운 설계, 요구 충돌 해결 | GPT-6 Astra | high |

기계적인 명령은 본체가 도구로 직접 처리합니다. 설계도 본체가 해결할 수 있으면 추가 에이전트를 만들지 않습니다. Terra와 모든 작업의 max 고정은 기본 배정에 넣지 않았습니다. 이 표는 품질·효율 검증을 시작할 기본값이며 보편적인 최적값이나 절감률을 보장하지 않습니다.

## 작동 방식과 확인한 한계

1. `UserPromptSubmit` 훅이 매 사용자 입력에 짧은 역할 배정 지침을 추가합니다. 본체가 이전 대화를 포함해 하위 작업을 분류하고, 생성 도구의 `model`과 `reasoning_effort`를 직접 지정합니다.
2. `PreToolUse` 훅은 지원되는 `spawn_agent` / `Agent` 경로에서 명시적인 역할 표식을 읽어 빠진 모델·노력 수준을 채웁니다. 다른 인수는 모두 보존합니다.
3. `SubagentStart` 훅은 실제 시작 모델을 기록합니다. 공개 이벤트에 effort가 없으면 null로 남깁니다. 부모 작업 식별자는 짧은 SHA-256 지문으로만 기록해 서로 다른 실행을 구분합니다.
4. **Codex 0.153.4의 실제 V2 검사에서는 하위 생성이 `PreToolUse`를 통과하지 않았습니다.** 따라서 1번이 주 경로이고 2번은 지원 경로의 보완 수단입니다. 모든 도구를 강제로 제어하는 보안 경계가 아닙니다. 본체의 분류·인자 지정이 잘못될 가능성은 남습니다.

사용자는 평소처럼 작업을 요청하면 됩니다. `[codex-route:lookup]` 같은 표식은 본체가 하위 지시의 첫 줄에 넣는 내부 약속이며, 사용자가 매번 입력할 필요가 없습니다. 원문 키워드만 보고 난이도를 추측하지 않고 짧은 후속 지시도 앞선 맥락과 함께 분류하게 했습니다.

- 사용자가 직접 지정한 모델·effort는 자동 배정보다 우선합니다.
- 이미 지정된 모델/effort 또는 사용자 정의 `agent_type`은 재작성하지 않습니다.
- `fork_turns=all` 또는 생략된 V2 전체 상속을 임의로 `none`으로 바꾸지 않습니다. 다른 모델을 쓸 때 본체가 처음부터 충분한 목표·제약·근거·검증 조건을 담아 독립 작업을 구성해야 합니다.
- 알 수 없는 역할, 잘못된 JSON, 로그 쓰기 실패는 원래 작업을 막지 않습니다.
- 실패 모델을 차례대로 모두 거치는 재시도, 본체의 중복 조사, 불필요한 재귀 위임을 지시하지 않습니다.
- 필요한 원문·출처·필수 테스트·사용자 진행 설명을 줄이지 않습니다.
- 본체의 저장 기본 모델·노력 수준, 속도 등급, 권한 및 기존 훅은 변경하지 않습니다.

## 설치 및 상태 확인 — Windows PowerShell

Python 3과 로그인된 Codex CLI가 필요합니다. Python 표준 라이브러리만 사용하므로 pip 의존성은 없습니다. `python`이 PATH에 없다면 아래 `$routerPython`을 사용할 Python 실행 파일의 절대 경로로 지정하세요. 설치 후 Python이나 저장소 위치를 바꾸면 설치·활성화 명령을 다시 실행해야 합니다.

```powershell
$routerPython = (Get-Command python -ErrorAction Stop).Source
git clone https://github.com/dsaka2wip-cpu/Codex-Model-Router.git
Set-Location .\Codex-Model-Router
& $routerPython -m unittest -v
& $routerPython .\install.py install
& $routerPython .\manage.py activate --standalone
& $routerPython .\manage.py status --standalone
```

`install.py`는 사용자 `$CODEX_HOME/hooks.json`(미설정 시 `~/.codex/hooks.json`)에 이 라우터의 세 항목만 병합합니다. 재설치는 중복을 만들지 않습니다. 기존 hooks.json은 먼저 이 폴더의 `backups/`에 보존합니다. 기본 모델이나 플러그인 설정을 다시 쓰지 않습니다.

`manage.py activate`는 설치·활성화가 승인된 상황에서만 사용합니다. Codex App Server가 반환한 정의와 설치 코드의 명령·이벤트·매처·동기 실행·타임아웃을 대조하고, **이 세 훅의 현재 해시만** 기본 신뢰 설정에 등록합니다. 전역 신뢰 우회는 사용하지 않습니다. 설정 백업은 비밀 설정의 유출을 피하도록 Codex 홈의 `config.toml.router-backup-*`에 둡니다. 기존 설정 버전이 달라지면 쓰기를 거절합니다.

활성화는 해당 훅 세 개가 `enabled: true`, `trustStatus: trusted`인지 재확인합니다. `status`는 네트워크 모델 생성 없이 훅 상태와 모델 카탈로그를 조회합니다. `--standalone`은 관리 명령 동안 별도 stdio App Server를 사용하며 실행 후 종료합니다. 기존 앱·서비스는 종료하지 않습니다. Windows 검증 환경에서는 daemon proxy 연결 대신 이 옵션을 사용했습니다.

직접 관리하려면 Codex CLI의 `/hooks`에서도 세 `Codex Model Router` 항목을 검토·신뢰·비활성화할 수 있습니다. 새 명령 정의는 다시 신뢰가 필요합니다. 훅의 정의 해시는 스크립트 본문 전체의 무결성 검사와 같지 않으므로 스크립트 변경 뒤에는 테스트를 다시 실행하세요.

**이미 진행 중인 데스크톱 턴에 새 훅이 소급 적용된다고 보장하지 않습니다.** 설치 후 다음 사용자 입력부터 훅 실행 기록을 확인하세요. 파일이 존재하거나 신뢰 상태라는 것만으로 특정 앱 경로가 실제 실행됐다고 단정하지 않습니다. 강제 재시작·새 작업 생성은 설치 과정에 포함하지 않습니다.

## 검증과 로그

- `python -m unittest -v`: API 호출 없이 배정, 인수 보존, 명시값 우선, 전체 상속, 알 수 없는 입력, 설치 중복·제거·백업을 검사합니다.
- `live_check.py`: **명시적으로 실행할 때만 Codex 구독 사용량이 발생**하는 작은 실제 확인입니다. 합성 JSON 한 항목을 한 자식에게 읽히고, 가능한 런타임 메타데이터를 `live-check.json`에 저장합니다. 자동 설치나 일반 훅 실행에서는 호출하지 않습니다.
- `logs/YYYY-MM-DD.jsonl`: UTC 시각, 정책 주입/배정/보존 여부, 알려진 역할·모델·effort만 기록합니다. 사용자 프롬프트, 기사, 파일 경로, 키, 원래 작업 ID, 대화 내용, 비공개 사고 과정을 수집하지 않습니다. 삭제해도 실행에 영향이 없습니다.
- `routed`는 훅이 생성 인수를 재작성했다는 뜻이며 청구 서버의 모델 사용 증명은 아닙니다. V2는 이 이벤트 없이 입력 정책을 통해 직접 모델을 지정할 수 있습니다.
- 원시 토큰, 캐시 토큰, 출력의 부분집합인 추론 토큰, 비용, 구독 한도를 구분하세요. 전체 효율은 본체·자식·검토·실패를 합쳐 수락된 결과당 비교해야 합니다. 이 도구는 청구액이나 절감률을 추정하지 않습니다.

Windows / Codex 0.153.4에서 오프라인 검사 7개가 통과했고, 실제 합성 lookup 작업의 Luna 시작과 정답을 확인했습니다. 실제 effort는 공개 시작 이벤트에 없어 별도로 검증하지 못했습니다. `live_check.py`의 결과에서 모델 시작 확인과 model/effort 동시 확인을 구분합니다. 로컬 핸드오프·실행 로그·백업은 저장소에 포함하지 않습니다.

## 비활성화 / 제거

```powershell
$routerPython = (Get-Command python -ErrorAction Stop).Source
# 저장소 폴더에서 실행
& $routerPython .\install.py uninstall
```

이 라우터의 세 훅만 제거하고 다른 훅은 유지합니다. config.toml의 사용되지 않는 신뢰 해시 항목은 남을 수 있지만 훅 명령은 실행되지 않습니다. 나중에 다른 설정이 바뀔 수 있으므로 예전 config.toml 전체를 자동 복원하지 않습니다. 등록이 남아 있는 동안에는 이 폴더를 이동·삭제하지 마세요. 제거 후 폴더와 로그를 정리할 수 있습니다.

## 파일과 근거

- `router.py`: 배정표, 입력 정책, 생성 인수 재작성, 최소 로그.
- `install.py`: 기존 훅을 보존하는 설치·제거와 백업.
- `manage.py`: Codex 기본 API를 통한 발견·모델 지원·정확한 정의 신뢰 확인.
- `test_router.py`: 오프라인 검사. `live_check.py`: 선택적인 실제 실행 검사.

공식 근거: [Hooks와 도구 예외](https://learn.chatgpt.com/docs/hooks#tool-coverage), [입력 재작성 계약](https://learn.chatgpt.com/docs/hooks#pretooluse), [신뢰 등록](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks), [하위 에이전트 모델 선택](https://learn.chatgpt.com/docs/agent-configuration/subagents#choosing-models-and-reasoning).

배정 참고: [Instavar 비교 실험](https://instavar.com/research/agents/gpt-5-6-codex-models-reasoning-levels-benchmark-2026), [사용자의 위임 비용 비교](https://www.reddit.com/r/codex/comments/1veac6s/a_cost_analysis_of_my_usage_of_using_sol_using/), [Astra·Sol·Luna 역할 분담 사례](https://www.reddit.com/r/codex/comments/1w8iwgi/updated_threetier_agent_architecture_astra_med/). 환경·과제·반복 수의 한계가 있으므로 검증된 최적 배정으로 주장하지 않습니다.
