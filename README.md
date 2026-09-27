# OpenCode / Grok Build Worker

Codex가 코드 작업을 한 Worker 세션에 맡기고 구조화된 결과를 받는 두 스킬이다. `$opencode-worker`는 기존 OpenCode 경로를 사용한다. `$grok-worker`는 Grok Build CLI를 직접 사용한다. 두 스킬은 **버전별 공유 실행 코드와 references**를 사용하며, 스킬 진입점은 각각 갱신할 수 있다.

## 설치 구조

Python 3.10+, Git이 필요하다. 사용하려는 엔진의 CLI와 인증도 미리 준비한다. 설치 시 유료 모델은 호출하지 않는다.

```sh
python3 install.py
```

Windows PowerShell에서는 `python .\install.py` 또는 `py -3 .\install.py`를 사용한다. 별도 Codex 홈에는 `--home "/path/to/codex-home"`을 지정한다. `--skill opencode-worker` 또는 `--skill grok-worker`로 한 스킬만 설치·갱신한다. 관리 중인 기존 스킬을 갱신할 때는 반드시 `--replace`를 붙인다. `--migrate-unmanaged`는 비관리 스킬의 이전만 허용하며 `--replace`를 대신하지 않는다. 두 종류를 함께 갱신하려면 두 옵션을 모두 명시한다.

`.agents\skills`를 사용하는 환경에서는 `--home 'C:\Users\Lonpeach\.agents'`를 지정한다. 기존 수동 설치 스킬은 자동으로 덮어쓰지 않는다. 먼저 `python .\install.py --home 'C:\Users\Lonpeach\.agents' --skill opencode-worker --migrate-unmanaged --dry-run`으로 계획을 확인한 뒤, 준비되면 `--dry-run`을 빼고 실행한다. 기존 폴더는 `.agents\worker-backups\`에 영구 보관된다. 기본 `~/.codex` 설치는 `.agents` 스킬을 변경하지 않는다.

```text
~/.codex/
├── skills/
│   ├── opencode-worker/    SKILL.md, SKILL_VERSION, BUNDLE_VERSION, 얇은 CLI 진입점
│   └── grok-worker/        SKILL.md, SKILL_VERSION, BUNDLE_VERSION, 얇은 CLI 진입점
└── worker-bundles/
    └── 2.2.2/   scripts, assets, references, VERSION, BUNDLE_HASH
```

설치된 두 `scripts/lite.py`는 공유 번들의 실제 실행기를 호출하되 **각 스킬의 엔진에 고정**된다. `grok-worker`에서는 `--engine`을 생략해도 Grok을 사용하고 `--engine opencode`는 실행 전에 거부한다. `opencode-worker`도 반대 엔진을 거부한다. 같은 엔진을 명시하는 기존 명령은 계속 지원한다. 엔진 고정은 환경변수로 바뀌지 않는다. 소스 checkout의 `scripts/lite.py`만 기존처럼 기본 OpenCode와 `--engine grok`을 모두 지원한다.

설정 파일 경로와 `--project`/`--config`/`run` 옵션은 유지된다. Grok 설정은 기존 설정 JSON의 `grok` 필드에 저장된다. `mode`와 저장소 잠금은 두 엔진이 공유한다. 설치할 때 이전 설정을 이동하거나 삭제하지 않는다.

`skill-versions.json`은 두 스킬의 독립 버전을 기록하고 `VERSION`은 공유 번들 버전을 기록한다. 한 스킬만 새 번들에 연결해도 다른 스킬과 이전 번들은 유지된다. 같은 번들 버전의 내용이 달라지면 설치를 거부하므로 공유 코드 변경 시 `VERSION`을 올린다. 같은 스킬·번들 버전 재설치와 참조 중인 번들 누락도 거부한다. 동일한 Codex 홈에 대한 동시 설치는 전용 잠금으로 차단하고 나중에 시작한 설치는 변경 없이 종료한다. 교체 실패나 Ctrl+C 중단 시 이전 스킬 복원을 시도한다. 롤백은 이번 트랜잭션이 실제로 승격한 경로만 제거하며 다른 프로세스가 생성한 경로는 보존한다. 원본은 임시 작업 폴더 밖의 `worker-backups/`에 보관하므로 복원 자체가 실패해도 자동 정리로 사라지지 않는다. 복원 실패 시 나머지 스킬도 복원을 시도하고, 복구용 백업 경로를 오류에 표시하며 새 번들도 보존한다. 관리 스킬의 백업은 전체 교체가 성공한 뒤 정리하고, 비관리 스킬의 백업은 성공 후에도 영구 보관한다. 강제 종료·전원 차단 시 자동 복원은 보장하지 않는다. **이전 번들은 자동으로 삭제하지 않는다.** 사용자가 모든 설치 스킬의 `BUNDLE_VERSION` 참조와 실행 중인 세션이 없는지 확인한 후 수동으로 정리한다.

## 시작

OpenCode 모델 설정:

```sh
python3 ~/.codex/skills/opencode-worker/scripts/lite.py models
python3 ~/.codex/skills/opencode-worker/scripts/lite.py set-default <provider/model>
python3 ~/.codex/skills/opencode-worker/scripts/lite.py --project "/absolute/repo" run --brief "/absolute/brief.txt" --explicit
```

Grok 모델은 선택 사항이다. 지정하지 않으면 Grok CLI 기본 모델을 따른다. 설치된 Grok 진입점은 `--engine grok` 없이도 같은 엔진을 사용한다.

```sh
python3 ~/.codex/skills/grok-worker/scripts/lite.py models
python3 ~/.codex/skills/grok-worker/scripts/lite.py --project "/absolute/repo" run --brief "/absolute/brief.txt" --explicit
```

Windows에서는 `python3` 대신 `python` 또는 `py -3`를 쓰고 실제 절대 경로를 지정한다. Grok 설정·실행 조건은 [Grok 안내](references/grok.md), OpenCode 설정은 [설정 안내](references/lite-v2.md), Windows CLI 위치는 [Windows 안내](references/windows.md)를 본다.

검증 증거에서 `cmd`·`powershell`·`pwsh`와 알려진 shell/script wrapper는 exit 0만으로 내부 검증을 확인할 수 없어 `unverified`로 남긴다. 대소문자·절대 경로·공백 경로를 포함한 명령에도 같은 기준을 적용한다. 이 분류는 실제 명령 실행을 차단하는 보안 샌드박스가 아니다.

Git 변경 증거는 파일 내용뿐 아니라 Git이 추적하는 실행 권한과 파일 종류도 구분한다. `core.filemode=false`일 때는 실행 권한 변화만으로 변경을 보고하지 않는다. 서브모듈은 체크아웃된 커밋과 내부의 추적·비추적 파일 변경을 재귀적으로 비교하며, 상위 저장소에서는 해당 서브모듈 경로로 집계한다. 서브모듈 변경을 숨기는 Git 설정은 증거 수집에 적용하지 않는다.

## 검증

```sh
python3 -m unittest discover -s scripts -p 'test_*.py' -v
python3 -m unittest discover -s . -p 'test_install.py' -v
node --check assets/submit-result.mjs
```

테스트는 가짜 CLI를 사용하고 유료 provider를 호출하지 않는다. CI는 Windows, macOS, Linux에서 Python 3.10/3.13을 검사한다. 실제 CLI·SDK·인증과 모델 응답의 품질은 별도 실사용 검증이 필요하다. 두 스킬의 프롬프트와 설치 진입점은 각각의 엔진을 명확히 선택하며 실패 시 자동으로 다른 엔진으로 바꾸지 않는다.

실제 CLI 비교 결과는 [Grok 벤치마크](references/grok-benchmark-2026-09-26.md)와 [OpenCode 벤치마크](references/opencode-benchmark-2026-09-27.md)에 적었다.
