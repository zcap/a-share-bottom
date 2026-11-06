#!/usr/bin/env bash
# 计算状态（每只股票的复权因子、历史最低价等）保存在本仓库的 data 分支。
# 每次保存都只保留一个提交，仓库不会越来越大。
#   bash scripts/state.sh pull   取回到 state/ 目录
#   bash scripts/state.sh push   把 state/ 目录保存回 data 分支
set -euo pipefail

DIR=state
if [ -n "${STATE_REPO_URL:-}" ]; then
  REPO_URL="$STATE_REPO_URL" # 本地测试用
else
  : "${GITHUB_TOKEN:?需要 GITHUB_TOKEN}"
  : "${GITHUB_REPOSITORY:?需要 GITHUB_REPOSITORY}"
  REPO_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
fi

case "${1:-}" in
  pull)
    rm -rf "$DIR" .state_repo
    set +e
    git ls-remote --exit-code --heads "$REPO_URL" data >/dev/null 2>&1
    rc=$?
    set -e
    if [ "$rc" -eq 0 ]; then
      git clone --quiet --depth 1 --branch data "$REPO_URL" .state_repo
      mkdir -p "$DIR"
      cp -a .state_repo/. "$DIR"/
      rm -rf "$DIR/.git" .state_repo
      echo "已取回上次的计算状态"
    elif [ "$rc" -eq 2 ]; then
      mkdir -p "$DIR"
      echo "还没有 data 分支，这是第一次运行，将从头构建"
    else
      echo "无法访问仓库（git ls-remote 返回 $rc），停止运行以免覆盖已有数据"
      exit 1
    fi
    touch "$DIR/.pulled"
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
    git checkout -q -b data
    git add -A
    git -c user.name="github-actions[bot]" \
        -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
        commit -q -m "计算状态 $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    git push -q -f "$REPO_URL" data
    echo "计算状态已保存到 data 分支"
    ;;
  *)
    echo "用法：bash scripts/state.sh pull|push"
    exit 2
    ;;
esac
