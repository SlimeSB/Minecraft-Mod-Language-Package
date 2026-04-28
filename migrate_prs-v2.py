#!/usr/bin/env python3
"""
migrate_prs.py - 将开放 PR 中的旧路径迁移至新路径

旧结构: projects/{GameVer}/assets/{ProjectSlug}/{Namespace}/lang/
新结构: projects/assets/{ProjectSlug}/{GameVer}/{Namespace}/lang/

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

# Windows 默认 GBK 编码会导致 gh/git 输出乱码，统一强制 UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

REPO = "CFPAOrg/Minecraft-Mod-Language-Package"
EXCLUDE_PRS = {5967}  # 路径结构变更 PR 本身，不参与迁移
MIGRATION_COMMIT_MSG = "chore: migrate to new path structure"

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


def get_pr_old_files(pr_number: int) -> list[str]:
    r = run(f'gh api "repos/{REPO}/pulls/{pr_number}/files" --paginate --jq ".[].filename"')
    if r.returncode != 0:
        raise RuntimeError(f"无法获取 PR #{pr_number} 文件列表")
    files = [f.strip() for f in r.stdout.strip().splitlines() if f.strip()]
    return [f for f in files if is_old_path(f)]


def has_migration_commit(pr_number: int) -> bool:
    """检查 PR 是否包含之前迁移脚本添加的提交（用于发现需要重新迁移的 PR）"""
    r = run(f'gh api "repos/{REPO}/pulls/{pr_number}/commits" --jq ".[].commit.message"')
    if r.returncode != 0:
        return False
    return MIGRATION_COMMIT_MSG in r.stdout


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


def migrate_pr(pr_number: int, dry_run: bool) -> str:
    """
    通过 git rebase 将 PR 迁移至新路径结构。

    与旧的 git mv + commit 方案相比，rebase 方案的优势：
    - 原始提交的 Author 信息全程保留，git blame 归属正确
    - git 的重命名检测会自动将旧路径的变更映射到新路径，不会产生文件冲突
    - 对于已被旧脚本错误迁移的 PR，会先撤销迁移提交再重新 rebase

    返回: 'migrated' | 'skipped' | 'manual' | 'no_permission' | 'error'
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
        old_files = get_pr_old_files(pr_number)
    except RuntimeError as e:
        print(f"  ✗ {e}")
        return "error"

    _has_migration_commit = has_migration_commit(pr_number)

    if not old_files and not _has_migration_commit:
        print("  ✓ 路径已是新格式，跳过")
        return "skipped"

    if old_files:
        print(f"  包含 {len(old_files)} 个旧路径文件，将通过 rebase 迁移:")
        for f in old_files:
            print(f"    {f}")
            print(f"    → {old_to_new(f)}")
    if _has_migration_commit:
        print("  检测到之前的迁移提交，将撤销后重新 rebase")

    if dry_run:
        return "migrated"

    original_branch = get_current_branch()
    checked_out = False
    saved_head = None

    try:
        run(f"gh pr checkout {pr_number}", check=True, capture=False)
        checked_out = True

        # 拉取最新 main，确保 rebase 基准是最新状态
        run("git fetch origin main", check=True, capture=False)

        saved_head = run("git rev-parse HEAD").stdout.strip()

        # 撤销分支顶端连续的迁移提交（由本脚本旧版本添加）
        tip_migration_count = 0
        for msg in run("git log --format=%s origin/main..HEAD").stdout.strip().splitlines():
            if msg.strip() == MIGRATION_COMMIT_MSG:
                tip_migration_count += 1
            else:
                break
        if tip_migration_count > 0:
            run(f"git reset --hard HEAD~{tip_migration_count}", check=True)
            print(f"  已撤销 {tip_migration_count} 个迁移提交")

        # rebase 到 origin/main
        # git 的 three-way merge 会通过重命名检测，将旧路径的变更自动应用到新路径
        # 原始提交的 Author 全程保留，git blame 不受影响
        result = run(
            "git -c diff.renames=true -c merge.renames=true rebase origin/main"
        )
        if result.returncode != 0:
            conflict_files = run("git diff --name-only --diff-filter=U").stdout.strip()
            print("  ✗ rebase 遇到冲突，需手动解决:")
            for f in (conflict_files.splitlines() or ["(无法获取冲突列表，见上方输出)"]):
                print(f"    {f}")
            run("git rebase --abort")
            # 恢复到操作前的状态（含已撤销的迁移提交）
            run(f"git reset --hard {saved_head}")
            return "manual"

        # --force-with-lease 避免意外覆盖他人在此期间推送的提交
        run("git push --force-with-lease", check=True, capture=False)
        print("  ✓ 迁移完成，已推送（原始提交者 blame 已保留）")
        return "migrated"

    except RuntimeError as e:
        print(f"  ✗ {e}")
        if checked_out and saved_head:
            run(f"git reset --hard {saved_head}")
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
        "migrated": [], "skipped": [], "no_permission": [], "manual": [], "error": []
    }

    for number in pr_numbers:
        print(f"\n── PR #{number} ──")
        status = migrate_pr(number, dry_run=args.dry_run)
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
    if results["manual"]:
        print(f"需手动处理: {len(results['manual'])} 个 {results['manual']}")
    if results["error"]:
        print(f"出错:       {len(results['error'])} 个 {results['error']}")


if __name__ == "__main__":
    main()
