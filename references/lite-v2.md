# OpenCode Lite v2 설정과 한계

이 문서는 기본 OpenCode 엔진의 세부 설정이다. Grok Build 엔진은 [Grok Build 안내](grok.md)를 본다.

## 목차
환경 · 모델 설정 · 모드와 공유 스킬 · 결과 의미 · 타임아웃 정책 · 생략한 기능 · 검증

## 환경

Python 3.10+, Git, macOS/Linux 또는 네이티브 Windows, PATH의 OpenCode CLI와 기존 provider 인증이 필요하다.
Windows PowerShell 실행·CLI 경로 선택·인코딩은 [Windows 안내](windows.md)를 따른다. WSL은 필수가 아니다.
제출 도구는 설치된 CLI와 호환되는 `@opencode-ai/plugin` SDK의 `dist/tool.js`를 사용한다.
검색 위치는 `OPENCODE_CONFIG_DIR`, `${XDG_CONFIG_HOME:-~/.config}/opencode`,
`${XDG_CACHE_HOME:-~/.cache}/opencode` 아래의 node_modules다. 없으면 중단하며 몰래 설치하지 않는다.
Windows 홈 탐색은 `USERPROFILE`을 우선한다. SDK/플러그인은 공백과 한글 경로를 지원하는 `file:` URI로 전달한다.
SDK는 사용자가 기존 OpenCode 설정 디렉터리에 맞는 버전으로 준비한다. 설정 파일을 덮어쓰지 않는다.
Node.js는 실제 JavaScript 제출 도구와 제품 테스트, Windows npm CLI 진입점 실행에 필요하다.
정상 Worker 실행 중에는 전역 설정이나 인증 파일을 생성·수정하지 않는다.

## 모델 설정

기본 설정은 `${XDG_CONFIG_HOME:-~/.config}/opencode-worker/config.json`이다.
Windows에서 XDG 미지정 시 `%USERPROFILE%\.config\opencode-worker\config.json`이며 자동 이전하지 않는다.
`--config` 또는 `OPENCODE_WORKER_CONFIG`로 대체 파일을 선택할 수 있다.
기존 설정의 writer_default/default, projects, variants, aliases, mode, project_modes를 읽는다.
알 수 없는 설정 필드는 보존하지만 fallback 등은 Lite 실행에서 사용하지 않는다.
새 `grok` 객체의 default/projects는 Grok 모델 전용이며 OpenCode route와 분리된다.
전역 mode/project_modes와 checkout 잠금은 두 엔진이 공유한다.

```sh
python3 scripts/lite.py models
python3 scripts/lite.py models <provider>
python3 scripts/lite.py set-default <provider/model> --variant <supported-variant>
python3 scripts/lite.py --project /absolute/repo set-project <provider/model> --variant <supported-variant>
python3 scripts/lite.py --project /absolute/repo resolve
```

`models <provider>`는 사용 가능한 variant 이름도 출력한다. setter는 저장 전에 실제 모델 목록과
명시한 variant를 확인한다. 인증·가격·성능·provider 내부 모델 정체성까지 검증하는 것은 아니다.
정상 `run`은 목록을 재조회하지 않는다. 잘못된 실행 route는 오류로 멈추고 다른 모델로 재실행하지 않는다.

우선순위: 이번 --model → canonical 프로젝트 route → writer_default → default.
variant: 이번 --variant → 선택 모델의 저장 variants 항목. 프로젝트별 variant 저장소를 새로 만들지 않는다.
같은 모델의 저장 variant는 프로젝트 간 공유되는 기존 형식이다. 다른 값은 --variant로 명시한다.

```sh
python3 scripts/lite.py set-default <provider/model> --clear-variant
python3 scripts/lite.py --project /absolute/repo set-project null
python3 scripts/lite.py set-default null
```

첫 명령은 해당 모델의 저장 variant를 지운다. null은 프로젝트 route 또는 전역 기본 route를 해제한다.
기본 모델을 해제해도 기존 프로젝트 route는 남는다. Worker 전체 중단에는 off를 사용한다.

## 모드와 공유 스킬

```sh
python3 scripts/lite.py set-mode off
python3 scripts/lite.py set-mode auto
python3 scripts/lite.py --project /absolute/repo set-mode manual --project-only
python3 scripts/lite.py --project /absolute/repo set-mode inherit --project-only
```

전역 `off`는 새 Worker 실행을 차단하며 프로젝트 `auto`로 되살아나지 않는다.
이미 실행 중인 프로세스를 종료하는 기능은 아니다.
그 외에는 프로젝트 모드가 전역 모드를 상속/재정의한다. `manual`은 `--explicit`이 필요하다.
`auto`는 런처의 실행 허용 모드다. 스킬의 사용 조건을 무시하고 모든 코딩 요청을 자동 위임한다는 뜻이 아니다.

실행에는 반드시 --project를 지정한다. 읽기 전용 조사는 --read-only, 필요하고 승인된 외부 스킬 원문은
--skill-dir /absolute/known-skill로 지정한다. 경로에는 실제 SKILL.md가 있어야 하며 원문 수정은 금지된다.
정규화한 경로에 `*` 또는 `?`가 있으면 중단한다. 정확한 경로가 권한 패턴으로 확대되는 것을 막기 위해서다.
자동 전체 카탈로그 탐색, 전역 설정 조사, 누락된 도구 자동 설치는 하지 않는다.
원래 `.agents/worker-capabilities.json` 자동 발견과 광범위 공유 스킬 자동 허용은 이식하지 않았다.
특정 CLI/MCP/Editor가 실제 연결됐는지는 별개다. 필요한 도구가 없으면 blocker로 보고한다.

잠금은 기본 opencode-worker/locks의 canonical checkout 해시를 사용한다.
--config와 무관하며 같은 사용자 HOME/XDG 설정 공간에서 공유한다.
서로 다른 HOME/XDG, 비협조적 프로세스까지 막는 OS 전역 보장은 아니다. 설정 변경도 같은 .lock을 사용한다.
Windows는 경로 대소문자를 정규화하고 비차단 `msvcrt` 잠금을, Linux/macOS는 `flock`을 사용한다.

## 결과 의미

결과 제출 schema와 1,800자 제한은 기존 제출 모듈을 재사용한다. 제출 뒤 개발 도구 호출이 있으면 결과가 무효다.
최상위 `status`가 실행 판정이다. `result.status`는 Worker의 제출값이므로 단독으로 믿지 않는다.
오류·거부·정상 stop 없음·미종료 도구·누락 또는 충돌 제출은 `needs_escalation`이다.
새 `step_start`나 도구 이벤트가 오면 이전 stop은 더 이상 정상 종료 근거가 아니다.
세션 이벤트에는 동일한 유효 sessionID가 필요하며, 세션 생성 전 오류만 ID 생략을 허용한다.
부분 report는 유효하게 수집된 경우 진단용으로 보존한다.
CLI 한 번 실행은 모델 요청 한 번이 아니다. 세션 내 탐색·도구·모델 요청은 여러 번 발생할 수 있다.

model, variant는 선택한 CLI 인자다. 현재 CLI JSONL만으로 모델 정체성을 독립 확인하지 못하므로
observed_model과 observed_variant는 null이다. 추가 export/debug/모델 호출을 몰래 하지 않는다.
토큰은 OpenCode step_finish의 provider-reported 수치다. 청구 금액이나 Codex 사용량으로 환산하지 않는다.

validation_commands는 마지막 로컬 검증의 직접 실행 명령을 최대 10개 선언한다.
복합 shell·기록 누락·변경 전 검사·원격/MCP는 unverified일 수 있다. 이를 실패로 단정하거나
형식만 맞추려고 검증을 반복하지 않는다. observed_pass는 선언 명령의 exit 0 관측까지만 뜻한다.
선언된 검증 도구의 상태가 `error`이면 exit 코드가 없어도 실패다. 반대로 완료된 명령의
exit가 없으면 `unverified`이며, 이를 성공이나 실패로 추측하지 않는다.
최근 10개 명령만 결과 대조에 사용하지만 호출 ID는 최대 10,000개까지 기억한다.
오래된 이벤트의 중복 수신을 새 검증으로 세지 않는다. 추적 한도 초과·충돌은 완료 판정을 차단한다.
프로세스 종료, 테스트 개수, 요구사항 충족, 원격 작업 완료를 혼동하지 않는다.

## 타임아웃 정책

시간 제한은 절대 경과 시간이 아니라 **유효한 이벤트 활동 기준**이다. OpenCode JSON/event가
`--inactivity-timeout`(기본 300초) 동안 하나도 소비되지 않을 때만 Worker를 중단한다.
단조 시계(`time.monotonic`)로 마지막 활동 이후 시간을 재며, 정상 도구/모델 이벤트가
도착할 때마다 비활성 마감이 갱신된다. 따라서 OpenCode 이벤트가 계속 나오는 한 총 실행
시간이 길어도(예: 600초 초과) 중단하지 않는다. 컨트롤러의 stderr 진행/heartbeat 출력은
활동이 아니므로 비활성 타이머를 초기화하지 않는다.
숫자·빈 객체·알 수 없는 유형·다른 세션의 출력도 활동으로 인정하지 않는다.
현재 처리하는 유형은 `step_start`, `step_finish`, `text`, `reasoning`, `tool_use`, `error`다.
CLI가 새로운 유형을 추가하면 완료 판정을 차단하므로, 해당 버전의 실제 이벤트를 검토하고 지원을 추가한다.

```sh
python3 scripts/lite.py --project /absolute/repo run --brief /absolute/task.txt
python3 scripts/lite.py --project /absolute/repo run --brief /absolute/task.txt --inactivity-timeout 300
python3 scripts/lite.py --project /absolute/repo run --brief /absolute/task.txt --hard-timeout 7200
```

`--hard-timeout`은 명시적으로 지정할 때만 켜지는 선택적 총 경과 시간 상한이며 기본값은
없음(비활성)이다. 기존 `--timeout`은 같은 hard limit의 별칭으로 남겨 명시적으로 지정하면
동일하게 총 상한으로 동작한다. 다만 기본값은 더 이상 1800초 총 제한이 아니므로 활성 세션이
600초에 종료되지 않는다. `--inactivity-timeout`과 `--hard-timeout` 모두 유한한 양수여야 한다.

비활성 타임아웃은 provider 오류나 정상 프로세스 종료와 구분해 `timeout_kind`와 메시지로
보고한다. 비활성 중단 때도 부분 변경을 보존하고 자동 재실행하지 않는다. 호스트가 자식
프로세스 종료를 막으면 정리를 보장할 수 없다. 남은 실행 상태를 먼저 확인한다.
Linux/macOS의 SIGTERM은 Ctrl-C와 같은 정리 경로로 처리하고 기존 신호 핸들러를 복원한다.
메인 스레드에서만 신호 핸들러를 설치한다. POSIX의 SIGKILL, 다른 프로세스 그룹으로 이탈한 자식,
호스트가 거부하는 종료까지 보장하지 않는다. 별도 스레드에서 라이브러리로 실행할 때는 호스트가 종료를 관리한다.
Windows는 kill-on-close Job Object로 자식 수명을 관리한다. 생성과 Job 할당 사이의 자식,
외부 서비스가 시작한 프로세스까지 포함하는 보장은 아니다. Job 할당 실패 시 중단한다.
권한 프로필이나 Job Object를 보안 샌드박스로 설명하지 않는다.

## 생략한 기능

자동 fallback/replay, --fix-from 수정 체인, 전체 Skill 카탈로그 주입, 상세 evidence 저장은 없다.
기존 streaming/submission/verification 모듈을 재사용한다. 파일이 존재하거나 import됐다는 이유만으로
추가 모델 턴이 생기지는 않는다. 반대로 런처만 줄 수를 세어 전체가 가볍다고 주장하지 않는다.

## 검증

```sh
python3 -m unittest discover -s scripts -p 'test_lite_v2.py'
python3 -m unittest discover -s scripts -p 'test_*.py'
```

Windows는 같은 명령에 `python`을 사용한다. CI는 Windows/Linux/macOS의 Python 3.10/3.13을 검사한다.
테스트는 가짜 CLI/SDK와 실제 JSONL 처리 경로를 사용한다. 실제 provider 통합·품질·사용량 절감을 입증하지 않는다.
Node.js가 없으면 JavaScript 제출 계약 테스트는 건너뛰므로 skipped 수를 확인한다.
OS 전용 프로세스/경로 검사는 해당 OS에서 실행한다. Windows에서 심볼릭 링크 생성 권한이 없을 때는 해당 검사만 건너뛴다.
비용을 쓰지 않는 회귀 검사와 실사용 평가는 [스킬 평가 기준](skill-evaluation.md)에서 구분한다.
