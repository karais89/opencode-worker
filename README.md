# OpenCode Worker — Lite v2

Codex/ChatGPT가 계획을 세우고 OpenCode에 구현을 맡긴 뒤, 짧은 결과를 받아 판단하는 스킬이다.
**Head → 작업 지시 → OpenCode 한 세션 → 구조화된 결과 → Head**를 유지한다.
한 세션 안에서는 모델 요청, 파일 탐색, 수정과 테스트가 여러 번 발생할 수 있다.

## 무엇이 들어 있나

| 파일 | 역할 |
| --- | --- |
| [SKILL.md](SKILL.md) | 모델이 따르는 한국어 실행 절차와 판단 원칙 |
| `scripts/lite.py` | 설정·모델 선택·잠금·단일 세션 실행 |
| `scripts/streaming.py` | JSONL 처리, 활동 타임아웃, 프로세스 정리, 제한된 관측 기록 |
| `scripts/platform_support.py` + `scripts/windows_job.py` | OS별 잠금·CLI 실행·파이프 읽기·프로세스 수명 관리 |
| `scripts/submission.py` + `assets/submit-result.mjs` | 구조화된 결과 제출과 형식 검증 |
| `scripts/verification.py` | 선언된 로컬 검증 명령과 관측 종료 코드 대조 |
| `assets/worker-agent.json` | 도구 권한 프로필. OS 샌드박스가 아님 |
| `scripts/test_*.py` | 유료 모델을 호출하지 않는 회귀 테스트 |

실행기는 남기되 별도 Planner/Reviewer, 자동 재시도·모델 전환·수정 체인, 전체 스킬 목록 주입은 하지 않는다.
코드 줄 수와 LLM 사용량은 다른 지표다. 이 구조만으로 절감률을 보장하지 않는다.

## 시작하기

필요한 환경은 **macOS/Linux 또는 네이티브 Windows, Python 3.10+, Git, OpenCode CLI, 기존 provider 인증,
CLI와 호환되는 `@opencode-ai/plugin` SDK**다. Node.js는 JavaScript 제출 도구 검증과 Windows npm 설치판 실행에도 필요하다.
이 스킬만 올린다고 CLI가 없는 ChatGPT 환경에서 실행할 수 있는 것은 아니다.
자세한 SDK 위치와 설정은 [설정 안내](references/lite-v2.md)를 확인한다. 한 환경에는 이 버전만 설치한다.

스킬 디렉터리에서 최초 모델 설정을 한다. 아래 `<provider/model>`은 실제 사용 가능한 식별자로 바꾼다.

```sh
python3 scripts/lite.py models
python3 scripts/lite.py models <provider>
python3 scripts/lite.py set-default <provider/model>
```

지원되는 실행 강도를 지정할 때만 `--variant <supported-variant>`를 추가한다.
작업 지시는 별도 UTF-8 파일에 다음 네 항목으로 적는다. 전체 크기는 8 KiB 이하이다.

```text
GOAL: 구현할 목표
PLAN: 이미 결정된 방향
CONSTRAINTS: 수정 범위와 제한 사항
DONE WHEN: 완료를 확인할 조건
```

대상은 지시 파일만 둔 임시 폴더가 아니라 실제 저장소여야 한다.

```sh
python3 scripts/lite.py --project "/absolute/repo" resolve
python3 scripts/lite.py --project "/absolute/repo" run --brief "/absolute/task.txt" --explicit
```

`--project`와 `--config`는 `run` 앞에, 나머지 실행 옵션은 뒤에 둔다.
정상 `run`은 모델 목록을 재조회하지 않고 저장된 모델로 OpenCode CLI를 한 번 시작한다.

### Windows / PowerShell

WSL 없이 Windows Python으로 실행할 수 있다. `python3` 대신 `python` 또는 `py -3`를 사용한다.
`provider/model`과 경로는 실제 값으로 바꾼다.

```powershell
python .\scripts\lite.py models
python .\scripts\lite.py set-default 'provider/model'
python .\scripts\lite.py --project 'C:\src\my-project' run --brief 'C:\work\task.txt' --explicit
```

지시 파일은 UTF-8로 저장한다(BOM 허용, UTF-16 미지원). 표준 npm 설치판의 `.cmd`는 shell로 실행하지 않고
Node 진입점을 사용한다. 비표준 설치는 `OPENCODE_WORKER_BIN`에 실제 실행 파일 경로를 지정한다.
설치·설정 위치·종료 한계는 [Windows 안내](references/windows.md)를 확인한다.
이 변경은 Codex 샌드박스 제한을 해제하거나 Linux 전용 프로젝트 도구를 자동 변환하지 않는다.

## 자주 쓰는 선택 사항

| 필요 | 옵션 또는 명령 |
| --- | --- |
| 이번 실행의 모델·강도 변경 | `run ... --model <provider/model> --variant <variant>` |
| 파일 수정 없는 조사 | `run ... --read-only` |
| 알려진 외부 스킬 읽기 | `run ... --skill-dir "/absolute/known-skill"` |
| 활동 없는 시간 조정 | `run ... --inactivity-timeout 300` |
| 전체 실행 시간도 제한 | `run ... --hard-timeout 7200` |
| 새 Worker 실행 차단 | `python3 scripts/lite.py set-mode off` |

`off`는 프로젝트 설정이나 `--explicit`으로 우회할 수 없다. 이미 실행 중인 프로세스를 종료하지는 않는다.
기본 시간 제한은 유효 이벤트가 없는 300초다. 유효 이벤트가 계속 오면 총 실행 시간에는 기본 상한이 없다.

## 결과를 어디까지 믿을 수 있나

stdout은 압축된 JSON 결과, stderr는 진행 표시다. 진행 표시만으로 성공을 판단하지 않는다.
**최상위 `status`가 실행 판정**이며, 안쪽 `result.status`는 Worker가 보고한 값이다.
오류가 발생하면 내부 보고가 `completed`여도 최상위는 `needs_escalation`일 수 있다.

| 정보 | 의미와 한계 |
| --- | --- |
| `validation_evidence` | 선언한 로컬 명령의 종료 코드만 대조한다. 구현 품질이나 테스트 개수를 증명하지 않는다. |
| `partial` / `unverified` | 증거가 부족한 상태다. 실패로 단정하거나 형식만 맞추려 재실행하지 않는다. |
| `model` / `variant` | CLI에 전달한 선택값이다. 독립 확인값 `observed_*`는 현재 null이다. |
| `worker_tokens` | OpenCode가 보고한 토큰과 누락 범위다. 실제 청구 금액·Head 사용량과 다르다. |

비정상 종료나 누락·충돌 제출을 완료로 처리하지 않는다. 부분 변경은 보존하고 자동 재실행하지 않는다.
권한 프로필은 임의 shell/MCP/외부 프로세스를 완전히 격리하는 보안 경계가 아니다.
Linux/macOS는 SIGTERM·Ctrl-C·타임아웃에 같은 그룹의 자식 정리를 시도한다.
Windows는 kill-on-close Job Object를 사용하지만 생성 직후 할당 전의 자식·외부 서비스로 시작한 프로세스·
호스트의 종료 거부까지 보장하지 않는다. 재실행 전에 남은 실행 상태를 확인한다.

## 검증과 평가

```sh
python3 -m unittest discover -s scripts -p 'test_*.py' -v
node --check assets/submit-result.mjs
```

Windows에서는 같은 검사에 `python`을 사용한다. CI는 Windows/Linux/macOS에서 Python 3.10/3.13을 검사한다.
테스트는 가짜 CLI와 SDK 인터페이스를 사용한다. OS 전용 검사와 Node.js가 없는 환경의 JavaScript 계약 검사는 skipped로 표시된다.
실제 OpenCode/SDK/provider의 호환성, 코드 품질, Head 토큰 절감은 별도의 실사용 검증이 필요하다.

[스킬 평가 기준](references/skill-evaluation.md)은 자동으로 확인할 수 있는 조건과 LLM 행동 평가를 구분한다.
[2026-09-23 감사 기록](references/audit-2026-09-23.md)에는 재현한 결함, 수정 범위와 미검증 항목을 남긴다.
