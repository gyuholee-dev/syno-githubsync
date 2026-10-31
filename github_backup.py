#!/usr/bin/env python3
"""개인 GitHub 저장소(소스코드)를 Synology NAS에 bare 미러로 백업한다.

설정은 환경 파일(기본: 이 스크립트와 같은 폴더의 githubsync.env)에서 읽는다.
같은 이름의 소문자 실행 인자(--github_token=값 --backup_dest=값)를 주면 환경 파일보다 우선한다.
--dry_run 을 주면 저장소는 받지 않고, 백업 경로 폴더 생성과 쓰기 권한, 저장소 접근 가능 여부만 확인한다.
백업 경로(BACKUP_DEST)를 지정하지 않으면 스크립트 폴더 아래의 <소유자명> 폴더에 백업한다.
"""
import argparse
import base64
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
API_URL = "https://api.github.com"
API_TIMEOUT = 30
GIT_TIMEOUT = 3600
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
RETRY_DELAYS = (5, 15)  # 일시적 오류 시 재시도 전 대기(초). 항목 수만큼 재시도한다.
GIT_NO_RETRY = ("Authentication failed", "Permission denied",
                "error: 401", "error: 403", "error: 404", "not found")
TOKEN_WARN_DAYS = 14
EXIT_TOKEN_EXPIRING = 5
LOCK_MAX_AGE = 12 * 3600
ID_MAP_NAME = ".repo_ids.json"


class BackupError(Exception):
    pass


def log(message):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def load_config(path):
    config = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            config[key] = value
    return config


def api_get(path, token, what, headers_out=None):
    """GitHub API 를 호출해 JSON 을 돌려준다. 실패하면 BackupError.

    일시적 오류(네트워크, 5xx, 429, Retry-After 가 붙은 403)는 재시도한다.
    headers_out 을 주면 응답 헤더(소문자 키)를 채워 준다.
    """
    request = urllib.request.Request(API_URL + path, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "syno-githubsync",
    })
    for delay in RETRY_DELAYS + (None,):
        retry_after = None
        try:
            with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
                if headers_out is not None:
                    headers_out.update(
                        (k.lower(), v) for k, v in response.headers.items())
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error = BackupError("GitHub API 오류: HTTP %d (%s)" % (e.code, what))
            retry_after = e.headers.get("Retry-After") if e.headers else None
            retryable = e.code >= 500 or e.code == 429 or \
                (e.code == 403 and retry_after is not None)
        except (urllib.error.URLError, OSError, ValueError) as e:
            error = BackupError("GitHub API 호출 실패 (%s): %s" % (what, e))
            retryable = True
        if not retryable or delay is None:
            raise error
        wait = min(int(retry_after), 60) if retry_after and retry_after.isdigit() else delay
        log("%s — %d초 후 재시도" % (error, wait))
        time.sleep(wait)


def token_days_left(headers):
    """응답 헤더에서 토큰 만료까지 남은 일수를 구한다. 만료일이 없으면 None."""
    value = headers.get("github-authentication-token-expiration")
    if not value:
        return None
    try:
        expires = datetime.datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return None
    return (expires - datetime.datetime.utcnow()).days


def check_token_expiry(headers):
    """토큰 만료가 임박했으면 경고를 남기고 True 를 돌려준다."""
    days = token_days_left(headers)
    if days is None or days >= TOKEN_WARN_DAYS:
        return False
    log("경고: 토큰이 %d일 후 만료됩니다 (%s). 새 토큰으로 교체하세요."
        % (max(days, 0), headers["github-authentication-token-expiration"]))
    return True


def get_login(token):
    """토큰 소유자의 GitHub 계정 이름을 돌려준다."""
    user = api_get("/user", token, "사용자 조회")
    login = user.get("login") if isinstance(user, dict) else None
    if not login or not REPO_NAME_RE.match(login):
        raise BackupError("GitHub 계정 이름을 확인할 수 없음")
    return login


def list_repos(token, headers_out=None):
    """내가 소유한 저장소의 (id, owner/name) 목록을 돌려준다. 실패하면 BackupError."""
    repos_found = []
    page = 1
    while True:
        what = "page %d" % page
        repos = api_get("/user/repos?per_page=100&page=%d&affiliation=owner" % page,
                        token, what, headers_out if page == 1 else None)
        if not isinstance(repos, list):
            raise BackupError("GitHub API 응답 형식이 올바르지 않음 (%s)" % what)
        if not repos:
            return repos_found
        try:
            repos_found.extend((repo["id"], repo["full_name"]) for repo in repos)
        except (KeyError, TypeError):
            raise BackupError("GitHub API 응답에 id/full_name 이 없음 (%s)" % what)
        page += 1


def run_git(args, auth_header, cleanup=None):
    """git 을 실행한다. 토큰은 URL/.git/config 에 남기지 않고 헤더로만 전달한다.

    인증·권한·없음 오류가 아니면 재시도하며, 재시도 전에 cleanup 경로를 지운다.
    """
    cmd = ["git", "-c", "http.extraheader=" + auth_header] + args
    for delay in RETRY_DELAYS + (None,):
        try:
            result = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, "시간 초과"
        except OSError as e:
            return False, str(e)
        err = " ".join(result.stderr.split())
        if result.returncode == 0:
            return True, err
        if delay is None or any(marker in err for marker in GIT_NO_RETRY):
            return False, err
        log("git 실패 — %d초 후 재시도: %s" % (delay, err))
        time.sleep(delay)
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)


def make_auth_header(token):
    auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
    return "Authorization: Basic " + auth


def repo_dir_name(full_name):
    """owner/name 에서 저장소 폴더 이름을 돌려준다. 허용되지 않는 이름이면 None."""
    name = full_name.split("/", 1)[-1]
    if not REPO_NAME_RE.match(name) or name in (".", ".."):
        return None
    return name


def lock_is_stale(lock_dir):
    """잠금을 잡은 프로세스가 없거나 너무 오래됐으면 True."""
    try:
        with open(os.path.join(lock_dir, "pid")) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        pid = None
    if pid is not None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            pass  # 권한이 없을 뿐 프로세스는 살아 있다
    try:
        return time.time() - os.path.getmtime(lock_dir) > LOCK_MAX_AGE
    except OSError:
        return True


def acquire_lock(lock_dir):
    """잠금 폴더를 만든다. 다른 실행이 잡고 있으면 FileExistsError."""
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        if not lock_is_stale(lock_dir):
            raise
        log("오래된 잠금을 정리합니다: " + lock_dir)
        shutil.rmtree(lock_dir, ignore_errors=True)
        os.mkdir(lock_dir)
    try:
        with open(os.path.join(lock_dir, "pid"), "w") as f:
            f.write("%d\n" % os.getpid())
    except OSError:
        pass


def load_id_map(dest):
    """저장소 id → 백업 폴더 이름 기록을 읽는다."""
    try:
        with open(os.path.join(dest, ID_MAP_NAME), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def save_id_map(dest, id_map):
    path = os.path.join(dest, ID_MAP_NAME)
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(id_map, f, indent=1, sort_keys=True)
        os.replace(path + ".tmp", path)
    except OSError as e:
        log("저장소 id 기록 저장 실패: %s" % e)


def find_renames(repos, id_map, dest):
    """GitHub에서 이름이 바뀐 저장소를 찾아 {id: 이전 폴더 이름} 으로 돌려준다."""
    renames = {}
    for repo_id, full_name in repos:
        new = repo_dir_name(full_name)
        old = id_map.get(str(repo_id))
        if new is None or old is None or old == new:
            continue
        if not os.path.isdir(os.path.join(dest, old)):
            continue
        if os.path.exists(os.path.join(dest, new)):
            log("경고: 이름이 바뀐 저장소를 옮길 수 없음 (%s → %s, 대상이 이미 있음)" % (old, new))
            continue
        renames[str(repo_id)] = old
    return renames


def apply_rename(dest, old, full_name, auth_header):
    """백업 폴더를 새 저장소 이름으로 옮기고 원격 주소를 갱신한다."""
    new = repo_dir_name(full_name)
    target = os.path.join(dest, new)
    try:
        os.rename(os.path.join(dest, old), target)
    except OSError as e:
        log("경고: 폴더 이름 변경 실패 (%s → %s): %s" % (old, new, e))
        return
    log("저장소 이름 변경 반영: %s → %s" % (old, new))
    ok, err = run_git(["-C", target, "remote", "set-url", "origin",
                       "https://github.com/%s.git" % full_name], auth_header)
    if not ok:
        log("경고: 원격 주소 갱신 실패(%s): %s" % (new, err))


def backup_repo(full_name, dest, work_dir, auth_header):
    name = repo_dir_name(full_name)
    if name is None:
        return False, "허용되지 않는 저장소 이름"
    target = os.path.join(dest, name)

    if os.path.isdir(target):
        ok, err = run_git(["-C", target, "remote", "update", "--prune"], auth_header)
        return ok, ("" if ok else "갱신: " + err)

    # 임시 경로에 받은 뒤 이동하여 중간 실패 시 깨진 미러가 남지 않게 한다.
    tmp = os.path.join(work_dir, name)
    url = "https://github.com/%s.git" % full_name
    ok, err = run_git(["clone", "--mirror", url, tmp], auth_header, cleanup=tmp)
    if ok:
        try:
            shutil.move(tmp, target)
        except OSError as e:
            ok, err = False, str(e)
    if not ok:
        shutil.rmtree(tmp, ignore_errors=True)
        return False, "클론: " + err
    log("신규 클론: " + full_name)
    return True, ""


def dry_run(token, dest):
    """저장소는 받지 않고 백업 경로·쓰기 권한·저장소 접근 가능 여부(git ls-remote)만 확인한다.

    변경은 백업 경로 폴더를 만드는 것뿐이며, 쓰기 확인용 임시 폴더는 바로 지운다.
    """
    auth_header = make_auth_header(token)
    log("[dry-run] 저장소는 받지 않고 백업 경로와 접근 가능 여부만 확인합니다 → " + dest)

    try:
        os.makedirs(dest, exist_ok=True)
        shutil.rmtree(tempfile.mkdtemp(prefix=".work.", dir=dest))
    except OSError as e:
        log("[dry-run] 백업 경로에 쓸 수 없음: %s (%s)" % (dest, e))
        return 2
    log("[dry-run] 백업 경로 쓰기 확인")
    lock_dir = os.path.join(dest, ".backup.lock")
    if os.path.exists(lock_dir):
        if lock_is_stale(lock_dir):
            log("[dry-run] 오래된 잠금이 있음 (실제 실행 시 자동 정리됨): " + lock_dir)
        else:
            log("[dry-run] 이미 실행 중임: " + lock_dir)
            return 3

    headers = {}
    try:
        repos = list_repos(token, headers)
    except BackupError as e:
        log(str(e))
        log("저장소 목록 조회 실패")
        return 1
    if not repos:
        log("저장소가 하나도 조회되지 않음 (토큰 권한 확인 필요)")
        return 1

    check_token_expiry(headers)
    renames = find_renames(repos, load_id_map(dest), dest)

    new_count = update_count = fail_count = 0
    for repo_id, full_name in repos:
        name = repo_dir_name(full_name)
        if name is None:
            log("[dry-run] 실패(%s): 허용되지 않는 저장소 이름" % full_name)
            fail_count += 1
            continue
        ok, err = run_git(
            ["ls-remote", "--heads", "https://github.com/%s.git" % full_name],
            auth_header)
        if not ok:
            log("[dry-run] 실패(%s): 접근 불가: %s" % (full_name, err))
            fail_count += 1
        elif str(repo_id) in renames:
            log("[dry-run] 갱신 예정: %s (이름 변경: %s → %s)"
                % (full_name, renames[str(repo_id)], name))
            update_count += 1
        elif os.path.isdir(os.path.join(dest, name)):
            log("[dry-run] 갱신 예정: " + full_name)
            update_count += 1
        else:
            log("[dry-run] 신규 클론 예정: " + full_name)
            new_count += 1

    log("[dry-run] 완료: 신규 클론 %d, 갱신 %d, 실패 %d (저장소 변경 없음)"
        % (new_count, update_count, fail_count))
    return 0 if fail_count == 0 else 1


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="개인 GitHub 저장소(소스코드)를 NAS에 bare 미러로 백업한다.",
        allow_abbrev=False)
    parser.add_argument("--github_token", default="", help="GitHub 토큰")
    parser.add_argument("--backup_dest", default="", help="백업 경로")
    parser.add_argument("--dry_run", action="store_true",
                        help="변경 없이 백업 계획과 저장소 접근 가능 여부만 확인")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    # 설정 우선순위: 실행 인자 > 환경 파일 > 프로세스 환경변수
    config_path = os.environ.get("GITHUB_BACKUP_CONFIG") or \
        os.path.join(SCRIPT_DIR, "githubsync.env")
    config_readable = os.access(config_path, os.R_OK)
    config = load_config(config_path) if config_readable else {}

    def setting(key):
        return getattr(args, key.lower()) or config.get(key) or os.environ.get(key, "")

    token = setting("GITHUB_TOKEN")
    if not token:
        if not config_readable:
            print("설정 파일을 읽을 수 없음: " + config_path, file=sys.stderr)
        else:
            print("GITHUB_TOKEN 이 설정되지 않음", file=sys.stderr)
        return 2

    # 작업 스케줄러에서는 HOME이 비어 있을 수 있다 (git 설정 경로에 필요).
    if not os.environ.get("HOME"):
        os.environ["HOME"] = SCRIPT_DIR
    os.environ["GIT_TERMINAL_PROMPT"] = "0"

    # 백업 경로: BACKUP_DEST > 스크립트 폴더/<소유자명>
    dest = setting("BACKUP_DEST")
    if not dest:
        try:
            dest = os.path.join(SCRIPT_DIR, get_login(token))
        except BackupError as e:
            log(str(e))
            return 1
    dest = os.path.abspath(dest)

    if args.dry_run:
        return dry_run(token, dest)

    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        print("백업 경로를 만들 수 없음: %s (%s)" % (dest, e), file=sys.stderr)
        return 2

    # 동시 실행 방지 (mkdir 은 원자적이다)
    lock_dir = os.path.join(dest, ".backup.lock")
    try:
        acquire_lock(lock_dir)
    except FileExistsError:
        print("이미 실행 중이거나 이전 실행이 비정상 종료됨: " + lock_dir, file=sys.stderr)
        return 3
    except OSError as e:
        print("잠금 폴더를 만들 수 없음: %s (%s)" % (lock_dir, e), file=sys.stderr)
        return 2

    # 강제 종료(SIGTERM) 시에도 finally 에서 정리되도록 한다.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))

    work_dir = None
    try:
        work_dir = tempfile.mkdtemp(prefix=".work.", dir=dest)
        auth_header = make_auth_header(token)

        log("백업 시작 → " + dest)
        headers = {}
        try:
            repos = list_repos(token, headers)
        except BackupError as e:
            log(str(e))
            log("저장소 목록 조회 실패")
            return 1
        if not repos:
            # 토큰 범위 문제 등으로 빈 목록이 오면 성공으로 오인하지 않는다.
            log("저장소가 하나도 조회되지 않음 (토큰 권한 확인 필요)")
            return 1
        token_expiring = check_token_expiry(headers)

        # 이름이 바뀐 저장소는 다른 저장소를 처리하기 전에 폴더부터 옮긴다.
        id_map = load_id_map(dest)
        renames = find_renames(repos, id_map, dest)
        for repo_id, full_name in repos:
            if str(repo_id) in renames:
                apply_rename(dest, renames[str(repo_id)], full_name, auth_header)

        ok_count = 0
        fail_count = 0
        for repo_id, full_name in repos:
            ok, err = backup_repo(full_name, dest, work_dir, auth_header)
            if ok:
                ok_count += 1
                id_map[str(repo_id)] = repo_dir_name(full_name)
            else:
                log("실패(%s): %s" % (full_name, err))
                fail_count += 1
        save_id_map(dest, id_map)

        log("완료: 성공 %d, 실패 %d" % (ok_count, fail_count))
        if fail_count:
            return 1
        # 백업은 성공했지만 작업 스케줄러가 알림을 보내도록 오류 코드로 끝낸다.
        return EXIT_TOKEN_EXPIRING if token_expiring else 0
    finally:
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(lock_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
