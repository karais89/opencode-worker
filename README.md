# opencode-worker — Lite v2 experiment

P008의 실험 브랜치다. main 기준점은 `326ecff40a5a1864d0124e4e652bb90f76a6f54a`로 보존한다.
사용자가 제공한 Lite의 **Codex Head → OpenCode 실행 Worker → 압축 결과 → Head 판단**을 따른다.
모델 선택·권한·결과 관측에 필요한 작은 로컬 실행기는 남긴다. 순수 SKILL.md 한 파일 버전은 아니다.

## 사용

Python 3.10+, Git, macOS/Linux, 기존 OpenCode CLI/provider 인증과 호환되는
`@opencode-ai/plugin` SDK가 필요하다. [설정 안내](references/lite-v2.md)를 먼저 확인한다.
기존 설치본을 자동 교체하지 않는다. Full/Lite는 같은 이름이므로 한 환경에 중복 설치하지 않는다.

```sh
python3 scripts/lite.py models
python3 scripts/lite.py models <provider>
python3 scripts/lite.py set-default <provider/model> --variant <supported-variant>
python3 scripts/lite.py --project /absolute/repo resolve
python3 scripts/lite.py --project /absolute/repo run --brief /absolute/task.txt --explicit
```

`--model` / `--variant`로 이번 실행을 재정의한다. `--read-only`는 큰 읽기 전용 조사에 사용한다.
외부 공유 스킬 원문이 필요하면 승인된 정확한 디렉터리에 한해 `--skill-dir`를 추가한다.
시간 제한은 **활동 기준**이다. OpenCode JSON/event가 `--inactivity-timeout`(기본 300초) 동안
없을 때만 중단하며, 이벤트가 계속 나오면 총 실행 시간이 길어도 유지한다. 선택적
`--hard-timeout`(기본 비활성)만 총 경과 시간 상한을 건다.
설정은 기존 opencode-worker/config.json을 재사용한다. 모델·variant 확인은 설정 시에만 수행하며
정상 실행은 저장된 route로 OpenCode CLI를 한 번 시작한다. 이는 모델 요청 한 번을 뜻하지 않는다.

## 유지한 것과 뺀 것

모델/variant 선택, 프로젝트 override, auto/manual/off, checkout/config 잠금, permission profile,
구조화 제출과 로컬 검증 명령의 종료 코드 대조를 유지한다. 전역 off는 프로젝트 auto보다 우선한다.
Reviewer/Planner, 모델 자동 전환, 재실행·수정 체인, 전역 카탈로그 주입, 거대한 실행 증거 저장은 없다.
`worker.py`는 Full 비교용으로 보존하지만 Lite에서 실행/import하지 않는다.

`lite.py` 외에 streaming.py, submission.py, verification.py, 제출 plugin과 permission asset을 사용한다.
줄 수와 바이트 수를 함께 기록한다. 수정 전 Lite 실행 코드 44,493바이트에서 수정 후 48,174바이트로 늘었다.
고정 작업 패킷은 동일한 예시 입력 기준 657자에서 361자로 줄었다. 어느 쪽도 실제 토큰 절감의 증거는 아니다.

## 결과 해석

stdout에는 작은 JSON 결과만 반환한다. raw JSONL·소스·diff를 Head에 계속 전달하지 않는다.
`model`/`variant`는 선택한 CLI 설정이며 `observed_model`/`observed_variant`는 현재 null이다.
실제 사용 모델을 독립 확인했다고 보고하지 않는다. `worker_used`는 세션 관측이며 과금 호출 횟수가 아니다.
`validation_evidence`는 선언한 명령 exit의 보조 증거다. 요구사항·테스트 품질·원격 완료는 별도 판단한다.
실패·비정상 종료·누락 제출을 completed로 승격하지 않으며 자동 재실행하지 않는다.
비활성 타임아웃은 provider 오류·정상 종료와 구분해 `timeout_kind`와 메시지로 보고한다.

## 검증

```sh
python3 -m unittest discover -s scripts -p 'test_lite_v2.py'
python3 -m unittest discover -s scripts -p 'test_*.py'
```

가짜 CLI/SDK 테스트와 live provider 검증은 구분한다. 설치 ZIP에는 Lite 실행 파일과 전용 테스트를 포함하며,
Full까지 포함하는 전체 회귀 테스트는 P008 저장소에서 실행한다.
검토 결과와 확인하지 못한 항목은 [감사 기록](references/lite-v2-audit-20260922.md)에 기록한다.
main 승격 전에는 같은 과제·모델·초기 checkout으로 비용, 시간, 품질, Head 재작업을 비교해야 한다.
