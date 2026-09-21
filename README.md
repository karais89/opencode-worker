# opencode-worker

Codex의 구현·테스트·디버깅을 OpenCode에 맡기는 얇은 개발 Skill입니다. 사용자는 한 번 요청하고, Codex는 중요한 판단과 최종 확인을 담당합니다. 정상 흐름은 **Codex → Worker 1회 → Codex 최소 확인**입니다. 실제로 재현한 결함이나 명백한 요구사항 위반이 있을 때만 수정 Worker를 **최대 1회** 실행합니다. Reviewer나 자동 반복 수정은 없습니다.

목적은 개발 도구를 줄이지 않고 Codex의 구현·반복 검토 부담을 줄이는 것입니다. 모든 작업에서 일정한 토큰 절감을 보장하지는 않습니다.

## 필요한 환경

- Codex의 로컬 Skill 기능, Python 3.10 이상, Git, Unix 환경(macOS/Linux; Windows 네이티브는 `fcntl` 때문에 지원하지 않음).
- PATH에서 실행되는 OpenCode CLI와 사용할 provider 인증/모델 설정.
- OpenCode와 호환되는 `@opencode-ai/plugin` SDK. 구조화된 결과 제출에 사용합니다. 설치 경로와 준비 방법은 [설정 안내](references/control.md)를 확인하세요.
- 제품 회귀 테스트를 실행하려면 Node.js도 PATH에 필요합니다.
- 해당 프로젝트의 개발 도구 및 테스트 환경. Unity 등은 기존 CLI/MCP/Skill 설정이 실제로 작동해야 합니다.

## 설치

이 디렉터리 자체가 단일 Skill이며 저장소 루트에 `SKILL.md`가 있습니다. 로컬 복사본을 설치할 때:

```sh
mkdir -p "$HOME/.agents/skills"
cp -R /absolute/path/to/opencode-worker "$HOME/.agents/skills/opencode-worker"
```

기존 같은 이름의 Skill이 있으면 먼저 별도 위치에 백업하고 중복을 해소하세요. 기존 Full/Lite를 자동 교체하지 않습니다. 설치가 보이지 않으면 Codex를 다시 시작하세요. 프로젝트에만 설치하려면 `<repo>/.agents/skills/opencode-worker`를 사용합니다.

GitHub에서 설치:

```sh
npx skills add karais89/opencode-worker
```

CLI에서 Codex와 원하는 설치 범위를 선택하세요. 소스는 [GitHub](https://github.com/karais89/opencode-worker)에 있으며 [MIT 라이선스](LICENSE)로 배포됩니다. skills.sh는 별도 publish 명령 대신 설치 통계를 통해 자동으로 등재합니다.

## 최초 설정

OpenCode에서 provider 연결을 먼저 완료한 뒤 설치된 경로를 지정합니다.

```sh
WORKER_SKILL="$HOME/.agents/skills/opencode-worker"
python3 "$WORKER_SKILL/scripts/worker.py" models
python3 "$WORKER_SKILL/scripts/worker.py" set-writer-default '<provider/model>' --variant '<supported-variant>'
python3 "$WORKER_SKILL/scripts/worker.py" resolve
```

모델/variant는 실제 목록에서 선택합니다. variant가 없는 모델은 `--variant`를 생략하세요. 기존 설정은 유지되며 자격증명을 Skill에 넣지 않습니다. 설정 파일 기본 위치는 `~/.config/opencode-worker/config.json`입니다.

기존 사용 조합은 **OpenCode + MergeGateway + DeepSeek V4.1 Flash / max**입니다. 배포본에 계정이나 이 모델을 강제하지 않습니다. provider/model/variant, 프로젝트별 라우팅과 명시적 fallback 설정을 바꿀 수 있습니다. backend는 현재 OpenCode이며 다른 backend 구현은 포함하지 않습니다. 이 저장소의 회귀 테스트가 실제 provider 연결이나 모델 성능을 검증하는 것은 아닙니다. [상세 설정](references/control.md).

## 사용

**자동:** “CSV 가져오기 기능을 구현하고 테스트해줘.”처럼 일반 개발 요청을 합니다. Codex가 description과 작업 범위를 보고 Skill을 선택합니다. 자동 선택은 모든 요청에 대한 강제 실행 규칙이 아닙니다. 질문·설계 논의·작은 수정에는 보통 사용하지 않습니다. 여러 파일을 읽어야 하는 영향 범위·원인·데이터 흐름 조사에는 `--read-only`로 위임할 수 있습니다. 수정 여부보다는 작업의 탐색량과 필요한 권한으로 구분합니다.

**명시적:** Codex에서 다음처럼 Skill을 직접 지정합니다.

```text
$opencode-worker CSV 가져오기 기능을 구현하고 테스트해줘.
```

명시적 지정은 해당 Skill 사용을 요청하는 방법입니다. off 설정이나 호스트 권한 거부를 우회하지 않습니다. UI의 Skill 선택기로도 지정할 수 있습니다. CLI/IDE는 `$` 또는 `/skills`를 지원하며, ChatGPT UI는 `@` 선택기를 사용합니다.

Codex가 짧은 작업 지시를 전달하고 Worker가 AGENTS.md·공유 Skill·도구를 발견해 탐색/구현/테스트/오류 수정을 수행합니다. Head는 압축된 결과를 확인하며 전체 소스나 전체 테스트를 습관적으로 다시 읽거나 실행하지 않습니다.

최종 답변 끝의 한 줄로 실제 실행 여부를 확인합니다(아래는 형식 예):

```text
Worker: DeepSeek V4.1 Flash · 1회 · 42초
Worker: DeepSeek V4.1 Flash · 2회(수정 1회) · 71초
```

작업이 실행되지 않았다면 미사용 사유를 표시합니다. 최종 응답의 중심은 구현 결과·검증·남은 위험입니다.

### 읽기 전용 조사

기존 `run` 명령에 `--read-only`를 추가합니다. 이 모드는 native read/glob/grep/list만 허용하며 shell·테스트 실행·파일 수정·임의 MCP 도구를 허용하지 않습니다. 결과의 `changed`는 빈 배열이고, `validation`에 파일/줄 근거를 포함한 조사 결과를, `risk`에 확인하지 못한 동작을 적습니다. 실행이 필요한 조사에 이 모드를 억지로 적용하거나 권한을 몰래 확장하지 않습니다.

### 완료 보고와 관측 증거

제출 도구는 기존 `status`, `changed`, `validation`, `risk`와 선택 필드 `validation_commands`를 받습니다. `validation_commands`에는 실제 마지막 검증에 사용한 직접 실행 명령을 최대 10개 적습니다(각 256자 이하, 중복 금지). 전체 제출 보고서는 여전히 1,800자 이내입니다.

컨트롤러는 이미 수집하는 shell 이벤트와 명령을 대조해 `validation_evidence`를 반환합니다. `observed_pass`는 선언된 로컬 명령의 성공 종료를 관측했다는 뜻이지 테스트 통과 개수·요구사항 충족·원격 작업 완료의 독립적인 증명이 아닙니다. `partial`은 일부만 확인, `unverified`는 확인할 근거가 부족함을 뜻합니다. 구체적으로 관측된 실패가 있으면 `failed`로 표시하고 완료 판정을 막습니다.

명령 선언이 없는 기존 보고서, 잘린/누락된 기록, 복합 shell 명령, 변경 전에 실행한 검증, MCP/Unity 원격 결과는 자동으로 검증 성공이 되지 않습니다. 미검증은 무조건 실패도 아닙니다. Head는 Worker 보고와 관측 증거를 구분해 남은 위험을 판단합니다. 검사기는 새 모델을 호출하거나 테스트를 다시 실행하지 않습니다. 전체 보안 경계나 범용 테스트 결과 파서는 아닙니다.

### 수정 맥락과 fallback

수정 1회는 원래 brief를 비공개 실행 증거에 보존하고 수정 brief와 함께 **새 세션**에 전달합니다. 이전 세션 전체를 자동 재개하지 않습니다. 실제 마지막으로 실행한 모델/variant와 승인 명령 옵션을 고정하며 수정 과정에서 fallback을 사용하지 않습니다. 설정이 바뀌어도 다른 모델로 몰래 바뀌지 않습니다. 원래 brief가 없는 과거 증거는 자동 추측하거나 재실행하지 않고 중단합니다.

**동작 변경:** 저장된 fallback 목록만으로는 대체 모델을 실행하지 않습니다. 사용을 허용한 실행에서 `--use-fallbacks`를 명시해야 합니다. `--no-fallback`을 함께 주면 비활성화가 우선합니다. provider 실패 후 이미 발생했을 수 있는 작업을 재실행하지 않는 경계는 유지합니다.

## 안전과 문제 해결

일반 프로젝트 내부 파일 작업, shell, 의존성, test/build/lint/format/debug 및 연결된 개발 도구를 유지합니다. secrets, 시스템/전역 설정, 승인 없는 외부 수정, push/publish/deploy, 파괴적 git 작업, 광범위 삭제, 중첩 Worker는 제한합니다. 이 정책은 OS 샌드박스 자체가 아니며 호스트 권한도 계속 적용됩니다.

동일 Git checkout은 잠금으로 동시 Writer 충돌을 막고 독립 checkout은 동시 실행할 수 있습니다. `busy`이면 실행 중인 작업을 확인하고 기다리세요. 잠금을 삭제해 우회하지 마세요.

`setup_required`이면 route와 provider 인증을, SDK 오류이면 [설정 안내](references/control.md)를 확인하세요. Worker의 최종 문장은 파싱하지 않고 제출 도구 결과를 검증합니다. 실행/제출 실패는 자동 재실행하지 않습니다. 구체적 구현 결함의 수정 기회도 1회뿐이며, 해결되지 않으면 남은 문제를 보고합니다.

## 검증

```sh
python3 -m unittest discover -s scripts -p 'test_*.py'
```

회귀 테스트는 가짜 CLI 이벤트와 임시 SDK 인터페이스 fixture를 사용하며 실제 provider 인증이나 네트워크 호출이 필요하지 않습니다. 실제 JavaScript 제출 검증 로직도 이 fixture로 검사하지만, 설치된 OpenCode/SDK의 통합 동작을 입증하지는 않습니다. 실제 모델·도구 환경의 연결은 별도 확인이 필요합니다.

제품에는 필요한 런타임과 회귀 테스트만 포함합니다. 연구 benchmark·fixture·과거 구현은 포함하지 않습니다. 설치에는 OpenCode provider 인증과 위 환경 준비가 별도로 필요합니다.

참고: [Codex Skill 사용·설치](https://developers.openai.com/codex/skills/), [skills CLI](https://skills.sh/docs/cli), [OpenCode plugins](https://opencode.ai/docs/plugins/).
