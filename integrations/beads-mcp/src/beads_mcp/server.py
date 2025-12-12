"""FastMCP server for beads issue tracker."""

import asyncio
import atexit
import importlib.metadata
import logging
import os
import signal
import subprocess
import sys
from functools import wraps
from types import FrameType
from typing import Any, Awaitable, Callable, TypeVar

from fastmcp import FastMCP

from beads_mcp.models import BlockedIssue, BriefIssue, DependencyType, Issue, IssueStatus, IssueType, OperationResult, Stats
from beads_mcp.tools import (
    beads_add_dependency,
    beads_blocked,
    beads_close_issue,
    beads_comment_add,
    beads_comment_list,
    beads_create_issue,
    beads_dep_tree,
    beads_detect_pollution,
    beads_get_schema_info,
    beads_init,
    beads_inspect_migration,
    beads_list_issues,
    beads_quickstart,
    beads_ready_work,
    beads_remove_dependency,
    beads_repair_deps,
    beads_reopen_issue,
    beads_show_issue,
    beads_stats,
    beads_update_issue,
    beads_validate,
    current_workspace,  # ContextVar for per-request workspace routing
)

# Setup logging for lifecycle events
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,  # Ensure logs don't pollute stdio protocol
)

T = TypeVar("T")

# Global state for cleanup
_daemon_clients: list[Any] = []
_cleanup_done = False

# Persistent workspace context (survives across MCP tool calls)
# os.environ doesn't persist across MCP requests, so we need module-level storage
_workspace_context: dict[str, str] = {}

# Create FastMCP server
mcp = FastMCP(
    name="Beads",
    instructions="""
We track work in Beads (bd) instead of Markdown.
Check the resource beads://quickstart to see how.

IMPORTANT: Call set_context with your workspace root before any write operations.

## Context Control (Reduce Token Usage)

When scanning or searching issues, use these parameters to minimize response size:

- `brief=True`: Returns only {id, title, status} - use when scanning for an issue
- `fields=["id", "dependencies"]`: Returns only specific fields - use when checking deps
- `max_description_length=100`: Truncates long descriptions - use for overviews

Examples:
- Finding an issue by name: `list(query="auth", brief=True)`
- Checking what blocks an issue: `show(issue_id, fields=["id", "dependencies"])`
- Quick status check: `ready(brief=True, limit=5)`

## Filtering Issues

Use scoping parameters to narrow results:

- `labels=["bug"]`: Issues with ALL specified labels (AND)
- `labels_any=["p0", "p1"]`: Issues with ANY specified label (OR)
- `query="search term"`: Search in title/description
- `unassigned=True`: Issues with no assignee
- `sort_policy="priority"|"oldest"|"hybrid"`: For ready() sorting

## Dependency Management

- `dep(issue_id, depends_on_id)`: Add blocker
- `dep_remove(issue_id, depends_on_id)`: Remove blocker
- `dep_tree(issue_id)`: See full dependency chain

## Labels via Update

Modify labels without replacing all:
- `update(issue_id, add_labels=["bug"])`: Add labels
- `update(issue_id, remove_labels=["wontfix"])`: Remove labels

## Comments

Track decisions and progress:
- `comment_add(issue_id, "Discovered root cause: ...")`: Add note
- `comment_list(issue_id)`: Review discussion

## Brief Output (Default for Write Operations)

Write operations (`create`, `update`, `close`, `reopen`, `dep`, `dep_remove`, `comment_add`)
return minimal confirmations by default to save context:

```json
{"ok": true, "id": "bd-123", "action": "created"}
```

Use `verbose=True` to get full object details when needed:
- `create(..., verbose=True)` - Returns full Issue object
- `update(..., verbose=True)` - Returns updated Issue
- `close(..., verbose=True)` - Returns closed Issue(s)

## Suggest Next (Close)

Use `close(issue_id, suggest_next=True)` to see issues unblocked by this close:
```json
{"ok": true, "id": "bd-1", "action": "closed", "message": "Unblocked: [{'id': 'bd-2', 'title': '...'}]"}
```
""",
)


def cleanup() -> None:
    """Clean up resources on exit.
    
    Closes daemon connections and removes temp files.
    Safe to call multiple times.
    """
    global _cleanup_done
    
    if _cleanup_done:
        return
    
    _cleanup_done = True
    logger.info("Cleaning up beads-mcp resources...")
    
    # Close all daemon client connections
    for client in _daemon_clients:
        try:
            if hasattr(client, 'cleanup'):
                client.cleanup()
                logger.debug(f"Closed daemon client: {client}")
        except Exception as e:
            logger.warning(f"Error closing daemon client: {e}")
    
    _daemon_clients.clear()
    logger.info("Cleanup complete")


def signal_handler(signum: int, frame: FrameType | None) -> None:
    """Handle termination signals gracefully."""
    sig_name = signal.Signals(signum).name
    logger.info(f"Received {sig_name}, shutting down gracefully...")
    cleanup()
    sys.exit(0)


# Register cleanup handlers
atexit.register(cleanup)
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# Get version from package metadata
try:
    __version__ = importlib.metadata.version("beads-mcp")
except importlib.metadata.PackageNotFoundError:
    __version__ = "dev"

logger.info(f"beads-mcp v{__version__} initialized with lifecycle management")


def with_workspace(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Decorator to set workspace context for the duration of a tool call.

    Extracts workspace_root parameter from tool call kwargs, resolves it,
    and sets current_workspace ContextVar for the request duration.
    Falls back to persistent context or BEADS_WORKING_DIR if workspace_root not provided.

    This enables per-request workspace routing for multi-project support.
    """
    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        # Extract workspace_root parameter (if provided)
        workspace_root = kwargs.get('workspace_root')

        # Determine workspace: parameter > persistent context > env > None
        workspace = (
            workspace_root
            or _workspace_context.get("BEADS_WORKING_DIR")
            or os.environ.get("BEADS_WORKING_DIR")
        )

        # Set ContextVar for this request
        token = current_workspace.set(workspace)

        try:
            # Execute tool with workspace context set
            return await func(*args, **kwargs)
        finally:
            # Always reset ContextVar after tool completes
            current_workspace.reset(token)

    return wrapper


def require_context(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Decorator to enforce context has been set before write operations.
    
    Passes if either:
    - workspace_root was provided on tool call (via ContextVar), OR
    - BEADS_WORKING_DIR is set (from set_context)
    
    Only enforces if BEADS_REQUIRE_CONTEXT=1 is set in environment.
    This allows backward compatibility while adding safety for multi-repo setups.
    """
    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        # Only enforce if explicitly enabled
        if os.environ.get("BEADS_REQUIRE_CONTEXT") == "1":
            # Check ContextVar or environment
            workspace = current_workspace.get() or os.environ.get("BEADS_WORKING_DIR")
            if not workspace:
                raise ValueError(
                    "Context not set. Either provide workspace_root parameter or call set_context() first."
                )
        return await func(*args, **kwargs)
    return wrapper


def _find_beads_db(workspace_root: str) -> str | None:
    """Find .beads/*.db by walking up from workspace_root.
    
    Args:
        workspace_root: Starting directory to search from
        
    Returns:
        Absolute path to first .db file found in .beads/, None otherwise
    """
    import glob
    current = os.path.abspath(workspace_root)
    
    while True:
        beads_dir = os.path.join(current, ".beads")
        if os.path.isdir(beads_dir):
            # Find any .db file in .beads/
            db_files = glob.glob(os.path.join(beads_dir, "*.db"))
            if db_files:
                return db_files[0]  # Return first .db file found
        
        parent = os.path.dirname(current)
        if parent == current:  # Reached root
            break
        current = parent
    
    return None


def _resolve_workspace_root(path: str) -> str:
    """Resolve workspace root to git repo root if inside a git repo.
    
    Args:
        path: Directory path to resolve
        
    Returns:
        Git repo root if inside git repo, otherwise the original path
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
            shell=sys.platform == "win32",
            stdin=subprocess.DEVNULL,  # Prevent inheriting MCP's stdin
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        logger.debug(f"Git detection failed for {path}: {e}")
        pass
    
    return os.path.abspath(path)


# Register quickstart resource
@mcp.resource("beads://quickstart", name="Beads Quickstart Guide")
async def get_quickstart() -> str:
    """Get beads (bd) quickstart guide.

    Read this first to understand how to use beads (bd) commands.
    """
    return await beads_quickstart()


# Context management tools
@mcp.tool(
    name="set_context",
    description="Set the workspace root directory for all bd operations. Call this first!",
)
async def set_context(workspace_root: str) -> str:
    """Set workspace root directory and discover the beads database.

    Args:
        workspace_root: Absolute path to workspace/project root directory

    Returns:
        Confirmation message with resolved paths
    """
    # Resolve to git repo root if possible (run in thread to avoid blocking event loop)
    try:
        resolved_root = await asyncio.wait_for(
            asyncio.to_thread(_resolve_workspace_root, workspace_root),
            timeout=5.0,  # Longer timeout to handle slow git operations
        )
    except asyncio.TimeoutError:
        logger.error(f"Git detection timed out after 5s for: {workspace_root}")
        return (
            f"Error: Git repository detection timed out.\n"
            f"  Provided path: {workspace_root}\n"
            f"  This may indicate a slow filesystem or git configuration issue.\n"
            f"  Please ensure the path is correct and git is responsive."
        )

    # Store in persistent context (survives across MCP tool calls)
    _workspace_context["BEADS_WORKING_DIR"] = resolved_root
    _workspace_context["BEADS_CONTEXT_SET"] = "1"

    # Also set in os.environ for compatibility
    os.environ["BEADS_WORKING_DIR"] = resolved_root
    os.environ["BEADS_CONTEXT_SET"] = "1"

    # Find beads database
    db_path = _find_beads_db(resolved_root)

    if db_path is None:
        # Clear any stale DB path
        _workspace_context.pop("BEADS_DB", None)
        os.environ.pop("BEADS_DB", None)
        return (
            f"Context set successfully:\n"
            f"  Workspace root: {resolved_root}\n"
            f"  Database: Not found (run 'bd init' to create)"
        )

    # Set database path in both persistent context and os.environ
    _workspace_context["BEADS_DB"] = db_path
    os.environ["BEADS_DB"] = db_path

    return (
        f"Context set successfully:\n"
        f"  Workspace root: {resolved_root}\n"
        f"  Database: {db_path}"
    )


@mcp.tool(
    name="where_am_i",
    description="Show current workspace context and database path",
)
async def where_am_i(workspace_root: str | None = None) -> str:
    """Show current workspace context for debugging."""
    context_set = (
        _workspace_context.get("BEADS_CONTEXT_SET")
        or os.environ.get("BEADS_CONTEXT_SET")
    )

    if not context_set:
        return (
            "Context not set. Call set_context with your workspace root first.\n"
            f"Current process CWD: {os.getcwd()}\n"
            f"BEADS_WORKING_DIR (persistent): {_workspace_context.get('BEADS_WORKING_DIR', 'NOT SET')}\n"
            f"BEADS_WORKING_DIR (env): {os.environ.get('BEADS_WORKING_DIR', 'NOT SET')}\n"
            f"BEADS_DB: {_workspace_context.get('BEADS_DB') or os.environ.get('BEADS_DB', 'NOT SET')}"
        )

    working_dir = (
        _workspace_context.get("BEADS_WORKING_DIR")
        or os.environ.get("BEADS_WORKING_DIR", "NOT SET")
    )
    db_path = (
        _workspace_context.get("BEADS_DB")
        or os.environ.get("BEADS_DB", "NOT SET")
    )
    actor = os.environ.get("BEADS_ACTOR", "NOT SET")

    return (
        f"Workspace root: {working_dir}\n"
        f"Database: {db_path}\n"
        f"Actor: {actor}"
    )


# Register all tools
@mcp.tool(name="ready", description="Find tasks that have no blockers and are ready to be worked on.")
@with_workspace
async def ready_work(
    limit: int = 10,
    priority: int | None = None,
    assignee: str | None = None,
    # Scoping parameters
    labels: list[str] | None = None,
    labels_any: list[str] | None = None,
    unassigned: bool = False,
    sort_policy: str | None = None,
    # Output control
    brief: bool = False,
    fields: list[str] | None = None,
    max_description_length: int | None = None,
    workspace_root: str | None = None,
) -> list[Issue] | list[BriefIssue] | list[dict[str, Any]]:
    """Find issues with no blocking dependencies that are ready to work on."""
    issues = await beads_ready_work(
        limit=limit,
        priority=priority,
        assignee=assignee,
        labels=labels,
        labels_any=labels_any,
        unassigned=unassigned,
        sort_policy=sort_policy,
    )

    # Strip dependencies/dependents to reduce payload size
    # Use show() for full details
    for issue in issues:
        issue.dependencies = []
        issue.dependents = []

    # Apply output control
    if brief:
        return [BriefIssue(id=i.id, title=i.title, status=i.status) for i in issues]

    if fields:
        return [{k: getattr(i, k, None) for k in fields if hasattr(i, k)} for i in issues]

    if max_description_length:
        for issue in issues:
            if issue.description and len(issue.description) > max_description_length:
                issue.description = issue.description[:max_description_length] + "..."

    return issues


@mcp.tool(
    name="list",
    description="List all issues with optional filters (status, priority, type, assignee, labels, query).",
)
@with_workspace
async def list_issues(
    status: IssueStatus | None = None,
    priority: int | None = None,
    issue_type: IssueType | None = None,
    assignee: str | None = None,
    limit: int = 20,  # Reduced from 50 to avoid MCP buffer overflow
    # Scoping parameters
    labels: list[str] | None = None,
    labels_any: list[str] | None = None,
    query: str | None = None,
    unassigned: bool = False,
    # Output control
    brief: bool = False,
    fields: list[str] | None = None,
    max_description_length: int | None = None,
    workspace_root: str | None = None,
) -> list[Issue] | list[BriefIssue] | list[dict[str, Any]]:
    """List all issues with optional filters."""
    issues = await beads_list_issues(
        status=status,
        priority=priority,
        issue_type=issue_type,
        assignee=assignee,
        limit=limit,
        labels=labels,
        labels_any=labels_any,
        query=query,
        unassigned=unassigned,
    )

    # Strip dependencies/dependents to reduce payload size
    # Use show() for full details
    for issue in issues:
        issue.dependencies = []
        issue.dependents = []

    # Apply output control
    if brief:
        return [BriefIssue(id=i.id, title=i.title, status=i.status) for i in issues]

    if fields:
        return [{k: getattr(i, k, None) for k in fields if hasattr(i, k)} for i in issues]

    if max_description_length:
        for issue in issues:
            if issue.description and len(issue.description) > max_description_length:
                issue.description = issue.description[:max_description_length] + "..."

    return issues


@mcp.tool(
    name="show",
    description="Show detailed information about a specific issue including dependencies and dependents.",
)
@with_workspace
async def show_issue(
    issue_id: str,
    # Output control
    brief: bool = False,
    fields: list[str] | None = None,
    max_description_length: int | None = None,
    workspace_root: str | None = None,
) -> Issue | BriefIssue | dict[str, Any]:
    """Show detailed information about a specific issue."""
    issue = await beads_show_issue(issue_id=issue_id)

    # Apply output control
    if brief:
        return BriefIssue(id=issue.id, title=issue.title, status=issue.status)

    if fields:
        return {k: getattr(issue, k, None) for k in fields if hasattr(issue, k)}

    if max_description_length:
        if issue.description and len(issue.description) > max_description_length:
            issue.description = issue.description[:max_description_length] + "..."

    return issue


@mcp.tool(
    name="create",
    description="""Create a new issue (bug, feature, task, epic, or chore) with optional design,
acceptance criteria, and dependencies. Returns brief confirmation by default; use verbose=True for full Issue.""",
)
@with_workspace
@require_context
async def create_issue(
    title: str,
    description: str = "",
    design: str | None = None,
    acceptance: str | None = None,
    external_ref: str | None = None,
    priority: int = 2,
    issue_type: IssueType = "task",
    assignee: str | None = None,
    labels: list[str] | None = None,
    id: str | None = None,
    deps: list[str] | None = None,
    verbose: bool = False,
    workspace_root: str | None = None,
) -> Issue | OperationResult:
    """Create a new issue."""
    issue = await beads_create_issue(
        title=title,
        description=description,
        design=design,
        acceptance=acceptance,
        external_ref=external_ref,
        priority=priority,
        issue_type=issue_type,
        assignee=assignee,
        labels=labels,
        id=id,
        deps=deps,
    )
    if verbose:
        return issue
    return OperationResult(id=issue.id, action="created")


@mcp.tool(
    name="update",
    description="""Update an existing issue's status, priority, assignee, description, design notes,
acceptance criteria, labels, or time estimate. Use this to claim work (set status=in_progress).
Returns brief confirmation by default; use verbose=True for full Issue.""",
)
@with_workspace
@require_context
async def update_issue(
    issue_id: str,
    status: IssueStatus | None = None,
    priority: int | None = None,
    assignee: str | None = None,
    title: str | None = None,
    description: str | None = None,
    design: str | None = None,
    acceptance_criteria: str | None = None,
    notes: str | None = None,
    external_ref: str | None = None,
    # Label operations
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    # Time estimate
    estimated_minutes: int | None = None,
    # Output control
    verbose: bool = False,
    workspace_root: str | None = None,
) -> Issue | list[Issue] | OperationResult | None:
    """Update an existing issue."""
    # If trying to close via update, redirect to close_issue to preserve approval workflow
    if status == "closed":
        issues = await beads_close_issue(issue_id=issue_id, reason="Closed via update")
        if not verbose:
            return OperationResult(id=issue_id, action="closed")
        return issues[0] if issues else None

    issue = await beads_update_issue(
        issue_id=issue_id,
        status=status,
        priority=priority,
        assignee=assignee,
        title=title,
        description=description,
        design=design,
        acceptance_criteria=acceptance_criteria,
        notes=notes,
        external_ref=external_ref,
        add_labels=add_labels,
        remove_labels=remove_labels,
        estimated_minutes=estimated_minutes,
    )
    if verbose:
        return issue
    return OperationResult(id=issue_id, action="updated")


@mcp.tool(
    name="close",
    description="""Close (complete) an issue. Mark work as done when you've finished implementing/fixing it.
Returns brief confirmation by default; use verbose=True for full Issue.
Use suggest_next=True to see issues unblocked by this close (level 1 dependents only).""",
)
@with_workspace
@require_context
async def close_issue(
    issue_id: str,
    reason: str = "Completed",
    verbose: bool = False,
    suggest_next: bool = False,
    workspace_root: str | None = None,
) -> list[Issue] | OperationResult:
    """Close (complete) an issue."""
    # Get dependents BEFORE closing if suggest_next requested
    dependents = []
    if suggest_next:
        issue = await beads_show_issue(issue_id=issue_id)
        dependents = issue.dependents

    # Close the issue
    issues = await beads_close_issue(issue_id=issue_id, reason=reason)

    if verbose:
        return issues

    result = OperationResult(id=issue_id, action="closed")

    # Check which dependents are now unblocked
    if suggest_next and dependents:
        unblocked = []
        for dep in dependents:
            if dep.dependency_type == "blocks":
                # Get fresh state of dependent
                dep_issue = await beads_show_issue(issue_id=dep.id)
                # Check if all its blockers are now closed
                all_blockers_closed = all(
                    d.status == "closed"
                    for d in dep_issue.dependencies
                    if d.dependency_type == "blocks"
                )
                if all_blockers_closed and dep_issue.status == "open":
                    unblocked.append({"id": dep.id, "title": dep.title})

        if unblocked:
            result.message = f"Unblocked: {unblocked}"

    return result


@mcp.tool(
    name="reopen",
    description="""Reopen one or more closed issues. Sets status to 'open' and clears closed_at timestamp.
Returns brief confirmation by default; use verbose=True for full Issue list.""",
)
@with_workspace
@require_context
async def reopen_issue(
    issue_ids: list[str],
    reason: str | None = None,
    verbose: bool = False,
    workspace_root: str | None = None,
) -> list[Issue] | OperationResult:
    """Reopen one or more closed issues."""
    issues = await beads_reopen_issue(issue_ids=issue_ids, reason=reason)
    if verbose:
        return issues
    ids = ", ".join(issue_ids)
    return OperationResult(id=ids, action="reopened", message=f"{len(issues)} issue(s)")


@mcp.tool(
    name="dep",
    description="""Add a dependency between issues. Types: blocks (hard blocker),
related (soft link), parent-child (epic/subtask), discovered-from (found during work).
Returns brief confirmation by default; use verbose=True for full message.""",
)
@with_workspace
@require_context
async def add_dependency(
    issue_id: str,
    depends_on_id: str,
    dep_type: DependencyType = "blocks",
    verbose: bool = False,
    workspace_root: str | None = None,
) -> str | OperationResult:
    """Add a dependency relationship between two issues."""
    result = await beads_add_dependency(
        issue_id=issue_id,
        depends_on_id=depends_on_id,
        dep_type=dep_type,
    )
    if verbose:
        return result
    return OperationResult(id=f"{issue_id}->{depends_on_id}", action="dep_added")


@mcp.tool(
    name="dep_remove",
    description="""Remove a dependency between issues.
Returns brief confirmation by default; use verbose=True for full message.""",
)
@with_workspace
@require_context
async def remove_dependency(
    issue_id: str,
    depends_on_id: str,
    dep_type: DependencyType | None = None,
    verbose: bool = False,
    workspace_root: str | None = None,
) -> str | OperationResult:
    """Remove a dependency relationship between two issues."""
    result = await beads_remove_dependency(
        issue_id=issue_id,
        depends_on_id=depends_on_id,
        dep_type=dep_type,
    )
    if verbose:
        return result
    return OperationResult(id=f"{issue_id}->{depends_on_id}", action="dep_removed")


@mcp.tool(
    name="dep_tree",
    description="Get the dependency tree for an issue, showing blockers and dependents.",
)
@with_workspace
async def dep_tree(
    issue_id: str,
    max_depth: int = 3,
    workspace_root: str | None = None,
) -> dict[str, Any]:
    """Get dependency tree for an issue."""
    return await beads_dep_tree(issue_id=issue_id, max_depth=max_depth)


@mcp.tool(
    name="comment_add",
    description="""Add a comment to an issue for tracking progress or decisions.
Returns brief confirmation by default; use verbose=True for full comment.""",
)
@with_workspace
@require_context
async def comment_add(
    issue_id: str,
    text: str,
    author: str | None = None,
    verbose: bool = False,
    workspace_root: str | None = None,
) -> dict[str, Any] | OperationResult:
    """Add a comment to an issue."""
    result = await beads_comment_add(issue_id=issue_id, text=text, author=author)
    if verbose:
        return result
    return OperationResult(id=issue_id, action="comment_added")


@mcp.tool(
    name="comment_list",
    description="List all comments on an issue.",
)
@with_workspace
async def comment_list(
    issue_id: str,
    workspace_root: str | None = None,
) -> list[dict[str, Any]]:
    """List comments on an issue."""
    return await beads_comment_list(issue_id=issue_id)


@mcp.tool(
    name="stats",
    description="Get statistics: total issues, open, in_progress, closed, blocked, ready, and average lead time.",
)
@with_workspace
async def stats(workspace_root: str | None = None) -> Stats:
    """Get statistics about tasks."""
    return await beads_stats()


@mcp.tool(
    name="blocked",
    description="Get blocked issues showing what dependencies are blocking them from being worked on.",
)
@with_workspace
async def blocked(workspace_root: str | None = None) -> list[BlockedIssue]:
    """Get blocked issues."""
    return await beads_blocked()


@mcp.tool(
    name="init",
    description="""Initialize bd in current directory. Creates .beads/ directory and
database with optional custom prefix for issue IDs.""",
)
@with_workspace
@require_context
async def init(prefix: str | None = None, workspace_root: str | None = None) -> str:
    """Initialize bd in current directory."""
    return await beads_init(prefix=prefix)


@mcp.tool(
    name="debug_env",
    description="Debug tool: Show environment and working directory information",
)
@with_workspace
async def debug_env(workspace_root: str | None = None) -> str:
    """Debug tool to check working directory and environment variables."""
    info = []
    info.append("=== Working Directory Debug Info ===\n")
    info.append(f"os.getcwd(): {os.getcwd()}\n")
    info.append(f"PWD env var: {os.environ.get('PWD', 'NOT SET')}\n")
    info.append(f"BEADS_WORKING_DIR env var: {os.environ.get('BEADS_WORKING_DIR', 'NOT SET')}\n")
    info.append(f"BEADS_PATH env var: {os.environ.get('BEADS_PATH', 'NOT SET')}\n")
    info.append(f"BEADS_DB env var: {os.environ.get('BEADS_DB', 'NOT SET')}\n")
    info.append(f"HOME: {os.environ.get('HOME', 'NOT SET')}\n")
    info.append(f"USER: {os.environ.get('USER', 'NOT SET')}\n")
    info.append("\n=== All Environment Variables ===\n")
    for key, value in sorted(os.environ.items()):
        if not key.startswith("_"):  # Skip internal vars
            info.append(f"{key}={value}\n")
    return "".join(info)


@mcp.tool(
    name="inspect_migration",
    description="Get migration plan and database state for agent analysis.",
)
@with_workspace
async def inspect_migration(workspace_root: str | None = None) -> dict[str, Any]:
    """Get migration plan and database state for agent analysis.
    
    AI agents should:
    1. Review registered_migrations to understand what will run
    2. Check warnings array for issues (missing config, version mismatch)
    3. Verify missing_config is empty before migrating
    4. Check invariants_to_check to understand safety guarantees
    
    Returns migration plan, current db state, warnings, and invariants.
    """
    return await beads_inspect_migration()


@mcp.tool(
    name="get_schema_info",
    description="Get current database schema for inspection.",
)
@with_workspace
async def get_schema_info(workspace_root: str | None = None) -> dict[str, Any]:
    """Get current database schema for inspection.
    
    Returns tables, schema version, config, sample issue IDs, and detected prefix.
    Useful for verifying database state before migrations.
    """
    return await beads_get_schema_info()


@mcp.tool(
    name="repair_deps",
    description="Find and optionally fix orphaned dependency references.",
)
@with_workspace
async def repair_deps(fix: bool = False, workspace_root: str | None = None) -> dict[str, Any]:
    """Find and optionally fix orphaned dependency references.
    
    Scans all issues for dependencies pointing to non-existent issues.
    Returns orphaned dependencies and optionally removes them with fix=True.
    """
    return await beads_repair_deps(fix=fix)


@mcp.tool(
    name="detect_pollution",
    description="Detect test issues that leaked into production database.",
)
@with_workspace
async def detect_pollution(clean: bool = False, workspace_root: str | None = None) -> dict[str, Any]:
    """Detect test issues that leaked into production database.
    
    Detects test issues using pattern matching (titles starting with 'test', etc.).
    Returns detected test issues and optionally deletes them with clean=True.
    """
    return await beads_detect_pollution(clean=clean)


@mcp.tool(
    name="validate",
    description="Run comprehensive database health checks.",
)
@with_workspace
async def validate(
    checks: str | None = None,
    fix_all: bool = False,
    workspace_root: str | None = None,
) -> dict[str, Any]:
    """Run comprehensive database health checks.
    
    Available checks: orphans, duplicates, pollution, conflicts.
    If checks is None, runs all checks.
    Returns validation results for each check.
    """
    return await beads_validate(checks=checks, fix_all=fix_all)


async def async_main() -> None:
    """Async entry point for the MCP server."""
    await mcp.run_async(transport="stdio")


def main() -> None:
    """Entry point for the MCP server."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
