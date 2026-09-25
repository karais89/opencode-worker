# Windows 네이티브 실행

이 안내는 **Windows의 Python에서 직접 실행**하는 경우다. Worker 실행기에 WSL은 필요하지 않다.
WSL에서 실행한다면 Linux용 명령과 해당 환경의 CLI·인증·SDK를 사용한다. 두 환경의 설치와 설정은 별개다.

## 준비

Windows 10/11 환경에서 Python 3.10+, Git for Windows, Windows용 OpenCode CLI, 기존 provider 인증,
CLI와 호환되는 `@opencode-ai/plugin` SDK를 준비한다. npm 설치판은 `node.exe`도 PATH에 있어야 한다.
실제 프로젝트의 shell·빌드·테스트 도구는 별도로 준비한다. 이 실행기가 Linux 전용 프로젝트 명령을
Windows 명령으로 바꾸거나, Codex의 OS 샌드박스·네트워크 제한을 해제하는 것은 아니다.

PowerShell에서 다음 명령으로 **현재 실행 환경**을 확인한다. 아래 예제는 스킬 디렉터리 기준이다.

```powershell
python --version
git --version
Get-Command opencode -All
python .\scripts\lite.py models
```

`python`이 없다면 설치된 Python의 절대 경로나 `py -3`를 사용한다. `python3`라는 이름이나
Unix shebang 실행을 전제로 하지 않는다. 필요한 프로그램이 없으면 중단하며 자동 설치하지 않는다.

## CLI 선택

기본적으로 PATH의 OpenCode를 찾는다. `opencode.exe`는 인자 배열로 직접 실행한다.
표준 npm 설치의 `opencode.cmd`는 shell로 실행하지 않고, 인접한
`node_modules/opencode-ai/bin/opencode`를 `node.exe`로 실행한다.
프로젝트의 `node_modules/.bin` 설치도 같은 패키지의 진입점을 찾는다.
CLI 자체의 아키텍처 선택·`OPENCODE_BIN_PATH` 처리 로직은 그대로 유지한다.

비표준 설치 위치나 다른 실행 파일을 쓰려면 **하나의 경로**를 지정한다.

```powershell
$env:OPENCODE_WORKER_BIN = 'C:\Tools\opencode.exe'
python .\scripts\lite.py models
```

환경변수 값에 `&`, 인자, 추가 따옴표를 넣어 명령 문자열을 만들지 않는다.
명시한 `.py` 래퍼는 실행기와 같은 Python으로 실행한다. 임의 `.bat`·`.cmd`·`.ps1` 래퍼는
평가하지 않는다. 지원하지 않는 래퍼 오류가 나면 실제 `opencode.exe` 경로를 지정한다.
공백·한글·shell 특수문자가 포함된 인자는 shell 확장 없이 전달한다.

## 모델 설정과 실행

`provider/model`은 `models`에 나온 실제 식별자로 바꾼다. `--project`는 실제 저장소의 절대 경로다.

```powershell
python .\scripts\lite.py set-default 'provider/model'
python .\scripts\lite.py --project 'C:\src\my-project' resolve

@'
GOAL: 구현할 목표
PLAN: 결정된 방향
CONSTRAINTS: 수정 범위와 제한
DONE WHEN: 완료를 확인할 조건
'@ | Set-Content -LiteralPath 'C:\work\task.txt' -Encoding UTF8

python .\scripts\lite.py --project 'C:\src\my-project' run --brief 'C:\work\task.txt' --explicit
```

예제의 `C:\work`는 먼저 존재해야 한다. Windows PowerShell 5.1의 기본 리다이렉션은 UTF-16 파일을
만들 수 있으므로 `-Encoding UTF8`을 명시한다. UTF-8 BOM은 허용하지만 UTF-16은 받지 않는다.
지시 파일은 BOM을 포함해 8 KiB 이하이며, 비어 있으면 실행하지 않는다.
stdout 결과는 ASCII escape를 사용한 JSON이다. `\uXXXX` 형태의 한글은 JSON 파서로 정상 복원된다.
설정 파일은 UTF-8로 저장하고 UTF-8 BOM이 있는 기존 파일도 읽는다.

기본 설정은 `%USERPROFILE%\.config\opencode-worker\config.json`이다. `XDG_CONFIG_HOME`이 있으면
그 아래 `opencode-worker\config.json`을 쓴다. `--config`와 `OPENCODE_WORKER_CONFIG`도 그대로 동작한다.
SDK 탐색은 [설정 안내](lite-v2.md)와 같으며, Windows 홈은 `USERPROFILE`을 우선한다.
한글·공백이 있는 SDK/플러그인 경로는 `file:` URI로 변환한다.

## 잠금과 종료의 의미

Windows에서는 같은 checkout의 대소문자 경로 별칭을 정규화하고 `msvcrt`의 비차단 바이트 잠금을 쓴다.
Linux/macOS에서는 기존 `flock`을 유지한다. 잠금 파일을 지워 중복 실행을 우회하지 않는다.
서로 다른 사용자·HOME/XDG, 별도 WSL 설치, 비협조적 실행까지 통합 잠금하는 기능은 아니다.

stdout/stderr는 제한된 큐와 두 reader thread로 읽는다. stderr 대량 출력이나 파이프 대기는
유효 이벤트 활동으로 간주하지 않으며 기존 inactivity/hard timeout 정책을 유지한다.

Windows Worker는 별도 프로세스 그룹과 **kill-on-close Job Object**에 배치한다.
타임아웃·Ctrl-C에는 Job 종료를 요청하고, 정상 종료 시에도 Job 핸들을 닫아 남은 Job 구성원을 정리한다.
컨트롤러가 강제 종료돼도 마지막 Job 핸들이 닫히면 해당 Job의 프로세스가 정리된다.
Job 생성·할당이 거부되면 계속 실행하지 않고 오류와 부분 결과를 보고한다.

이는 **OS 보안 샌드박스가 아니다**. 프로세스 생성과 Job 할당은 원자적이지 않으므로 할당 전 생성된
자식, 외부 서비스가 대신 시작한 프로세스, 호스트가 거부한 종료까지 보장하지 않는다.
Linux/macOS의 SIGTERM·프로세스 그룹 정리 경로도 유지한다. 실패 후에는 남은 상태를 확인하고
부분 변경을 보존하며 자동 재시도하지 않는다.

## 검증 범위

```powershell
python -m unittest discover -s scripts -p 'test_*.py' -v
node --check assets/submit-result.mjs
```

CI는 가짜 CLI/SDK로 실제 subprocess·파이프·잠금·Job Object 경로를 검증한다.
Windows 전용 검사는 다른 OS에서, POSIX 신호/프로세스 그룹 검사는 Windows에서 건너뛴다.
심볼릭 링크는 Windows 권한이 없을 때 해당 검사만 건너뛴다. Node가 없으면 JavaScript 계약 검사도
건너뛰므로 skipped 사유를 확인한다. 실제 OpenCode 버전·SDK·provider 인증·모델 응답의 통합 호환성은
별도 실사용 검증 대상이며, CI 성공이 이를 증명하지 않는다.

OpenCode upstream은 Windows 직접 실행을 허용하되 최상의 경험에는 WSL을 권장한다.
이 PR의 범위는 Worker 실행기의 네이티브 호환성이다.

## 구현 근거

- [Python selectors: Windows 파이프 미지원](https://docs.python.org/3/library/selectors.html)
- [Python msvcrt: 바이트 범위 잠금](https://docs.python.org/3/library/msvcrt.html)
- [Microsoft Job Objects: 프로세스 수명 관리](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- [OpenCode Windows 안내](https://opencode.ai/docs/windows-wsl/)
