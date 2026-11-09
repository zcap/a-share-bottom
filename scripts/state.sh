#!/usr/bin/env bash
# 计算状态（每只股票的复权因子、历史最低价等）保存在本仓库的分支里：A 股 data，港股 data-hk，美股 data-us。
# 每次保存都只保留一个提交，仓库不会越来越大。
#   bash scripts/state.sh pull  [分支] [目录]   取回上次的状态（默认 data 分支 → state/ 目录）
#   bash scripts/state.sh push  [分支] [目录]   把目录保存回分支
#   bash scripts/state.sh fetch 分支 目录       只读取回（发布网页时用）；分支还不存在时得到空目录
set -euo pipefail

CMD="${1:-}"
BRANCH="${2:-data}"
DIR="${3:-state}"
if [ -n "${STATE_REPO_URL:-}" ]; then
  REPO_URL="$STATE_REPO_URL" # 本地测试用
else
  : "${GITHUB_TOKEN:?需要 GITHUB_TOKEN}"
  : "${GITHUB_REPOSITORY:?需要 GITHUB_REPOSITORY}"
  REPO_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
fi

clone_into() {
  # 把分支内容取回到 $DIR：成功返回 0；分支不存在返回 2（得到空目录）；其他任何失败都直接退出，
  # 避免之后用空状态覆盖已有数据。（在 if / || 里调用时 set -e 不生效，所以每一步都显式检查）
  rm -rf "$DIR"
  local rc=0
  git ls-remote --exit-code --heads "$REPO_URL" "$BRANCH" >/dev/null 2>&1 || rc=$?
  if [ "$rc" -eq 2 ]; then
    mkdir -p "$DIR" || exit 1
    return 2
  fi
  if [ "$rc" -ne 0 ]; then
    echo "无法访问仓库（git ls-remote 返回 $rc），停止运行以免覆盖已有数据"
    exit 1
  fi
  local tmp
  tmp=$(mktemp -d) || exit 1
  git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$tmp/repo" \
    || { echo "取回 $BRANCH 分支失败，停止运行以免覆盖已有数据"; exit 1; }
  { mkdir -p "$DIR" && cp -a "$tmp/repo/." "$DIR"/; } || { echo "复制状态文件失败"; exit 1; }
  rm -rf "$DIR/.git" "$tmp"
  return 0
}

case "$CMD" in
  pull)
    rc=0
    clone_into || rc=$?
    if [ "$rc" -eq 0 ]; then
      echo "已从 $BRANCH 分支取回上次的计算状态"
    else
      echo "还没有 $BRANCH 分支，这是第一次运行，将从头构建"
    fi
    touch "$DIR/.pulled"
    ;;
  fetch)
    rc=0
    clone_into || rc=$?
    if [ "$rc" -eq 0 ]; then
      echo "已取回 $BRANCH 分支"
    else
      echo "还没有 $BRANCH 分支（这个市场还没有运行过）"
    fi
    ;;
  push)
    if [ ! -f "$DIR/.pulled" ]; then
      echo "没有成功取回上次的状态，跳过保存，避免覆盖已有数据"
      exit 0
    fi
    if [ ! -f "$DIR/meta.json" ]; then
      echo "没有可保存的状态文件"
      exit 0
    fi
    tmp=$(mktemp -d)
    cp -a "$DIR"/. "$tmp"/
    rm -f "$tmp/.pulled"
    cd "$tmp"
    git init -q
    git checkout -q -b "$BRANCH"
    git add -A
    git -c user.name="github-actions[bot]" \
        -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
        commit -q -m "计算状态 $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    git push -q -f "$REPO_URL" "$BRANCH"
    echo "计算状态已保存到 $BRANCH 分支"
    ;;
  *)
    echo "用法：bash scripts/state.sh pull|push [分支] [目录]；bash scripts/state.sh fetch 分支 目录"
    exit 2
    ;;
esac
