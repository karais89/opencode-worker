---
name: grok-worker
description: Codex/ChatGPT가 구현·다중 파일 수정·버그 수정·테스트를 Grok Build CLI 한 세션에 위임한다. 사용자가 $grok-worker를 지정하거나 Grok Build에 작업을 맡기라고 명시한 경우 사용한다. 일반 OpenCode 위임에는 사용하지 않는다.
---

# Grok Build Worker

Head가 목표와 완료 조건을 정한 뒤 Grok Build CLI 한 세션에 맡긴다. 지시·완료·보고 기준은 [Worker 공통 계약](../references/worker-contract.md)을 따르고, 실행·결과·권한의 세부 조건은 [Grok 안내](../references/grok.md)를 읽는다.

## 1. 지시 파일 만들기

확인된 Git 저장소의 절대 경로를 사용한다. 지시 파일은 [Worker 공통 계약](../references/worker-contract.md)의 형식으로 만든다.

## 2. 한 세션 실행하기

Grok Build CLI, 기존 인증, Python 3.10+, Git이 필요하다. Grok 모델이 설정되지 않았으면 CLI 기본 모델을 따른다. 명시적인 위임 요청에는 `--explicit`을 붙인다.

```sh
python3 "<this-skill>/scripts/lite.py" --project "/absolute/repo" run --engine grok --brief "/absolute/brief.txt" --explicit
```

Windows에서는 `python` 또는 `py -3`를 사용한다. `--variant`, `--read-only`, `--skill-dir`는 Grok 경로에서 지원하지 않는다. 전역 `off`와 저장소 잠금은 OpenCode와 공유한다.

## 3. 결과 확인하고 보고하기

정상 `end`, 유효한 JSON 결과, Git 변경 경로 일치가 모두 필요하다. 공통 계약에 따라 최상위 `status`를 우선하고 검증 근거의 한계를 명시한다.
