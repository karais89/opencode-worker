# OpenCode / Grok Build Worker

Codex가 코드 작업을 한 Worker 세션에 맡기고 구조화된 결과를 받는 두 스킬이다. `$opencode-worker`는 기존 OpenCode 경로를 사용한다. `$grok-worker`는 Grok Build CLI를 직접 사용한다. 두 스킬은 **버전별 공유 실행 코드와 references**를 사용하며, 스킬 진입점은 각각 갱신할 수 있다.

## 설치 구조

Python 3.10+, Git이 필요하다. 사용하려는 엔진의 CLI와 인증도 미리 준비한다. 설치 시 유료 모델은 호출하지 않는다.

```sh
python3 install.py
```

Windows PowerShell에서는 `python .\install.py` 또는 `py -3 .\install.py`를 사용한다. 별도 Codex 홈에는 `--home "/path/to/codex-home"`을 지정한다. `--skill opencode-worker` 또는 `--skill grok-worker`로 한 스킬만 설치·갱신한다. 관리 중인 기존 스킬을 갱신할 때는 `--replace`를 붙인다.

`.agents\skills`를 사용하는 환경에서는 `--home 'C:\Users\Lonpeach\.agents'`를 지정한다. 기존 수동 설치 스킬은 자동으로 덮어쓰지 않는다. 먼저 `python .\install.py --home 'C:\Users\Lonpeach\.agents' --skill opencode-worker --migrate-unmanaged --dry-run`으로 계획을 확인한 뒤, 준비되면 `--dry-run`을 빼고 실행한다. 기존 폴더는 `.agents\worker-backups\`에 영구 보관된다. 기본 `~/.codex` 설치는 `.agents` 스킬을 변경하지 않는다.

```text
~/.codex/
├── skills/
│   ├── opencode-worker/    SKILL.md, SKILL_VERSION, BUNDLE_VERSION, 얇은 CLI 진입점
│   └── grok-worker/        SKILL.md, SKILL_VERSION, BUNDLE_VERSION, 얇은 CLI 진입점
└── worker-bundles/
    └── 2.2.0/   scripts, assets, references, VERSION, BUNDLE_HASH
```

설치된 두 `scripts/lite.py`는 같은 번들의 실제 실행기를 호출한다. 설치 소스의 `scripts/lite.py`도 기존과 같이 직접 실행된다. 설정 파일 경로와 `--project`/`--config`/`run` 옵션은 유지되며, 기본 엔진은 OpenCode다. Grok 설정은 기존 설정 JSON의 `grok` 필드에 저장된다. `mode`와 저장소 잠금은 두 엔진이 공유한다. 설치할 때 이전 설정을 이동하거나 삭제하지 않는다.

`skill-versions.json`은 두 스킬의 독립 버전을 기록하고 `VERSION`은 공유 번들 버전을 기록한다. 한 스킬만 새 번들에 연결해도 다른 스킬과 이전 번들은 유지된다. 같은 번들 버전의 내용이 달라지면 설치를 거부하므로 공유 코드 변경 시 `VERSION`을 올린다. 같은 스킬·번들 버전 재설치와 참조 중인 번들 누락도 거부한다. 교체가 실패하면 이전 스킬을 복원한다. 비관리 스킬을 이전할 때는 원본을 영구 백업하며, 실패 시 백업에서 원래 위치로 복원한다. 이전 번들은 다른 스킬의 참조를 확인한 후 정리한다.

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

실제 CLI 비교 결과는 [Grok 벤치마크](references/grok-benchmark-2026-09-26.md)와 [OpenCode 벤치마크](references/opencode-benchmark-2026-09-27.md)에 적었다.
