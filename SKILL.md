---
name: coding-worker
description: Codex/ChatGPT가 구현·다중 파일 수정·버그 수정·테스트·디버깅을 OpenCode 또는 Grok Build CLI Worker 한 세션에 위임한다. 사용자가 $coding-worker를 지정하거나 Worker/외부 코딩 에이전트에 구현 작업을 맡기라고 명시한 경우 사용한다. OpenCode 또는 Grok Build를 직접 지정한 경우에도 사용한다. 단순 질문, 설계만 필요한 요청, 아주 작은 편집, Worker 사용을 거부한 요청에는 사용하지 않는다.
---

# Coding Worker — OpenCode / Grok Build

**Head의 계획 → 짧은 작업 지시 → 선택한 Worker 한 세션 → 압축 결과 → Head의 판단**을 유지한다.
`worker.py`는 설정·잠금·실행·결과 수집만 담당한다. `lite.py`는 이전 사용자를 위한 호환 진입점이다. 별도 Planner/Reviewer, 자동 모델 전환,
재실행·수정 체인을 추가하지 않는다. 한 세션 안의 모델 요청과 도구 호출은 여러 번일 수 있다.
기본 엔진은 OpenCode다. 사용자가 Grok Build를 명시한 경우 `--engine grok`을 사용하고
[Grok Build 안내](references/grok.md)를 읽는다. 한 엔진 실패를 다른 엔진으로 자동 재실행하지 않는다.

## 1. 대상과 완료 조건 정하기

확인된 실제 저장소의 절대 경로를 사용한다. 홈·상위 폴더를 뒤져 대상을 추측하지 않는다.
빈 폴더나 지시 파일만 있는 임시 폴더는 대상이 아니다. 기존 사용자 변경을 보존한다.
Worker가 할 코드 탐색을 Head가 먼저 반복하지 말고, 이미 아는 정보로 지시 파일을 작성한다.

```text
GOAL: 달성할 목표
PLAN: 이미 결정된 핵심 방향
CONSTRAINTS: 작업 범위와 지켜야 할 제약
DONE WHEN: 확인 가능한 완료 조건
```

지시는 자기완결적인 UTF-8 파일로 저장하고 **8 KiB 이하**로 유지한다.
대상이 불명확하면 임의 대체하지 말고 확인한다.

## 2. Worker 실행하기

아래 자리표시자를 실제 경로로 바꾼다. 명시적인 위임 요청에는 `--explicit`을 붙인다.

```sh
python3 "<this-skill>/scripts/worker.py" --project "/absolute/repo" \
  run --brief "/absolute/brief.txt" --explicit
```

Windows 네이티브 환경에서는 `python` 또는 `py -3`와 Windows 절대 경로를 사용한다. WSL을 전제로 하지 않는다.
PowerShell 명령·UTF-8 지시 파일·CLI 위치가 필요할 때만 [Windows 안내](references/windows.md)를 읽는다.

```powershell
python '<this-skill>\scripts\lite.py' --project 'C:\src\repo' run --brief 'C:\work\brief.txt' --explicit
```

Grok Build를 요청받았다면 같은 지시 파일에 `run --engine grok`을 사용한다. Grok 모델은
OpenCode의 `provider/model` 경로와 별도로 설정하며, 미설정 시 Grok CLI 기본 모델을 따른다.

설정된 provider 사용에 대한 동의를 반복해서 묻지 않는다. 호스트가 요구하는 보안 승인은
정상 UI로 받고 거부를 우회하지 않는다. 필수 CLI·인증·SDK가 없으면 차단 사유를 보고한다.

| 상황 | 적용할 규칙 |
| --- | --- |
| OpenCode 모델 선택 | `--model` → 프로젝트 설정 → `writer_default` → `default` 순서다. |
| Grok 모델 선택 | `--model` → Grok 프로젝트 설정 → Grok 전역 설정 → Grok CLI 기본 모델 순서다. |
| 실행 강도 선택 | OpenCode에서만 `--variant`가 우선이고, 없으면 선택 모델에 저장된 값을 쓴다. |
| 실행 모드 | 전역 `off`는 항상 차단한다. `manual`은 `--explicit`이 필요하다. |
| 읽기 전용 조사 | OpenCode에서 `--read-only`를 붙인다. read/glob/grep/list와 제출만 허용한다. |
| 외부 스킬 원문 | OpenCode에서 승인된 정확한 경로만 `--skill-dir`로 읽는다. 원본은 수정하지 않는다. |

설정이 없으면 멈춘다. 정상 실행 중 모델 목록을 다시 조회하지 않는다.
최초 설정이나 변경이 필요할 때만 [설정 안내](references/opencode.md)를 읽는다.
읽기 전용 모드에서는 shell·테스트·수정·임의 MCP를 실행하지 않고, `changed=[]`와 파일/줄 근거를 받는다.
외부 스킬 경로에는 실제 `SKILL.md`가 있어야 하며 `*`, `?`를 넣지 않는다. 전역 스킬 목록을 스캔하지 않는다.
일반 Worker의 프로젝트 도구·shell·테스트·빌드는 유지하되 권한 프로필을 OS 샌드박스로 믿지 않는다.
동일 저장소의 잠금을 삭제하거나 다른 config로 중복 실행하지 않는다.

## 3. 종료와 실패 처리하기

유효한 OpenCode 이벤트가 **300초간 없으면** 중단한다(`--inactivity-timeout`).
진행 메시지·heartbeat·무관한 JSON은 활동이 아니다. 총 실행 상한은 기본으로 두지 않으며,
필요한 경우에만 `--hard-timeout`을 명시한다. 중단 뒤 남은 프로세스가 없는지 확인하기 전 재실행하지 않는다.

OpenCode Worker는 최종 검증 뒤 `codex_worker_submit_result`로 제출한다. 제출 후 개발 도구를 호출하지 않는다.
제출 형식 오류만 같은 세션에서 고친다. 마지막 자연어 답변을 완료 신호로 파싱하지 않는다.
Grok Worker는 정상 `end` 이벤트, 유효한 JSON 결과, Git 변경 경로 일치를 함께 요구한다.
Grok의 자연어 답변이나 프로세스 종료 코드만으로 완료를 판정하지 않는다.
**최상위 `status`를 우선한다.** `needs_escalation`이면 안쪽 `result.status`가 `completed`여도 완료로 보고하지 않는다.
오류·거부·비정상 종료·검증 실패를 숨기지 않고 부분 변경을 보존한다. 자동 재실행하지 않는다.
사용자가 후속 수정을 명시하면 새로운 작업으로 처리한다.

## 4. 결과를 필요한 만큼 확인하기

요구사항을 `result.changed`, `validation`, `risk`와 대조한다. 전체 소스·diff·테스트를 습관적으로 반복하지 않는다.
구체적 불일치, 검증 실패, 요구사항 누락, 고위험 변경이 있을 때만 해당 부분을 추가 확인한다.

OpenCode의 `validation_evidence.status=observed_pass`는 **선언한 명령의 exit 0을 관측했다**는 뜻이다.
테스트 개수·품질·요구사항 충족·원격 완료까지 증명하지 않는다. `partial`·`unverified`를 실패로 단정하지 말고,
증거 형식만 맞추려고 정상 검증을 다시 시키지 않는다. 실행하지 않은 검증은 미확인으로 남긴다.

## 5. 사용자에게 보고하기

**변경한 것 / 실제 검증한 것 / 남은 위험**을 짧게 보고한다. 진행 메시지나 도구 개수로 성공을 판정하지 않는다.
`model`·`variant`는 CLI 설정값이다. `observed_model`·`observed_variant`가 null이면 실제 모델을 독립 확인했다고 말하지 않는다.
`session_id`는 과금 횟수가 아니다. 시간은 관측값만, 토큰은 provider 보고값과 누락 범위를 함께 제시한다.
Grok의 `worker_cost_usd`는 CLI 보고값이다. Grok 경로도 선언 명령의 관측 exit만 검증 근거로 삼는다.
측정하지 않은 비용·토큰 절감률을 추측하지 않는다.

스킬 자체를 개선·평가할 때만 [평가 기준](references/skill-evaluation.md)을 읽는다.
Grok Build 경로의 단발 실사용 비교와 한계는 [벤치마크 기록](references/grok-benchmark-2026-09-26.md)에 있다.
