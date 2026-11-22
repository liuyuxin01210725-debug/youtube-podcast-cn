"""Before installing or claiming a runnable tip, require its SHA on the remote."""
import argparse
import re
import subprocess
import sys

TIP = re.compile(r"(?:(?P<branch>\S+)@)?(?P<sha>[0-9a-fA-F]{40})")


class RemoteTipError(ValueError):
    pass


def require_remote_tip(tip, *, remote="origin", runner=subprocess.run):
    """Return the verified SHA from an advertised ref, optionally of a specific branch."""
    match = TIP.fullmatch(tip) if isinstance(tip, str) else None
    if match is None or match["sha"] == "0" * 40:
        raise RemoteTipError("请提供非全零的 40 位十六进制 SHA，或 branch@SHA")
    sha = match["sha"].lower()
    ref = "refs/heads/" + match["branch"] if match["branch"] else None
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tip", metavar="SHA|branch@SHA")
    parser.add_argument("--remote", default="origin")
    args = parser.parse_args(argv)
    try:
        sha = require_remote_tip(args.tip, remote=args.remote)
    except RemoteTipError as error:
        print(error, file=sys.stderr)
        return 1
    print(sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
