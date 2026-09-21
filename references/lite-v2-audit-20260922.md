# Lite v2 재검증 · 2026-09-22

## 기준과 범위

P008 main: `326ecff40a5a1864d0124e4e652bb90f76a6f54a`.
이전 Lite v2: `1fae3e99f0744a0239926d5ea45399683e7ed6fe`.
수정은 experiment/lite-v2에만 적용한다. main, 사용자 provider 인증, 전역 설정, 배포본은 변경하지 않는다.
원래 업로드 Skill의 Head → 정확한 저장소/짧은 계획 → OpenCode → 결과 → Head 원칙을 기준으로 비교했다.

## 재현한 결함과 수정

| 항목 | 이전 동작 | 수정 |
|---|---|---|
| 완료 판정 | 제출 뒤 provider 오류, stop 누락, length 종료도 completed | 정상 종료·세션·제출·오류 상태를 함께 검사 |
| 모델 기록 | 설정값을 실제 사용 모델처럼 설명 | selected_cli_arguments와 독립 관측 null을 구분 |
| checkout 잠금 | --config 위치별로 잠금 분리 | Full의 기본 설정 공간과 canonical checkout 키를 공유 |
| 설정 저장 | 동시 변경 잠금과 설정 타입 검증 누락 | Full과 같은 .lock 아래 read-modify-write, 잘못된 타입 거부 |
| 모델 설정 | 지원 여부 확인 없이 model/variant 저장 | 설정 시에만 inventory/variant 검사, run에서는 조회하지 않음 |
| 조사 권한 | 조사 위임 문구는 있으나 read-only 옵션 없음 | native 읽기 전용 권한과 결과/관측 도구 검사 복원 |
| 공유 Skill | 외부 원문 접근 경로 누락 | 승인된 정확한 --skill-dir만 읽기 허용, 전체 홈 스캔 없음 |
| 대상 저장소 | cwd 기본값으로 scratch 실행 가능 | --project 명시, home/root 및 명백한 빈/scratch-only 대상 거부 |
| 실패와 재작업 | 중단 시 부분 결과 누락, 무조건 두 번째 Worker 금지 문구 | 부분 결과 보존, 자동 재실행 금지와 사용자 후속 요청을 구분 |
| 회귀 fixture | fake executable에 literal backslash-n 사용 | 두 줄만 수정, 검증 assertions는 유지 |

`off`는 전역 중단으로 강화했다. 프로젝트 auto가 이를 되살리지 않는다. 이는 Full의 프로젝트 우선 규칙과 의도적인 차이다.
저장 variant는 기존처럼 모델별이며 프로젝트별 독립 variant 저장소는 추가하지 않았다.
`worker_used`는 세션 관측일 뿐 과금 발생/모델 요청 횟수의 확정 증거가 아니다.

## 복잡도 측정

대상: launcher + streaming.py + submission.py + verification.py + submit-result.mjs.
권한 JSON, 문서, 테스트, 미사용 Full 파일은 아래 코드 합계에서 제외한다.

| 버전 | 실행 코드 줄 수 | 실행 코드 바이트 |
|---|---:|---:|
| Full 기준 | 1,269 | 71,080 |
| 이전 Lite v2 | 1,009 | 44,493 |
| 수정 Lite v2 | 950 | 48,174 |

런처는 431줄/15,023바이트에서 372줄/18,704바이트로 바뀌었다.
줄 수 감소를 경량화의 증거로 사용하지 않는다. 정확성/누락 기능 보완 때문에 바이트는 늘었다.
동일 예시 `/repo`, `GOAL\nExample`의 고정 작업 패킷은 657자에서 361자로 줄었다.
이는 모델 system prompt, AGENTS, 도구 schema, 실행 출력과 reasoning을 포함한 전체 토큰 측정이 아니다.
정상 경로의 OpenCode CLI 실행은 한 번이다. 세션 내 모델 요청은 여러 번 가능하다.
공용 스트림/제출/검증 코드는 남아 있으며 순수 SKILL.md 한 파일 대체라고 주장하지 않는다.

## 실행 증거

- O196: 이전 Lite에서 provider 오류/stop 없음/length 종료를 completed로 오판하는 세 사례와 config별 잠금 분리를 재현.
- O198: 변경하지 않은 main을 같은 WebJjonku 샌드박스에서 실행. 109개 중 105개 통과, 4개 실패/오류.
- O201: 수정 Lite 전용 30개 및 공용 17개 통과. 실제 프로세스로 실행한 fake CLI + JSONL + 구조화 결과 경로 포함.
- O202: 수정 후 전체 139개 중 137개 통과. 두 fixture 오류는 해소, 아래 두 제한은 계속 재현.
- O205: Lite 32/32 PASS; Full/Lite cross-config checkout lock PASS.
- O206: final full suite 141, pass 139, error 1, failure 1; package source SHA-256 match PASS.
- O203: 전체 실행 코드 바이트/줄 수와 고정 패킷 크기를 실제 측정.
- 별도 격리 컨테이너: inventory parser 검증을 추가한 Lite 전용 32개 통과.

실제 provider 요청, 유료 모델 E2E, Codex 사용량 및 A/B 품질·비용 측정은 실행하지 않았다.
테스트용 CLI/SDK fixture를 실제 설치된 OpenCode와의 호환성 증명으로 취급하지 않는다.

## 남은 제한

1. WebJjonku 샌드박스가 테스트용 외부 `.env` 생성을 거부한다. 보호 정책을 풀거나 테스트를 삭제/skip하지 않았다.
2. 자식 프로세스 종료 테스트의 marker가 생성되는 실패가 main과 수정본 모두에서 재현된다. 종료 성공을 보장하지 않는다.
   이 문제의 근본 원인을 코드/호스트 정책 중 하나로 단정하지 않았다. 타임아웃 후에는 상태를 확인하기 전 재실행하지 않는다.
3. 실제 모델 식별은 독립 관측하지 못하므로 observed_model/observed_variant는 null이다.
4. 기존 자동 fallback, --fix-from, 전체 공유 카탈로그/manifest 자동 발견, 상세 실행 evidence 저장은 이식하지 않았다.
   필요한 외부 원문은 명시적으로 연결하고 필요한 진단은 해당 문제에 한해 수행한다.
5. 단순한 경로/명령 permission은 OS 샌드박스가 아니며, CLI/MCP/Editor의 실제 연결 여부는 별도 검증해야 한다.

따라서 상태는 **검증된 수정 사항을 포함한 실험 후보**이며, main 승격 또는 production-ready 판정이 아니다.
