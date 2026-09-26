# OpenCode / Grok Build Worker

Codex가 코드 작업을 한 Worker 세션에 맡기고 구조화된 결과를 받는 두 스킬이다. `$opencode-worker`는 기존 OpenCode 경로를 사용한다. `$grok-worker`는 Grok Build CLI를 직접 사용한다. 두 스킬은 **한 버전의 실행 코드와 한 references 영역**을 공유한다.

## 설치 구조

Python 3.10+, Git이 필요하다. 사용하려는 엔진의 CLI와 인증도 미리 준비한다. 설치 시 유료 모델은 호출하지 않는다.

```sh
python3 install.py
```

Windows PowerShell에서는 `python .\install.py` 또는 `py -3 .\install.py`를 사용한다. 별도 Codex 홈에는 `--home "/path/to/codex-home"`을 지정한다. 기존 동일 이름의 설치를 교체할 때만 `--replace`를 붙인다.

현재 `C:\Users\Lonpeach\.agents\skills\opencode-worker`처럼 `.agents\skills`를 사용하는 환경에서는 `python .\install.py --home 'C:\Users\Lonpeach\.agents'`로 같은 위치에 두 스킬을 설치할 수 있다. 단, 기존 폴더는 관리 대상 표시가 없으므로 자동 교체하지 않는다. 기존 폴더를 백업하거나 이동한 뒤 명령을 실행한다. 새 번들은 `.agents\worker-bundles\2.1.0`에 놓인다. 기본 `~/.codex` 설치는 현재 `.agents` 스킬을 업그레이드하지 않는다.

```text
~/.codex/
├── skills/
│   ├── opencode-worker/    SKILL.md, agents, 얇은 CLI 진입점
│   └── grok-worker/        SKILL.md, agents, 얇은 CLI 진입점
└── worker-bundles/
    └── 2.1.0/   scripts, assets, references, VERSION
```

설치된 두 `scripts/lite.py`는 같은 번들의 실제 실행기를 호출한다. 설치 소스의 `scripts/lite.py`도 기존과 같이 직접 실행된다. 설정 파일 경로와 `--project`/`--config`/`run` 옵션은 유지되며, 기본 엔진은 OpenCode다. Grok 설정은 기존 설정 JSON의 `grok` 필드에 저장된다. `mode`와 저장소 잠금은 두 엔진이 공유한다. 설치할 때 이전 설정을 이동하거나 삭제하지 않는다.

기존에 `skills/opencode-worker`를 수동 설치했다면 설치기가 이를 자동으로 덮어쓰지 않는다. 기존 폴더를 보관한 뒤 두 스킬을 함께 설치한다. Lite v2 기반의 첫 확장 번들이므로 버전은 `2.1.0`으로 시작한다. 저장소에는 이전 배포 태그가 없어 이 번호는 번들 호환성 표지이며 외부 릴리스 이력을 뜻하지 않는다. 이후 버전도 두 스킬의 `BUNDLE_VERSION`을 같은 번들로 맞춰 배포한다. 새 버전으로 교체할 때는 `--replace`를 사용한다. 같은 버전 재설치, 두 스킬의 버전 불일치, 공유 번들 누락은 거부한다. 교체 도중 파일 이동이 실패하면 이전 폴더 복원을 시도한다. 이전 번들은 다른 스킬이 참조하는지 확인한 후 정리한다. 두 스킬이 한 번들 버전에 의존하는 설치 결합은 있지만, 실행 코드와 references의 수정 지점은 각각 한 곳이다.

## 시작

OpenCode 모델 설정:

```sh
python3 ~/.codex/skills/opencode-worker/scripts/lite.py models
python3 ~/.codex/skills/opencode-worker/scripts/lite.py set-default <provider/model>
python3 ~/.codex/skills/opencode-worker/scripts/lite.py --project "/absolute/repo" run --brief "/absolute/brief.txt" --explicit
```

Grok 모델은 선택 사항이다. 지정하지 않으면 Grok CLI 기본 모델을 따른다.

```sh
python3 ~/.codex/skills/grok-worker/scripts/lite.py models --engine grok
python3 ~/.codex/skills/grok-worker/scripts/lite.py --project "/absolute/repo" run --engine grok --brief "/absolute/brief.txt" --explicit
```

Windows에서는 `python3` 대신 `python` 또는 `py -3`를 쓰고 실제 절대 경로를 지정한다. Grok 설정·실행 조건은 [Grok 안내](references/grok.md), OpenCode 설정은 [설정 안내](references/lite-v2.md), Windows CLI 위치는 [Windows 안내](references/windows.md)를 본다.

## 검증

```sh
python3 -m unittest discover -s scripts -p 'test_*.py' -v
python3 -m unittest discover -s . -p 'test_install.py' -v
node --check assets/submit-result.mjs
```

테스트는 가짜 CLI를 사용하고 유료 provider를 호출하지 않는다. CI는 Windows, macOS, Linux에서 Python 3.10/3.13을 검사한다. 실제 CLI·SDK·인증과 모델 응답의 품질은 별도 실사용 검증이 필요하다. 두 스킬의 프롬프트는 각각의 엔진을 명확히 선택하며 실패 시 자동으로 다른 엔진으로 바꾸지 않는다.

실제 Grok CLI 비교의 진행 상태와 한도 때문에 남은 검증은 [벤치마크 기록](references/grok-benchmark-2026-09-26.md)에 적었다.
