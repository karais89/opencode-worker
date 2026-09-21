---
name: opencode-worker
description: Codex/ChatGPT Head가 목표와 정확한 저장소를 정하고 OpenCode Worker 한 세션에 구현, 다중 파일 변경, 버그 수정, 테스트 및 디버깅을 위임한다. 큰 읽기 전용 코드 조사도 지원한다. $opencode-worker 또는 명시적인 OpenCode 위임 요청에 사용한다. 단순 질문, 설계만 필요한 요청, 아주 작은 편집과 Worker 사용 거부에는 사용하지 않는다.
---

# OpenCode Worker Lite v2

**Head → 짧은 작업 지시 → OpenCode 한 세션 → 압축 결과 → Head 판단**을 유지한다.
`worker.py`를 호출하지 않는다. `lite.py`는 설정·잠금·실행·결과 관측만 담당한다.
별도 Planner/Reviewer, 모델 자동 전환, 자동 재실행·수정 체인을 만들지 않는다.
**1회는 OpenCode CLI 실행/세션 수다.** 세션 안의 모델 요청·도구 호출·테스트 수정은 여러 번일 수 있다.

## 계획과 대상

사용자 요구, 중요한 설계 결정, 제약, 완료 조건을 짧게 정한다. 이미 확인한 정보만 사용하고
Worker가 할 구현 탐색을 먼저 반복하지 않는다. 정확한 실제 저장소 루트를 확인한다.
홈·상위 폴더를 뒤져 프로젝트를 추측하지 않는다. 빈 폴더나 작업 지시 파일만 있는 scratch는 실행 대상이 아니다.
기존 사용자 변경을 보존한다. brief에는 GOAL / PLAN / CONSTRAINTS / DONE WHEN만 자기완결적으로 적는다.

## 실행

```sh
python3 <this-skill>/scripts/lite.py --project /absolute/repo run --brief /absolute/brief.txt
```

명시적 위임 요청에는 `--explicit`을 붙인다. 설정된 provider 사용에 대해 대화형 동의를 중복 요구하지 않는다.
호스트가 별도 보안 승인을 요구하면 정상 권한 UI를 사용하고 거부를 우회하지 않는다.

모델은 **이번 `--model` → 프로젝트 route → writer_default/default** 순서다.
variant는 이번 `--variant`가 우선이고 없으면 선택 모델의 저장 variant를 사용한다.
설정이 없으면 멈추며 OpenCode 기본 모델로 몰래 대체하지 않는다.
정상 실행에는 모델 목록 조회가 없다. 최초 설정·변경 때만 [설정 안내](references/lite-v2.md)를 읽는다.
전역 `off`는 프로젝트 설정과 `--explicit`으로도 우회하지 않는다. `manual`은 명시적 요청만 허용한다.

큰 **읽기 전용 조사**에는 `--read-only`를 사용한다. native read/glob/grep/list와 결과 제출만 허용하며
shell·테스트·수정·임의 MCP는 금지한다. 결과는 changed=[]와 파일/줄 근거, 미확인 사항으로 받는다.
실행이 필요한 조사를 읽기 전용으로 완수했다고 주장하지 않는다.

외부 공유 스킬 원문이 실제로 필요한 경우에만, 이미 알고 있고 사용이 승인된 정확한 스킬 디렉터리를
`--skill-dir /absolute/known-skill`로 지정한다. 읽기만 허용하며 전역 스킬 카탈로그를 먼저 스캔하지 않는다.
일반 Writer의 프로젝트 도구·shell·테스트·빌드 능력은 유지한다. 권한 profile은 OS 샌드박스가 아니다.
동일 checkout의 잠금을 삭제하거나 다른 config로 우회하지 않는다.

## 결과와 실패

Worker는 마지막 개발/검증 뒤 `codex_worker_submit_result`를 호출한다. 제출 형식 오류만 같은 세션에서 고친다.
자연어 마지막 답변을 기계 프로토콜로 파싱하지 않는다. 실행 오류·거부·비정상 종료·제출 실패는 완료가 아니다.
부분 변경을 보존하고 자동 재실행하지 않는다. 사용자가 후속 수정을 명시하면 별도의 새 작업으로 다룬다.

Head는 요구사항과 result의 changed / validation / risk를 대조한다. 전체 소스·diff·테스트를 습관적으로 반복하지 않는다.
구체적 불일치·검증 실패·요구사항 누락·고위험 변경에만 좁게 확인한다.
`validation_evidence`는 선언한 로컬 명령 종료 코드의 관측일 뿐 테스트 개수·품질·원격 완료 증명이 아니다.
partial/unverified는 무조건 실패가 아니다. 증거 형식을 맞추려고 정상 검증을 다시 시키지 않는다.

## 보고

구현 결과, 실제 확인한 검증, 남은 위험을 보고한다. stderr 진행 정보는 관측한 단계/개수일 뿐 성공 판정이 아니다.
`model` / `variant`는 **CLI에 지정한 설정값**이다. `observed_model` / `observed_variant`가 null이면
실제 모델을 독립 확인했다고 말하지 않는다. session_id도 과금된 모델 호출 횟수의 증거는 아니다.
시간은 관측값만, 토큰은 provider-reported 수치와 누락 범위를 함께 사용한다. 절감률을 추측하지 않는다.
