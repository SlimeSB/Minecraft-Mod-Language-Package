#!/usr/bin/env python3
"""
migrate_prs.py - 将开放 PR 中的旧路径迁移至新路径

旧结构: projects/{version}/assets/{mod}/{namespace}/lang/
新结构: projects/assets/{mod}/{version}/{namespace}/lang/

用法:
  python migrate_prs.py 5966              # 迁移指定 PR
  python migrate_prs.py 5966 5964 5963   # 迁移多个 PR
  python migrate_prs.py --all             # 迁移所有开放 PR
  python migrate_prs.py --all --dry-run  # 预览，不实际执行
"""

import subprocess
import json
import sys
import argparse
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

REPO = "CFPAOrg/Minecraft-Mod-Language-Package"
EXCLUDE_PRS = {5967}

VERSIONS = {
    "1.12.2", "1.16", "1.16-fabric",
    "1.18", "1.18-fabric",
    "1.19",
    "1.20", "1.20-fabric",
    "1.21", "1.21-fabric",
    "26.1", "26.1-fabric",
}


def run(cmd: str, check=False, capture=True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd, shell=True, capture_output=capture,
        encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"命令失败: {cmd}")
    return result


def check_deps():
    if run("gh --version").returncode != 0:
        print("错误: 未找到 GitHub CLI (gh)，请先安装：")
        print()
        print("  Windows (winget):  winget install --id GitHub.cli")
        print("  Windows (scoop):   scoop install gh")
        print("  macOS (brew):      brew install gh")
        print("  Linux (apt):       sudo apt install gh")
        print("  其他平台/手动安装: https://cli.github.com/")
        print()
        print("安装完成后重新运行此脚本。")
        sys.exit(1)

    if run("gh auth status").returncode != 0:
        print("错误: GitHub CLI 未授权，请运行以下命令登录：")
        print()
        print("  gh auth login")
        print()
        print("  按提示选择:")
        print("    ? Where do you use GitHub? → GitHub.com")
        print("    ? What is your preferred protocol? → HTTPS")
        print("    ? Authenticate Git with your GitHub credentials? → Yes")
        print("    ? How would you like to authenticate? → Login with a web browser")
        print()
        print("登录完成后重新运行此脚本。")
        sys.exit(1)

    if run("git --version").returncode != 0:
        print("错误: 未找到 git。")
        print("  下载地址: https://git-scm.com/downloads")
        sys.exit(1)

    print("✓ 环境检查通过")


def is_old_path(path: str) -> bool:
    """旧结构: projects/{GameVer}/assets/{ProjectSlug}/{Namespace}/..."""
    parts = path.replace("\\", "/").split("/")
    return (
        len(parts) >= 5
        and parts[0] == "projects"
        and parts[1] in VERSIONS
        and parts[2] == "assets"
    )


def old_to_new(path: str) -> str:
    """
    projects/{GameVer}/assets/{ProjectSlug}/{Namespace}/lang/{file}
    → projects/assets/{ProjectSlug}/{GameVer}/{Namespace}/lang/{file}
    """
    parts = path.replace("\\", "/").split("/")
    new_parts = ["projects", "assets", parts[3], parts[1], parts[4]] + parts[5:]
    return "/".join(new_parts)


def get_pr_old_files_with_status(pr_number: int) -> list[dict]:
    """获取 PR 中所有旧路径文件的文件名、状态和新路径"""
    r = run(
        f'gh api "repos/{REPO}/pulls/{pr_number}/files" --paginate '
        f'--jq ".[] | {{filename, status}}"'
    )
    if r.returncode != 0:
        raise RuntimeError(f"无法获取 PR #{pr_number} 文件列表")
    result = []
    for line in r.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        if is_old_path(entry["filename"]):
            entry["new_filename"] = old_to_new(entry["filename"])
            result.append(entry)
    return result


def get_pr_info(pr_number: int) -> dict:
    r = run(
        f'gh pr view {pr_number} --repo {REPO} '
        f'--json number,title,headRefName,headRepositoryOwner,maintainerCanModify,state'
    )
    if r.returncode != 0:
        raise RuntimeError(f"无法获取 PR #{pr_number} 信息")
    return json.loads(r.stdout)


def get_current_branch() -> str:
    return run("git branch --show-current").stdout.strip()


def migrate_pr(pr_number: int, dry_run: bool, target_base: str = "upstream/main") -> str:
    """
    执行单个 PR 的路径迁移。

    原理: 读取 PR 在旧路径上的文件内容，然后基于已包含新结构的 target_base
    重新创建这些文件的变更，最后强制推送到 PR 分支。

    这样做的好处是 PR 的新 base 就是 target_base，
    合并到 target_base 时不会有路径相关的冲突。

    返回: 'migrated' | 'skipped' | 'no_permission' | 'error'
    """
    try:
        info = get_pr_info(pr_number)
    except RuntimeError as e:
        print(f"  ✗ {e}")
        return "error"

    if info["state"] != "OPEN":
        print(f"  跳过: 状态为 {info['state']}")
        return "skipped"

    if not info.get("maintainerCanModify"):
        print(f"  ✗ PR 不允许维护者修改分支，无法推送")
        return "no_permission"

    owner = info["headRepositoryOwner"]["login"]
    branch = info["headRefName"]
    print(f"  分支: {owner}:{branch}")
    print(f"  标题: {info['title']}")

    try:
        changed_files = get_pr_old_files_with_status(pr_number)
    except RuntimeError as e:
        print(f"  ✗ {e}")
        return "error"

    if not changed_files:
        print(f"  ✓ 路径已是新格式，跳过")
        return "skipped"

    print(f"  需迁移 {len(changed_files)} 个文件:")
    for f in changed_files:
        print(f"    {f['filename']}  ({f['status']})")
        print(f"    → {f['new_filename']}")

    if dry_run:
        return "migrated"

    original_branch = get_current_branch()
    checked_out = False

    try:
        print(f"  正在检出 PR...")
        run(f"gh pr checkout {pr_number}", check=True, capture=False)
        checked_out = True

        local_branch = run("git rev-parse --abbrev-ref HEAD").stdout.strip()

        file_ops = []
        for f in changed_files:
            old_path = f["filename"]
            new_path = f["new_filename"]
            status = f["status"]

            if status == "removed":
                file_ops.append((new_path, None, "delete"))
            else:
                r = run(f'git show HEAD:"{old_path}"')
                if r.returncode == 0:
                    file_ops.append((new_path, r.stdout, "write"))
                else:
                    print(f"  ⚠ 无法读取文件内容: {old_path}")
                    return "error"

        print(f"  获取目标基底: {target_base}...")
        run(f"git fetch upstream", check=True, capture=False)
        run(f"git reset --hard {target_base}", check=True, capture=False)

        writes = 0
        deletes = 0
        for new_path, content, action in file_ops:
            full_path = Path(new_path)
            if action == "write":
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content, encoding="utf-8")
                writes += 1
            elif action == "delete":
                if full_path.exists():
                    full_path.unlink()
                    deletes += 1

        print(f"  写入 {writes} 个文件，删除 {deletes} 个文件")

        if writes == 0 and deletes == 0:
            print(f"  ⚠ 没有实际变更")
            return "error"

        run("git add -A", check=True, capture=False)

        diff = run("git diff --cached --name-only").stdout.strip()
        if not diff:
            print(f"  ⚠ 文件内容与目标基底相同，无变更需要提交")
            return "skipped"

        run(
            'git commit -m "chore: migrate to new path structure"'
            ' -m "自动迁移: PR #{}"'.format(pr_number),
            check=True, capture=False,
        )

        run(f"git push --force origin {local_branch}", check=True, capture=False)
        print(f"  ✓ 迁移完成，已强制推送")
        return "migrated"

    except RuntimeError as e:
        print(f"  ✗ {e}")
        return "error"

    finally:
        if original_branch and checked_out:
            run(f"git checkout {original_branch}")


def get_all_open_pr_numbers() -> list[int]:
    r = run(f'gh pr list --repo {REPO} --state open --json number --limit 500')
    if r.returncode != 0:
        print("错误: 无法获取 PR 列表")
        sys.exit(1)
    prs = json.loads(r.stdout)
    return [pr["number"] for pr in prs if pr["number"] not in EXCLUDE_PRS]


def main():
    parser = argparse.ArgumentParser(
        description="将开放 PR 中的旧路径结构迁移至新路径结构",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python migrate_prs.py 5966                   迁移 PR #5966
  python migrate_prs.py 5966 5964 5963        迁移多个指定 PR
  python migrate_prs.py --all                  迁移全部开放 PR
  python migrate_prs.py --all --dry-run       预览所有需要迁移的 PR
  python migrate_prs.py 5966 --dry-run        预览单个 PR
        """,
    )
    parser.add_argument(
        "prs", nargs="*", type=int, metavar="PR_NUMBER",
        help="要迁移的 PR 编号（可指定多个）"
    )
    parser.add_argument("--all", action="store_true", help="迁移所有开放 PR")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅预览需要迁移的内容，不执行任何修改"
    )
    parser.add_argument(
        "--target-base", default="upstream/main",
        help="目标基底分支/提交（必须包含新路径结构），默认: upstream/main"
    )

    args = parser.parse_args()

    if not args.prs and not args.all:
        parser.print_help()
        sys.exit(0)

    check_deps()

    if args.dry_run:
        print("\n=== 预览模式（不会执行任何修改）===\n")

    if args.all:
        print("正在获取所有开放 PR...")
        pr_numbers = get_all_open_pr_numbers()
        print(f"共 {len(pr_numbers)} 个 PR（已排除 #{', #'.join(str(n) for n in EXCLUDE_PRS)}）\n")
    else:
        pr_numbers = args.prs

    results: dict[str, list[int]] = {
        "migrated": [], "skipped": [], "no_permission": [], "error": []
    }

    for number in pr_numbers:
        print(f"\n── PR #{number} ──")
        status = migrate_pr(number, dry_run=args.dry_run, target_base=args.target_base)
        results[status].append(number)

    print("\n" + "=" * 44)
    if args.dry_run:
        print("预览完成（未执行任何修改）")
        print(f"可自动迁移: {len(results['migrated'])} 个 {results['migrated']}")
    else:
        print(f"迁移成功:   {len(results['migrated'])} 个 {results['migrated']}")
    print(f"已是新路径: {len(results['skipped'])} 个")
    if results["no_permission"]:
        print(f"无修改权限: {len(results['no_permission'])} 个 {results['no_permission']}")
    if results["error"]:
        print(f"出错:       {len(results['error'])} 个 {results['error']}")


if __name__ == "__main__":
    main()
