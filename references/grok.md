# Grok Build Worker

## 준비와 선택

Grok Build CLI(`grok`), 기존 `grok login` 인증 또는 `XAI_API_KEY`, Python 3.10+와 Git이 필요하다.
실제 Git 저장소와 HEAD 커밋을 대상으로 한다. `grok.com/build`의 CLI를 직접 실행하며,
OpenCode의 Grok 모델 경로를 사용하는 방식이 아니다. 설치나 로그인은 실행기가 대신하지 않는다.
Windows 네이티브 `grok.exe`를 지원한다. 비표준 설치는 실행 파일 **하나의 경로**를
`GROK_WORKER_BIN`에 지정한다. 명령 문자열이나 shell wrapper를 넣지 않는다.

아래 `<grok-skill>`은 설치된 `grok-worker` 디렉터리다. 해당 스킬의 얇은 진입점은
공유 번들의 실행기를 호출한다.

```powershell
python '<grok-skill>\scripts\lite.py' models --engine grok
python '<grok-skill>\scripts\lite.py' set-default grok-4.7 --engine grok
python '<grok-skill>\scripts\lite.py' --project 'C:\src\repo' resolve --engine grok
python '<grok-skill>\scripts\lite.py' --project 'C:\src\repo' run --engine grok --brief 'C:\work\brief.txt' --explicit
```

`run --engine grok --model <id>`는 한 번만 모델을 바꾼다. 모델 선택 우선순위는
이번 `--model` → Grok 프로젝트 설정 → Grok 전역 설정 → Grok CLI 기본 모델이다.
선택한 모델은 OpenCode 설정과 별도의 `grok` 설정에 저장된다. 모델이 없으면
CLI 기본 모델을 쓰며, `run` 중에는 모델 목록을 재조회하지 않는다.

```powershell
python '<grok-skill>\scripts\lite.py' --project 'C:\src\repo' set-project grok-4.7 --engine grok
python '<grok-skill>\scripts\lite.py' --project 'C:\src\repo' set-project null --engine grok
python '<grok-skill>\scripts\lite.py' set-default null --engine grok
```

기존 `mode`와 프로젝트 잠금은 두 엔진이 공유한다. `off`면 Grok도 실행하지 않는다.
`--variant`, `--read-only`, `--skill-dir`는 Grok 경로에서 현재 지원하지 않으며 실행 전에 중단한다.
읽기 전용 조사와 외부 스킬 경로가 필요하면 OpenCode 경로의 해당 옵션을 사용한다.

## 실행과 결과

실행기는 한 Grok 세션을 `--prompt-file`, `--cwd`, `--output-format streaming-json`,
`--permission-mode auto`, `--no-plan`, `--no-subagents`로 시작한다. 기본 ask 모드에서는
헤드리스 편집 승인이 취소되므로 Grok의 auto 모드를 명시한다. 이 모드에서도 위험한 호출은
승인을 요구하거나 거부될 수 있다. 사용자 Grok 설정, 훅, 플러그인과 deny 규칙은
CLI가 그대로 적용한다. 단, Worker 세션 간 암묵적 컨텍스트 유입을 막기 위해 실행기는
`GROK_MEMORY=0`을 강제하여 Grok의 cross-session memory를 비활성화한다. 실행기가 `--always-approve`를 추가하거나 거부를 우회하지 않는다.
권한 거부와 provider 오류는 완료로 처리하지 않는다. OS 프로세스 수명 관리는 OpenCode와
같은 플랫폼 도구를 사용하지만 보안 샌드박스를 제공하지 않는다.

Grok 최종 답변은 `status`, `changed`, `validation`, `risk`, 선택적인
`validation_commands`를 가진 JSON 객체여야 한다. 실행기는 기존 보고 형식을 검증하고,
정상 `end` 이벤트 및 실행 전후 Git 변경 경로와 결과의 `changed`를 대조한다.
기존 작업 트리의 변경은 `preexisting_changes`로 따로 표시한다. 결과가 불완전하거나
경로가 다르면 `needs_escalation`으로 반환하고 자동 재실행하지 않는다.

Grok의 execute/터미널 도구 이벤트(`kind=execute` 또는 알려진 shell tool alias)에 명령과 종료 코드가 있으면 선언한 최종 로컬 검증 명령과
대조한다. 누락되거나 이후 다른 도구가 실행된 경우에는 `unverified`로 둔다.
`observed_pass`도 명령의 exit 0만 뜻하므로 Head는 필요한 수락 검사를 독립적으로 확인한다.
`worker_tokens`, `worker_cost_usd`, `observed_model`은 Grok CLI가 보고한 값만
사용하며, 보고되지 않으면 추측하지 않는다. 한 번의 세션에 여러 모델 요청과 도구 호출이
있을 수 있다. 경과 시간은 런처의 관측값이며 과금 횟수와 다르다.

유효한 Grok JSON 이벤트가 기본 300초간 없으면 중단한다. 공식 `plan`/`auto_compact_*` lifecycle 이벤트는 정상 활동으로 허용하며, `max_turns_reached`는 완료를 막는 오류 증거로 기록한다. 반복되는 도구 목록,
stderr 경고, 런처 heartbeat는 활동으로 세지 않는다. `--hard-timeout`은 별도의 선택적
총 경과 시간 제한이다. 중단 뒤 부분 변경은 보존하고, 남은 프로세스 상태 확인 전
재실행하지 않는다.
