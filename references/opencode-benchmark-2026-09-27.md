# OpenCode 직접 실행과 설치된 Worker 비교 (2026-09-27)

## 조건

Windows 네이티브 환경의 OpenCode CLI `1.18.32`로, [Grok 비교](grok-benchmark-2026-09-26.md)와 같은 `normalize_tags` 작업을 수행했다. 직접 실행과 설치된 `$opencode-worker`는 동일한 초기 Git 커밋 `50ca8da4c1500fead4f97a395fa1f45ef56e9e4b`, 동일한 지시 내용·Worker 프롬프트·`codex-worker` 에이전트 설정을 사용했다. 두 실행 모두 `openai/gpt-6-sol`을 **선택 모델**로 지정했다. 실행기가 실제 모델 ID를 별도 이벤트로 관측한 것은 아니다.

OpenCode 직접 실행은 설치된 공유 번들의 `worker_env()`로 동일한 에이전트·제출 도구 설정을 만들고 CLI 프로세스를 바로 시작했다. Worker 실행은 설치된 `opencode-worker/scripts/lite.py` 진입점으로 시작했다. 경과 시간은 각 프로세스 시작부터 종료까지이며, 토큰은 OpenCode `step_finish` 이벤트의 provider 보고값이다. 비용은 이 경로에서 보고되지 않아 비교하지 않는다.

## 결과

| 경로 | 상태 | 경과 시간 | 보고된 총 토큰 | 독립 확인 | 제출 검증 증거 |
| --- | --- | ---: | ---: | --- | --- |
| OpenCode CLI 직접 실행 | `completed`, 종료 코드 0 | 42.57초 | 53,524 | `tags.py`만 변경, 테스트 2개·수용 조건 4개 통과 | `observed_pass` |
| 설치된 `$opencode-worker` | `completed`, 종료 코드 0 | 44.04초 | 61,798 | `tags.py`만 변경, 테스트 2개·수용 조건 4개 통과 | `unverified` |

두 결과는 변수 이름만 다르고 같은 순서 보존·정규화 동작을 구현했다. Worker가 `python -m unittest -v` 성공을 보고했지만 스트림에서 해당 명령의 최종 종료 증거를 확인하지 못해 `unverified`로 남겼다. 실행기 밖에서 별도로 수행한 `python -m unittest -q`는 두 복제본 모두 통과했다.

이번 한 쌍에서는 Worker가 1.47초 더 걸렸고 보고된 총 토큰은 8,274개 많았다. 단발 실행이며 캐시·모델 응답 변동을 통제하지 못했으므로 속도나 토큰 우열을 일반화하지 않는다. OpenCode의 최종 `changed` 값은 경로에 설명을 덧붙였지만, 독립 Git 확인에서는 두 복제본 모두 실제 변경 파일이 `tags.py` 하나였다.

## 설치 경로 발견

현재 npm 배포는 `opencode.cmd` 옆 패키지에 `node_modules/opencode-ai/bin/opencode.exe`를 둔다. 이전 실행기는 오래된 JavaScript 진입점만 찾아 이 Windows 설치를 거부했다. 알려진 패키지의 네이티브 실행 파일을 shell 없이 직접 찾도록 수정하고 회귀 테스트를 추가했다. 비교에는 수정본을 격리된 홈에 새로 설치해 사용했다. 실제 사용자 스킬 폴더는 바꾸지 않았다.
