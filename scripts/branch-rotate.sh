#!/usr/bin/env bash
#
# branch-rotate.sh - Manage branch rotation for OSS contribution workflow
#
# Use cases:
#   1. Normal development: work on your own fork with 'main' as primary branch
#   2. OSS contribution: sync with upstream, create clean PR branches
#
# Commands:
#   contribute [branch-name]  - Switch to contribution mode
#                               Saves main->local, syncs main with upstream
#                               Optionally creates feature branch
#   develop                   - Switch back to development mode
#                               Restores local->main for normal work
#   status                    - Show current branch configuration
#
set -euo pipefail

UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-main}"
LOCAL_BRANCH="local"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info() { echo -e "${BLUE}→${NC} $*"; }
success() { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
error() { echo -e "${RED}✗${NC} $*" >&2; }

# Check if we have uncommitted changes
check_clean_worktree() {
    if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
        error "You have uncommitted changes. Please commit or stash them first."
        git status --short
        exit 1
    fi
}

# Check if upstream remote exists
check_upstream() {
    if ! git remote get-url "$UPSTREAM_REMOTE" &>/dev/null; then
        error "Remote '$UPSTREAM_REMOTE' not found."
        echo "Add it with: git remote add $UPSTREAM_REMOTE <upstream-url>"
        exit 1
    fi
}

# Get current branch name
current_branch() {
    git rev-parse --abbrev-ref HEAD
}

# Check if branch exists
branch_exists() {
    git show-ref --verify --quiet "refs/heads/$1" 2>/dev/null
}

cmd_status() {
    echo ""
    echo "Branch Rotation Status"
    echo "======================"
    echo ""

    local current=$(current_branch)
    echo "Current branch: $current"
    echo ""

    echo "Local branches:"
    git branch -vv | grep -E "^\*?\s+(main|local|feat/)" | while read line; do
        echo "  $line"
    done
    echo ""

    if branch_exists "main"; then
        local main_commit=$(git rev-parse --short main)
        echo "main:  $main_commit"
    else
        echo "main:  (does not exist)"
    fi

    if branch_exists "$LOCAL_BRANCH"; then
        local local_commit=$(git rev-parse --short "$LOCAL_BRANCH")
        echo "local: $local_commit"
    else
        echo "local: (does not exist)"
    fi

    echo ""
    if git remote get-url "$UPSTREAM_REMOTE" &>/dev/null; then
        git fetch "$UPSTREAM_REMOTE" --quiet 2>/dev/null || true
        local upstream_commit=$(git rev-parse --short "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH" 2>/dev/null || echo "unknown")
        echo "upstream/$UPSTREAM_BRANCH: $upstream_commit"

        if branch_exists "main"; then
            local ahead_behind=$(git rev-list --left-right --count main..."$UPSTREAM_REMOTE/$UPSTREAM_BRANCH" 2>/dev/null || echo "? ?")
            local ahead=$(echo $ahead_behind | cut -d' ' -f1)
            local behind=$(echo $ahead_behind | cut -d' ' -f2)
            echo ""
            echo "main is $ahead commits ahead, $behind commits behind upstream"
        fi
    else
        warn "Upstream remote '$UPSTREAM_REMOTE' not configured"
    fi
    echo ""
}

cmd_contribute() {
    local feature_branch="${1:-}"

    info "Switching to contribution mode..."
    echo ""

    check_clean_worktree
    check_upstream

    # Fetch upstream
    info "Fetching $UPSTREAM_REMOTE..."
    git fetch "$UPSTREAM_REMOTE"

    # Check if we're currently on main
    local current=$(current_branch)

    # If local branch exists, delete it (it's outdated)
    if branch_exists "$LOCAL_BRANCH"; then
        if [[ "$current" == "$LOCAL_BRANCH" ]]; then
            # Switch to a detached HEAD first
            git checkout --detach HEAD 2>/dev/null
        fi
        info "Removing outdated '$LOCAL_BRANCH' branch..."
        git branch -D "$LOCAL_BRANCH"
    fi

    # Rename main to local
    if branch_exists "main"; then
        if [[ "$current" == "main" ]]; then
            info "Renaming main → $LOCAL_BRANCH..."
            git branch -m main "$LOCAL_BRANCH"
            current="$LOCAL_BRANCH"
        else
            info "Renaming main → $LOCAL_BRANCH..."
            git branch -m main "$LOCAL_BRANCH"
        fi
        success "Saved your main as '$LOCAL_BRANCH'"
    fi

    # Create clean main from upstream
    info "Creating clean main from $UPSTREAM_REMOTE/$UPSTREAM_BRANCH..."
    git checkout -b main "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH"
    success "main is now synced with upstream"

    # Create feature branch if specified
    if [[ -n "$feature_branch" ]]; then
        # Normalize branch name (replace spaces with dashes, add feat/ prefix if needed)
        local normalized_branch=$(echo "$feature_branch" | tr ' ' '-' | tr '[:upper:]' '[:lower:]')
        if [[ ! "$normalized_branch" =~ ^(feat|fix|chore|docs|refactor)/ ]]; then
            normalized_branch="feat/$normalized_branch"
        fi

        info "Creating feature branch '$normalized_branch'..."
        git checkout -b "$normalized_branch"
        success "Ready to work on '$normalized_branch'"
    fi

    echo ""
    success "Contribution mode activated!"
    echo ""
    echo "Your setup:"
    echo "  • $LOCAL_BRANCH: Your fork's work (preserved)"
    echo "  • main: Clean, synced with upstream"
    if [[ -n "$feature_branch" ]]; then
        echo "  • $(current_branch): Your PR branch (current)"
    fi
    echo ""
    echo "To cherry-pick commits from your fork:"
    echo "  git log --oneline $UPSTREAM_REMOTE/$UPSTREAM_BRANCH..$LOCAL_BRANCH"
    echo "  git cherry-pick <commit-hash>"
    echo ""
}

cmd_develop() {
    info "Switching to development mode..."
    echo ""

    check_clean_worktree

    local current=$(current_branch)

    # Check if local branch exists
    if ! branch_exists "$LOCAL_BRANCH"; then
        error "'$LOCAL_BRANCH' branch not found. Nothing to restore."
        echo "You may already be in development mode, or need to run 'contribute' first."
        exit 1
    fi

    # If on a feature branch, warn the user
    if [[ "$current" != "main" && "$current" != "$LOCAL_BRANCH" ]]; then
        warn "Currently on '$current'. This branch will be preserved."
    fi

    # Switch to local branch first if we're on main
    if [[ "$current" == "main" ]]; then
        git checkout "$LOCAL_BRANCH"
    fi

    # Delete or rename current main
    if branch_exists "main"; then
        info "Removing contribution main branch..."
        git branch -D main
    fi

    # Rename local to main
    info "Restoring $LOCAL_BRANCH → main..."
    git branch -m "$LOCAL_BRANCH" main
    git checkout main

    success "Development mode activated!"
    echo ""
    echo "Your 'main' branch is restored to your fork's state."
    echo "You can now continue normal development."
    echo ""
}

usage() {
    cat << 'EOF'
Usage: branch-rotate.sh <command> [options]

Commands:
  contribute [branch-name]  Switch to contribution mode
                            - Saves current main as 'local'
                            - Creates clean main from upstream
                            - Optionally creates feature branch for PR

  develop                   Switch back to development mode
                            - Restores 'local' as main
                            - For normal fork development

  status                    Show current branch configuration

Examples:
  # Start working on a new PR
  ./scripts/branch-rotate.sh contribute mcp-admin-consolidation

  # Just sync with upstream, no feature branch yet
  ./scripts/branch-rotate.sh contribute

  # Go back to normal development
  ./scripts/branch-rotate.sh develop

  # Check current setup
  ./scripts/branch-rotate.sh status

Environment:
  UPSTREAM_REMOTE   Remote name for upstream (default: upstream)
  UPSTREAM_BRANCH   Branch name on upstream (default: main)

EOF
}

# Main entry point
main() {
    local cmd="${1:-}"
    shift || true

    case "$cmd" in
        contribute|c)
            cmd_contribute "$@"
            ;;
        develop|dev|d)
            cmd_develop "$@"
            ;;
        status|s)
            cmd_status "$@"
            ;;
        help|--help|-h)
            usage
            ;;
        "")
            usage
            exit 1
            ;;
        *)
            error "Unknown command: $cmd"
            echo ""
            usage
            exit 1
            ;;
    esac
}

main "$@"
