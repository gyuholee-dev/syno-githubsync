# syno-githubsync

내 GitHub 저장소(소스코드)를 Synology NAS에 자동으로 백업하는 스크립트입니다.
정해진 주기마다 실행되어 저장소를 NAS에 복사하고, 이후에는 바뀐 부분만 갱신합니다.

- 대상: 내가 소유한 GitHub 저장소 전체 (private 포함)
- 환경: Synology DSM 7.4 (Python 3.8)

## 설계

### 파일 구조

```
/syno-githubsync/
├── github_backup.py          ← 스크립트
├── githubsync.env            ← 환경변수
└── backup.log                ← 실행 로그 (자동 생성)
```

### 환경변수

| 변수 | 필수 | 설명 |
|---|---|---|
| `GITHUB_TOKEN` | 필수 | GitHub Fine-grained personal access token |
| `BACKUP_DEST` | 선택 | Repository backup path |

### 동작

1. DSM 작업 스케줄러가 정해진 주기에 사용자 정의 스크립트를 실행하고, 이 스크립트가 `python3 github_backup.py` 를 실행해 출력을 `backup.log` 에 기록합니다.
2. `githubsync.env` 에서 토큰과 백업 경로를 읽습니다. 토큰이 없으면 바로 종료합니다. 백업 경로를 지정하지 않았다면 GitHub API로 소유자명을 조회해 `<스크립트 실행 경로>/<소유자명>` 을 백업 경로로 씁니다.
3. 백업 경로를 준비하고 중복 실행을 막는 잠금(`.backup.lock`)을 만듭니다. 이미 실행 중이면 종료합니다.
4. GitHub API로 내가 소유한 저장소 목록을 가져옵니다. API 오류가 나거나 목록이 비어 있으면 실패로 종료합니다.
5. 저장소마다 백업 경로의 같은 이름 폴더를 확인합니다.
   - 폴더가 없으면 `git clone --mirror` 로 받습니다. 임시 폴더에 받은 뒤 옮기므로 도중에 실패해도 깨진 폴더가 남지 않습니다.
   - 폴더가 있으면 `git remote update --prune` 으로 바뀐 부분만 갱신하고, GitHub에서 삭제된 브랜치·태그도 정리합니다.
   - 토큰은 실행하는 순간에만 HTTP 헤더로 전달되며 저장소 설정(`.git/config`)에는 남지 않습니다.
6. 결과를 출력하고 종료합니다. 실패한 저장소가 하나라도 있으면 오류 종료 코드로 끝나 작업 스케줄러가 알림을 보냅니다.

## 설치

### 1. 준비

- 패키지 센터에서 **Git Server** 를 설치합니다.
- DSM에서 **SSH 서비스를 활성화**합니다. (제어판 → 터미널 및 SNMP → 터미널 → SSH 서비스 활성화)
- GitHub에서 토큰을 발급합니다.
  Settings → Developer settings → Personal access tokens → Fine-grained tokens
  - Repository access: **All repositories**
  - Permissions: **Add permissions** → Repository permissions 의 **Contents** 추가 → Access 드롭다운에서 **Read-only** 선택
- 백업을 실행할 DSM 계정을 정하고, 그 계정에 스크립트 실행 경로와 백업 경로가 있는 공유 폴더의 **읽기/쓰기 권한**을 줍니다.
  (제어판 → 공유 폴더 → 편집 → 권한)

### 2. 파일 복사

스크립트를 실행할 경로를 정한뒤 NAS의 해당 경로에 스크립트를 복사합니다.

```bash
# 스크립트 실행 경로를 githubsync 로 지정할 경우
/<볼륨>/<공유폴더>/githubsync/
├── github_backup.py
└── githubsync.env.example
```

### 3. 환경변수 지정

NAS에 **관리자 계정**으로 SSH 접속해 입력합니다.

```bash
# 스크립트 실행 경로를 githubsync 로 지정할 경우
cd /<볼륨>/<공유폴더>/githubsync
cp githubsync.env.example githubsync.env
```

`vi githubsync.env` 등으로 파일을 열어 `GITHUB_TOKEN=` 에 발급된 토큰을 입력합니다. 백업 경로를 지정하려면 `# BACKUP_DEST=` 의 `#` 을 지우고 경로를 입력합니다. BACKUP_DEST 값이 없을 경우 스크립트 실행 경로 아래의 소유자명 폴더로 백업합니다.

```bash
# 토큰 필수 입력
GITHUB_TOKEN=github_pat_xxxxxxxxxxxxxxxx
# 백업폴더를 my-github 으로 지정할 경우
BACKUP_DEST=/<볼륨>/<공유폴더>/my-github
```

### 4. 권한 지정

이 단계는 생략해도 동작하지만, 환경변수에 토큰이 들어 있으므로 다른 사용자가 읽지 못하도록 지정하는 것을 권장합니다. 생략하더라도 위에서 정한 계정이 두 파일을 읽을 수 있어야 합니다.

**관리자 계정**으로 NAS에 SSH로 접속해 입력합니다. `<실행계정>` 은 백업을 실행할 DSM 계정 이름입니다. 

```bash
# 스크립트 실행 경로를 githubsync 로 지정할 경우
cd /<볼륨>/<공유폴더>/githubsync
sudo chown <실행계정> github_backup.py githubsync.env
sudo chmod 700 github_backup.py
sudo chmod 600 githubsync.env
```

### 5. 드라이런으로 확인하기

NAS에 **관리자 계정**으로 SSH 접속한 뒤, `<실행계정>` 권한으로 `--dry_run` 을 붙여 실행합니다. 드라이런은 실제 저장소를 받거나 갱신하지 않고, 저장소 목록 조회와 각 저장소 접근 가능 여부 및 백업폴더 쓰기 가능 여부만 확인합니다.

```bash
sudo -u <실행계정> python3 /<볼륨>/<공유폴더>/githubsync/github_backup.py --dry_run
```

마지막에 `[dry-run] 완료: 신규 클론 N, 갱신 M, 실패 0 (저장소 변경 없음)` 이 나오면 정상입니다.
문제가 있으면 출력된 메시지에서 원인을 확인합니다. 실제 백업은 6단계에서 등록한 작업이 실행될 때 이루어집니다.

### 6. 자동 실행 등록

DSM 제어판 → 작업 스케줄러 → 생성 → 예약된 작업 → 사용자 정의 스크립트

| 항목 | 값 |
|---|---|
| 사용자 | 위에서 정한 `<실행계정>` |
| 스케줄 | 원하는 주기 (하루 1회 등) |
| 실행 명령 | 아래 스크립트 |

```bash
# <볼륨>, <공유폴더> 를 실제 볼륨·공유 폴더 이름으로 변경
cd /<볼륨>/<공유폴더>/githubsync || exit 4
LOG=backup.log

echo "===== $(date '+%Y-%m-%d %H:%M:%S') =====" >> "$LOG"
python3 -B github_backup.py >> "$LOG" 2>&1
status=$?
echo "exit code: $status" >> "$LOG"

# 로그가 무한히 커지지 않도록 마지막 2000줄만 남긴다
tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"

exit $status
```

작업 설정의 **알림** 에서 "비정상 종료 시에만 실행 세부 정보 전송" 을 켜 두면, 백업이 실패했을 때 DSM 알림(메일)을 받을 수 있습니다.

등록한 작업을 선택하고 **실행** 을 누르면 스케줄을 기다리지 않고 바로 첫 백업을 실행할 수 있습니다.

### 7. SSH 비활성화

모든 설정이 끝나면 보안을 위해 SSH 서비스를 다시 비활성화합니다. (제어판 → 터미널 및 SNMP → 터미널 → SSH 서비스 활성화 체크 해제)

## 복원

### 1. 로컬 환경으로 가져오기

NAS의 공유 폴더를 PC에 연결(SMB 등)한 뒤, PC의 터미널에서 백업된 저장소를 미러로 클론합니다. 모든 브랜치와 태그가 그대로 들어옵니다.

```bash
# <마운트 경로> 를 PC에 마운트된 공유폴더 경로로 변경
# <백업폴더> 를 백업 경로로 변경
# 저장소명이 my-repo 일 경우
git clone --mirror <마운트 경로>/<백업폴더>/my-repo my-repo.git
```

작업 폴더가 필요하면 받은 미러에서 다시 클론합니다.

```bash
# 저장소명이 my-repo 일 경우
git clone my-repo.git my-repo
```

### 2. GitHub로 다시 push

1. GitHub에서 **빈 저장소**를 만듭니다. README, .gitignore 등은 추가하지 않습니다.
2. 위에서 받은 미러 폴더에서 브랜치와 태그를 push 합니다.

```bash
# 저장소명이 my-repo 일 경우
cd my-repo.git
git push https://github.com/<GitHub계정>/my-repo.git 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'
```

- `git push --mirror` 는 사용하지 않습니다. 미러에는 GitHub의 PR 참조(`refs/pull/*`)가 들어 있는데 GitHub가 이를 push 하지 못하게 막아 오류로 끝납니다.
- push 에는 쓰기 권한이 필요합니다. 백업용 읽기 전용 토큰이 아니라 PC에 설정된 본인의 GitHub 로그인(또는 Contents: Read and write 권한의 토큰)을 사용합니다.

## 알아둘 점

- **저장소의 소스코드만 백업합니다.** 이슈, 위키, PR 토론, Git LFS 파일은 포함되지 않습니다.
- **저장소의 삭제는 백업에 반영되지 않습니다.** GitHub에서 저장소 자체를 삭제해도 NAS의 백업 폴더는 지워지지 않고 그대로 보존됩니다. 이후 갱신은 되지 않습니다.
- **GitHub에서 저장소 이름을 바꾸면 백업 폴더 이름도 따라 바뀝니다.** 백업 경로의 `.repo_ids.json` 에 저장소 ID와 폴더 이름을 기록해 두고 이름 변경을 알아챕니다. 이 파일은 지우지 마세요. 바뀐 이름의 폴더가 이미 있으면 경고만 남기고 옮기지 않습니다. 이름 변경을 추적하기 전에 바꾼 저장소는 새 폴더로 새로 받아지고, 이전 폴더는 그대로 남습니다.
- **브랜치·태그의 삭제와 force-push 는 백업에 반영됩니다.** GitHub에서 브랜치나 태그를 지우거나 force-push 하면 백업에도 그대로 반영됩니다. (저장소 자체를 삭제한 경우는 아래 항목처럼 백업이 남습니다.) 과거 상태까지 보존하려면 백업 폴더에 Btrfs 스냅샷이나 Hyper Backup 버전 보관을 설정하세요.
- **백업 경로에 저장소와 이름이 같은 다른 폴더가 있으면 그 저장소는 백업에 실패합니다.** 저장소는 백업 경로 안의 저장소 이름과 같은 폴더에 저장됩니다. 예를 들어 백업 경로에 `githubsync` 라는 스크립트 폴더가 있는데 GitHub에도 `githubsync` 저장소가 있으면, 그 저장소는 백업되지 않고 오류로 기록됩니다.
- **토큰 만료가 임박하면 경고합니다.** 토큰 만료일이 14일 안으로 다가오면 로그에 경고를 남기고, 백업이 성공했더라도 종료 코드 5로 끝나 작업 스케줄러가 알림을 보냅니다. 새 토큰을 발급해 `githubsync.env` 의 `GITHUB_TOKEN` 을 교체하세요. 교체하지 않고 만료되면 백업이 인증 오류로 실패합니다.
- **일시적인 오류는 자동으로 재시도합니다.** 네트워크 오류나 GitHub 서버 오류(5xx), 요청 제한(429) 때는 API 호출과 git 명령을 최대 2회 다시 시도합니다. 인증·권한 오류와 저장소를 찾을 수 없는 오류는 재시도하지 않습니다.
- **오래된 잠금은 자동으로 정리됩니다.** 이전 실행이 도중에 강제 종료되어 `.backup.lock` 이 남아도, 그 실행의 프로세스가 없거나 12시간이 넘게 지났다면 다음 실행이 지우고 이어서 진행합니다. 실행 중인 백업이 있으면 종료 코드 3으로 끝납니다.
