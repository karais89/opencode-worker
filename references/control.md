# 설정과 실행 참조

일반 개발 작업에는 이 문서를 읽을 필요가 없습니다. `python3 <skill>/scripts/worker.py --help`와 `run --help`가 설치된 버전의 실제 CLI입니다.

## 환경

OpenCode CLI 및 provider 인증을 먼저 준비하세요. Skill은 인증 파일을 복사하거나 새 backend를 설치하지 않습니다. [OpenCode 공식 문서](https://opencode.ai/docs/)를 참고하세요.

제출 플러그인은 기존 `@opencode-ai/plugin/dist/tool.js`를 다음 디렉터리의 `node_modules`에서 찾습니다: `OPENCODE_CONFIG_DIR`, `${XDG_CONFIG_HOME:-~/.config}/opencode`, `${XDG_CACHE_HOME:-~/.cache}/opencode`. SDK가 없으면 실행을 중단하며 몰래 설치하지 않습니다. 기존 OpenCode config 디렉터리의 package.json에 CLI와 호환되는 `@opencode-ai/plugin` 의존성을 준비하고 해당 디렉터리에서 패키지 관리자로 설치하세요. 예: `npm install --prefix "$HOME/.config/opencode" @opencode-ai/plugin@<matching-version>`. 기존 package.json을 덮어쓰지 마세요. 사용자 설치 단계이며 Worker 작업 때마다 필요하지 않습니다.

프로세스 전용 plugin 설정으로 assets/submit-result.mjs를 로드합니다. 저장소나 전역 OpenCode 설정 파일에 플러그인을 추가하지 않습니다.

## 라우팅

`--config /absolute/config.json`으로 별도 설정을 지정할 수 있습니다. 우선순위는 `run --model` → 프로젝트 route → writer_default/default입니다. `--use-opencode-default`는 저장 route가 없을 때 OpenCode 기본 모델을 명시적으로 사용합니다. `models`로 정확한 모델 ID, `resolve`로 선택 결과를 확인하세요.

- `set-writer-default <provider/model> --variant <variant>`: 기본 모델.
- `--project /repo set-project <provider/model> --variant <variant>`: 프로젝트 모델.
- `set-alias <name> <provider/model>`: 별칭.
- `set-fallbacks <route...>`는 후보 목록만 저장합니다. 실제 사용은 `run --use-fallbacks`로 명시합니다. global/project/one-shot 모두 같은 규칙이며 `--no-fallback`이 우선합니다. 안전한 provider 실패에 한하며 이미 발생했을 수 있는 작업은 재실행하지 않습니다.
- `set-mode auto|manual|off`: 자동 허용/명시적 요청만/중단. manual은 `run --explicit` 필요, off는 explicit으로 우회 불가.

`models`의 `deepseek_flash`는 이름에 DeepSeek와 Flash가 있는 후보를 찾는 보조 목록입니다. 기존 `deepseek_v4_1_flash`는 버전 이름이 명시된 후보만 유지합니다. 일반 별칭을 V4.1로 단정하지 않습니다. 모델 목록은 인증·과금·성능·실제 모델 정체성의 보장이 아닙니다.

## 실행

```sh
python3 <skill>/scripts/worker.py --project /absolute/repo run --brief /absolute/task.txt --evidence-dir /outside/repo/evidence
```

brief는 목표·제약·완료 조건만 짧게(최대 8 KiB) 작성합니다. Worker가 프로젝트 지침과 원본 공유 Skill/CLI/MCP를 탐색합니다. 발견이 도구 설치나 연결 성공을 보장하지는 않습니다. 필요한 도구가 없으면 실제 blocker를 보고합니다. shell을 제한하는 read-only 모드는 일반 Writer에 적용하지 않습니다.

읽기만으로 해결할 수 있는 큰 코드 조사에는 `--read-only`를 추가합니다. native read/glob/grep/list와 제출 도구 외의 변경·shell·임의 MCP 도구는 허용하지 않습니다. `changed=[]`, `validation`에는 파일/줄 근거와 조사 결과, `risk`에는 미확인 사항을 제출합니다. 실행이 필요한 조사를 이 모드로 완수했다고 보고하지 않습니다.

## 수정 1회와 원래 맥락

실제 입력으로 재현한 결함 또는 명백한 요구사항 위반에만 `--fix-from <returned-evidence-dir>/writer.json --brief <short-correction.txt>`로 수정 1회를 요청합니다. 원래 project와 config를 유지하세요. 새로운 일반 run으로 제한을 우회하지 마세요. 두 번째 수정은 차단됩니다. Reviewer와 자동 수정 체인은 없습니다.

원래 실행의 비공개 `task_context`에 brief·read_only·승인 명령 옵션을 저장합니다. 수정은 원래 brief와 수정 brief를 함께 전달하는 **새 세션**이며 이전 OpenCode 세션 전체를 자동 재개하지 않습니다. 마지막 실제 실행 route/variant를 증거에서 고정합니다. 원래 variant가 없었으면 이후 전역 variant도 상속하지 않습니다. 모델 옵션 충돌이나 승인 명령 확대는 실행 전에 거절합니다. 수정에서는 fallback을 비활성화합니다. 현재 off/manual 모드와 호스트 권한은 계속 적용됩니다.

원래 writable-task brief가 없는 과거 증거는 맥락을 추측하지 않고 needs_escalation으로 중단합니다. 유효한 수정의 시도 횟수는 실행 전 기록되며 실패·중단도 사용한 1회로 계산합니다. 준비 단계에서 잘못된 입력이 거절된 것은 시도를 소비하지 않습니다.

## 검증 증거의 의미

기존 보고 필드 외에 선택적인 `validation_commands` 배열을 지원합니다. 최대 10개, 각 256자 이하의 중복 없는 직접 실행 명령이며 전체 보고 1,800자 제한에 포함됩니다. 워커는 마지막 변경 후 실행한 로컬 검증 명령만 명시합니다. shell wrapper/연결/pipe 없이 실행합니다. 읽기 전용 조사나 MCP/원격 검증은 빈 배열로 두고 실제 관측 내용을 기존 validation/risk에 보고합니다.

컨트롤러가 최근 최대 10개 shell 이벤트의 정확한 명령·종료 코드·순서를 대조해 `validation_evidence`를 만듭니다. `passed`, `failed`, `unverified`는 **명령 개수**이지 테스트 케이스 개수가 아닙니다. scope는 `declared_local_command_exits_only`입니다.

- `observed_pass`: 선언된 직접 실행 명령의 성공 종료를 모두 관측했습니다.
- `partial`: 일부 성공 종료만 확인했고 나머지는 근거가 부족합니다.
- `unverified`: 명령 미선언, 기록 누락/잘림, 복합 shell, 이후 변경 가능성, 원격 도구 등으로 성공 종료를 확인하지 못했습니다.
- `failed`: 선언된 명령의 마지막 관측 실행에서 실패를 확인했습니다. completed 보고를 needs_escalation으로 바꿉니다.

나중에 성공한 재실행은 이전 실패를 대체하지만, 같은 call ID의 중복 이벤트는 새 재실행이 아닙니다. 이후 편집·불명확한 도구·다른 shell 활동이 있으면 이전 성공을 최신 검증으로 단정하지 않습니다. 일반 개발 탐색 명령의 과거 실패를 무조건 최종 실패로 삼지 않습니다.

검사기는 자연어의 통과 개수를 파싱하지 않고, 새 테스트나 모델 호출을 하지 않습니다. 완료 보고는 요구사항 전체·테스트 품질·원격 작업 완료의 증명이 아닙니다. 선언 없는 기존 보고서를 계속 수용하되 unverified로 표시합니다. Head는 구체적 위험만 확인하고 routine 재검토·재실행을 추가하지 않습니다.

## 비공개 증거와 테스트

stdout은 작은 구조화된 결과입니다. 원래 brief를 포함한 writer.json은 저장소 밖의 비공개 evidence 디렉터리(0700)에 0600 권한으로 저장하며 기본 stdout에는 노출하지 않습니다. 실제 session/model/elapsed/usage 및 실행 증거도 남습니다. `--full-output`은 상세 비공개 필드까지 공개하므로 공유 전 주의하세요. `--log-dir`은 필요할 때만 사용하는 비공개 원본 로그 옵션입니다. 로그·작업 데이터·인증 정보는 배포 저장소에 커밋하지 마세요.

회귀 테스트의 CLI/SDK는 격리된 fixture입니다. 실제 플러그인 execute 로직과 Python 검증기의 일치, 이벤트 처리와 라우팅/수정 흐름을 검사하지만, 실제 provider나 설치된 SDK와의 호환성 검증을 대체하지 않습니다.
