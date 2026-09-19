# 설정과 실행 참조

일반 개발 작업에는 이 문서를 읽을 필요가 없습니다. `python3 <skill>/scripts/worker.py --help`와 `run --help`가 설치된 버전의 실제 CLI입니다.

## 환경

OpenCode CLI 및 provider 인증을 먼저 준비하세요. Skill은 인증 파일을 복사하거나 새 backend를 설치하지 않습니다. [OpenCode 공식 문서](https://opencode.ai/docs/)를 참고하세요.

제출 플러그인은 기존 `@opencode-ai/plugin/dist/tool.js`를 다음 디렉터리의 `node_modules`에서 찾습니다: `OPENCODE_CONFIG_DIR`, `${XDG_CONFIG_HOME:-~/.config}/opencode`, `${XDG_CACHE_HOME:-~/.cache}/opencode`. SDK가 없으면 실행을 중단하며 몰래 설치하지 않습니다. 기존 OpenCode config 디렉터리의 package.json에 CLI와 호환되는 `@opencode-ai/plugin` 의존성을 준비하고 해당 디렉터리에서 패키지 관리자로 설치하세요. 예: `npm install --prefix "$HOME/.config/opencode" @opencode-ai/plugin@<matching-version>`. 기존 package.json을 덮어쓰지 마세요. 사용자 설치 단계이며 Worker 작업 때마다 필요하지 않습니다.

프로세스 전용 plugin 설정으로 assets/submit-result.mjs를 로드합니다. 저장소나 전역 OpenCode 설정 파일에 플러그인을 추가하지 않습니다.

## 라우팅

`--config /absolute/config.json`으로 별도 설정을 지정할 수 있습니다. 우선순위는 `run --model` → 프로젝트 route → writer_default/default입니다. `--use-opencode-default`는 저장 route가 없을 때 OpenCode 기본 모델을 명시적으로 사용합니다. `models`로 정확한 모델 ID, `resolve`로 선택 결과를 확인하세요.

- `set-writer-default <provider/model> --variant <variant>`: 기본 모델.
- `--project /repo set-project <provider/model> --variant <variant>`: 프로젝트 모델.
- `set-alias <name> <provider/model>`: 별칭.
- `set-fallbacks <route...>` 후 `run --use-fallbacks`: 명시적 fallback. 안전한 provider 실패에 한하며 이미 발생한 작업을 재실행하지 않습니다.
- `set-mode auto|manual|off`: 자동 허용/명시적 요청만/중단. manual은 `run --explicit` 필요, off는 explicit으로 우회 불가.

## 실행

```sh
python3 <skill>/scripts/worker.py --project /absolute/repo run   --brief /absolute/task.txt --evidence-dir /outside/repo/evidence
```

brief는 목표·제약·완료 조건만 짧게(최대 8 KiB) 작성합니다. Worker가 프로젝트 지침과 원본 공유 Skill/CLI/MCP를 탐색합니다. 발견이 도구 설치나 연결 성공을 보장하지는 않습니다. 필요한 도구가 없으면 실제 blocker를 보고합니다. shell을 제한하는 read-only 모드는 일반 Writer에 적용하지 않습니다.

실제 입력으로 재현한 결함 또는 명백한 요구사항 위반에만 같은 옵션과 `--fix-from <returned-evidence-dir>/writer.json --brief <short-correction.txt>`로 수정 1회를 요청합니다. 새로운 일반 run으로 제한을 우회하지 마세요. 두 번째 수정은 차단됩니다. Reviewer와 자동 수정 체인은 없습니다.

stdout은 작은 구조화된 결과입니다. 실제 session/model/elapsed/usage 및 실행 증거는 결과와 외부 evidence 디렉터리에 남습니다. Head가 일상적으로 private evidence나 raw JSONL을 읽을 필요는 없습니다. `--log-dir`은 필요할 때만 사용하는 비공개 원본 로그 옵션입니다. 로그·작업 데이터·인증 정보는 배포 저장소에 커밋하지 마세요.
