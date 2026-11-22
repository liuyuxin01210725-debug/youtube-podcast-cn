"""Before installing or claiming a runnable tip, require its SHA on the remote."""
import argparse
from pathlib import Path
import re
import subprocess
import sys

TIP = re.compile(r"(?:(?P<branch>[^\s@]+)@)?(?P<sha>[0-9a-fA-F]{7,40})")


class RemoteTipError(ValueError):
    pass


def parse_tip(tip, *, allow_short=False):
    match = TIP.fullmatch(tip) if isinstance(tip, str) else None
    if (match is None or not match["sha"].strip("0")
            or (not allow_short and len(match["sha"]) != 40)):
        length = "7–40" if allow_short else "40"
        raise RemoteTipError(f"请提供非全零的 {length} 位十六进制 SHA，或 branch@SHA")
    return match["branch"], match["sha"].lower()


def require_remote_tip(tip, *, remote="origin", runner=subprocess.run):
    """Return the verified SHA from an advertised ref, optionally of a specific branch."""
    branch, sha = parse_tip(tip)
    ref = "refs/heads/" + branch if branch else None
    argv = ["git", "ls-remote", "--exit-code", "--", remote]
    if ref:
        argv.append(ref)
    try:
        result = runner(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RemoteTipError("无法查询远端引用") from error
    if result.returncode != 0:
        raise RemoteTipError("git ls-remote 未成功，无法确认远端 tip")
    for line in result.stdout.splitlines():
        advertised_sha, _, advertised_ref = line.partition("\t")
        if advertised_sha.lower() != sha:
            continue
        if ref:
            if advertised_ref == ref:
                return sha
        elif advertised_ref == "HEAD" or advertised_ref.startswith("refs/"):
            return sha
    raise RemoteTipError("远端引用未公布指定 tip，请先 push 并确认分支指向该 SHA")


def resolve_build_sha(sha, runner):
    if len(sha) == 40:
        return sha
    try:
        result = runner(["git", "rev-parse", f"--disambiguate={sha}"],
                        capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RemoteTipError("无法解析构建 SHA 的短前缀") from error
    full_sha = result.stdout.strip()
    if (result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", full_sha)
            or not full_sha.startswith(sha)):
        raise RemoteTipError("构建 SHA 的短前缀必须在本地 Git 对象中唯一")
    return full_sha


def require_current_binary(record, build_sha, runner):
    try:
        current = (record.parent / "CURRENT-iphoneos").resolve(strict=True)
        app = current if current.suffix == ".app" else (current / "PrivateListen.app").resolve(strict=True)
        if not app.is_dir() or not (app / "Info.plist").is_file():
            raise RemoteTipError("CURRENT-iphoneos 未指向可检查的 App")
        package_name = current.name if app.parent == current else app.stem
        # Versioned package names carry the build SHA as a separate token.
        revisions = re.findall(r"(?<![\w])[0-9a-fA-F]{7,40}(?![\w])", package_name)
        if len(revisions) != 1 or resolve_build_sha(revisions[0].lower(), runner) != build_sha:
            raise RemoteTipError("CURRENT-iphoneos 的包名无法对应构建源 SHA")
        binary = app / "PrivateListen.debug.dylib"
        if not binary.exists() and not binary.is_symlink():
            binary = app / "PrivateListen"
        if binary.resolve(strict=True).parent != app or not binary.is_file():
            raise RemoteTipError("CURRENT-iphoneos 缺少包内二进制")
        blob = binary.read_bytes()
    except (OSError, RuntimeError) as error:
        raise RemoteTipError("无法读取 CURRENT-iphoneos App 或包内二进制") from error
    for token in ("继续播放", "听后感", "mock_not_official"):
        if token.encode("utf-8") not in blob:
            raise RemoteTipError(f"CURRENT-iphoneos 二进制缺少「{token}」")


def require_claim_ready(tip, record, *, remote="origin", runner=subprocess.run):
    branch, sha = parse_tip(tip, allow_short=True)
    try:
        # Only the leading branch@SHA is part of the build source record.
        fields = record.read_bytes().split(maxsplit=1)
        build_tip = fields[0].decode("utf-8") if fields else None
    except (OSError, ValueError) as error:
        raise RemoteTipError("无法读取 CURRENT-iphoneos 构建源记录") from error
    build_branch, build_sha = parse_tip(build_tip, allow_short=True)
    if not build_branch:
        raise RemoteTipError("构建源记录必须以 branch@SHA 开头")
    same_prefix = sha.startswith(build_sha) or build_sha.startswith(sha)
    if (branch and branch != build_branch) or not same_prefix:
        raise RemoteTipError("宣称的 tip 与 CURRENT-iphoneos 构建源记录不一致")
    build_sha = resolve_build_sha(build_sha, runner)
    if resolve_build_sha(sha, runner) != build_sha:
        raise RemoteTipError("宣称的 tip 与 CURRENT-iphoneos 构建源记录不一致")
    require_remote_tip(f"{build_branch}@{build_sha}", remote=remote, runner=runner)
    require_current_binary(record, build_sha, runner)
    return build_sha


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tip", metavar="SHA|branch@SHA")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--build-record", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.build_record is None:
            sha = require_remote_tip(args.tip, remote=args.remote)
        else:
            sha = require_claim_ready(args.tip, args.build_record, remote=args.remote)
    except RemoteTipError as error:
        print(error, file=sys.stderr)
        return 1
    print(sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
