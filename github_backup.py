#!/usr/bin/env python3
"""개인 GitHub 저장소(소스코드)를 Synology NAS에 bare 미러로 백업한다.

설정은 환경 파일(기본: 이 스크립트와 같은 폴더의 githubsync.env)에서 읽는다.
같은 이름의 소문자 실행 인자(--github_token=값 --backup_dest=값)를 주면 환경 파일보다 우선한다.
--dry_run 을 주면 저장소는 받지 않고, 백업 경로 폴더 생성과 쓰기 권한, 저장소 접근 가능 여부만 확인한다.
백업 경로(BACKUP_DEST)를 지정하지 않으면 스크립트 폴더 아래의 <소유자명> 폴더에 백업한다.
"""
import argparse
import base64
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


def api_get(path, token, what):
    """GitHub API 를 호출해 JSON 을 돌려준다. 실패하면 BackupError."""
    request = urllib.request.Request(API_URL + path, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "syno-githubsync",
    })
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise BackupError("GitHub API 오류: HTTP %d (%s)" % (e.code, what))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise BackupError("GitHub API 호출 실패 (%s): %s" % (what, e))


def get_login(token):
    """토큰 소유자의 GitHub 계정 이름을 돌려준다."""
    user = api_get("/user", token, "사용자 조회")
    login = user.get("login") if isinstance(user, dict) else None
    if not login or not REPO_NAME_RE.match(login):
        raise BackupError("GitHub 계정 이름을 확인할 수 없음")
    return login


def list_repos(token):
    """내가 소유한 저장소의 owner/name 목록을 돌려준다. 실패하면 BackupError."""
    names = []
    page = 1
    while True:
        what = "page %d" % page
        repos = api_get("/user/repos?per_page=100&page=%d&affiliation=owner" % page,
                        token, what)
        if not isinstance(repos, list):
            raise BackupError("GitHub API 응답 형식이 올바르지 않음 (%s)" % what)
        if not repos:
            return names
        try:
            names.extend(repo["full_name"] for repo in repos)
        except (KeyError, TypeError):
            raise BackupError("GitHub API 응답에 full_name 이 없음 (%s)" % what)
        page += 1


def run_git(args, auth_header):
    """git 을 실행한다. 토큰은 URL/.git/config 에 남기지 않고 헤더로만 전달한다."""
    cmd = ["git", "-c", "http.extraheader=" + auth_header] + args
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "시간 초과"
    except OSError as e:
        return False, str(e)
    return result.returncode == 0, " ".join(result.stderr.split())


def make_auth_header(token):
    auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
    return "Authorization: Basic " + auth


def repo_dir_name(full_name):
    """owner/name 에서 저장소 폴더 이름을 돌려준다. 허용되지 않는 이름이면 None."""
    name = full_name.split("/", 1)[-1]
    if not REPO_NAME_RE.match(name) or name in (".", ".."):
        return None
    return name


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
    ok, err = run_git(["clone", "--mirror", url, tmp], auth_header)
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
        log("[dry-run] 이미 실행 중이거나 이전 실행이 비정상 종료됨: " + lock_dir)
        return 3

    try:
        repos = list_repos(token)
    except BackupError as e:
        log(str(e))
        log("저장소 목록 조회 실패")
        return 1
    if not repos:
        log("저장소가 하나도 조회되지 않음 (토큰 권한 확인 필요)")
        return 1

    new_count = update_count = fail_count = 0
    for full_name in repos:
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
        os.mkdir(lock_dir)
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
        try:
            repos = list_repos(token)
        except BackupError as e:
            log(str(e))
            log("저장소 목록 조회 실패")
            return 1
        if not repos:
            # 토큰 범위 문제 등으로 빈 목록이 오면 성공으로 오인하지 않는다.
            log("저장소가 하나도 조회되지 않음 (토큰 권한 확인 필요)")
            return 1

        ok_count = 0
        fail_count = 0
        for full_name in repos:
            ok, err = backup_repo(full_name, dest, work_dir, auth_header)
            if ok:
                ok_count += 1
            else:
                log("실패(%s): %s" % (full_name, err))
                fail_count += 1

        log("완료: 성공 %d, 실패 %d" % (ok_count, fail_count))
        return 0 if fail_count == 0 else 1
    finally:
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(lock_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
