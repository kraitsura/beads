"""FastMCP server for beads issue tracker.

Context Engineering Optimizations (v0.24.0):
- Lazy tool schema loading via discover_tools() and get_tool_info()
- Minimal issue models for list views (~80% context reduction)
- Result compaction for large queries (>20 issues)
- On-demand full details via show() command

These optimizations reduce context window usage from ~10-50k tokens to ~2-5k tokens,
enabling more efficient agent operation without sacrificing functionality.
"""

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

from beads_mcp.models import (
    BlockedIssue,
    BriefDep,
    BriefIssue,
    BriefTreeNode,
    CompactedResult,
    DependencyType,
    Issue,
    IssueMinimal,
    IssueStatus,
    IssueType,
    OperationResult,
    Stats,
)
from beads_mcp.tools import (
    beads_add_dependency,
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

# =============================================================================
# CONTEXT ENGINEERING: Compaction Settings (Configurable via Environment)
# =============================================================================
# These settings control how large result sets are compacted to prevent context overflow.
# Override via environment variables:
#   BEADS_MCP_COMPACTION_THRESHOLD - Compact results with >N issues (default: 20)
#   BEADS_MCP_PREVIEW_COUNT - Show first N issues in preview (default: 5)

def _get_compaction_settings() -> tuple[int, int]:
    """Load compaction settings from environment or use defaults.
    
    Returns:
        (threshold, preview_count) tuple
    """
    import os
    
    threshold = int(os.environ.get("BEADS_MCP_COMPACTION_THRESHOLD", "20"))
    preview_count = int(os.environ.get("BEADS_MCP_PREVIEW_COUNT", "5"))
    
    # Validate settings
    if threshold < 1:
        raise ValueError("BEADS_MCP_COMPACTION_THRESHOLD must be >= 1")
    if preview_count < 1:
        raise ValueError("BEADS_MCP_PREVIEW_COUNT must be >= 1")
    if preview_count > threshold:
        raise ValueError("BEADS_MCP_PREVIEW_COUNT must be <= BEADS_MCP_COMPACTION_THRESHOLD")
    
    return threshold, preview_count


COMPACTION_THRESHOLD, PREVIEW_COUNT = _get_compaction_settings()

if os.environ.get("BEADS_MCP_COMPACTION_THRESHOLD"):
    logger.info(f"Using BEADS_MCP_COMPACTION_THRESHOLD={COMPACTION_THRESHOLD}")
if os.environ.get("BEADS_MCP_PREVIEW_COUNT"):
    logger.info(f"Using BEADS_MCP_PREVIEW_COUNT={PREVIEW_COUNT}")

# Create FastMCP server
mcp = FastMCP(
    name="Beads",
    instructions="""
We track work in Beads (bd) instead of Markdown.
Check the resource beads://quickstart to see how.

IMPORTANT: Call context(action="set", workspace_root="...") before write operations.

## Quick Reference (11 tools)

| Action | Tool | Example |
|--------|------|---------|
| Find work | ready(brief=True) | Scan available tasks |
| List/search | list(query="auth", brief=True) | Search issues |
| Blocked issues | list(status="blocked") | Shows blocked_by info |
| Issue detail | show(id, fields=["id","dependencies"]) | Check specific issue |
| Create | create(title="...") | New issue |
| Update | update(id, status="in_progress") | Claim work |
| Close/reopen | close(id) / close(action="reopen", issue_ids=[...]) | Complete/reopen |
| Dependencies | dep(action="add|remove|tree", ...) | Manage deps |
| Comments | comment(action="add|list", ...) | Track progress |
| Statistics | stats() | Project overview |
| Admin | admin(action="validate|repair|...", ...) | Diagnostics |
| Context | context(action="set|show|init", ...) | Workspace setup |

## Token Optimization

- `brief=True`: Returns only {id, title, status} - use when scanning
- `fields=["id", "dependencies"]`: Returns only specific fields
- `max_description_length=100`: Truncates long descriptions
- Write ops return minimal confirmation by default (use `brief=False` for full)
- `dep(action="tree", brief=True)`: Compact dependency tree

## Filtering Issues

- `labels=["bug"]`: Issues with ALL specified labels (AND)
- `labels_any=["p0", "p1"]`: Issues with ANY specified label (OR)
- `query="search term"`: Search in title/description
- `unassigned=True`: Issues with no assignee
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


# =============================================================================
# CONTEXT ENGINEERING: Tool Discovery (Lazy Schema Loading)
# =============================================================================
# These tools enable agents to discover available tools without loading full schemas.
# This reduces initial context from ~10-50k tokens to ~500 bytes.

# Tool metadata for discovery (lightweight - just names and brief descriptions)
_TOOL_CATALOG = {
    "ready": "Find tasks ready to work on (no blockers)",
    "list": "List issues with filters (status, priority, type)",
    "show": "Show full details for a specific issue",
    "create": "Create a new issue (bug, feature, task, epic)",
    "update": "Update issue status, priority, or assignee",
    "close": "Close or reopen issues (action='close' or 'reopen')",
    "dep": "Manage dependencies (action='add', 'remove', or 'tree')",
    "comment": "Manage comments (action='add' or 'list')",
    "stats": "Get issue statistics",
    "blocked": "Show blocked issues and what blocks them",
    "context": "Manage workspace context (set, show, init)",
    "admin": "Administrative/diagnostic operations",
    "discover_tools": "List available tools (names only)",
    "get_tool_info": "Get detailed info for a specific tool",
}


@mcp.tool(
    name="discover_tools",
    description="List available beads tools (names and brief descriptions only). Use get_tool_info() for full details.",
)
async def discover_tools() -> dict[str, Any]:
    """Discover available beads tools without loading full schemas.
    
    Returns lightweight tool catalog to minimize context usage.
    Use get_tool_info(tool_name) for full parameter details.
    
    Context savings: ~500 bytes vs ~10-50k for full schemas.
    """
    return {
        "tools": _TOOL_CATALOG,
        "count": len(_TOOL_CATALOG),
        "hint": "Use get_tool_info('tool_name') for full parameters and usage"
    }


@mcp.tool(
    name="get_tool_info",
    description="Get detailed information about a specific beads tool including parameters.",
)
async def get_tool_info(tool_name: str) -> dict[str, Any]:
    """Get detailed info for a specific tool.
    
    Args:
        tool_name: Name of the tool to get info for
        
    Returns:
        Full tool details including parameters and usage examples
    """
    tool_details = {
        "ready": {
            "name": "ready",
            "description": "Find tasks with no blockers, ready to work on",
            "parameters": {
                "limit": "int (1-100, default 10) - Max issues to return",
                "priority": "int (0-4, optional) - Filter by priority",
                "assignee": "str (optional) - Filter by assignee",
                "workspace_root": "str (optional) - Workspace path"
            },
            "returns": "List of ready issues (minimal format for context efficiency)",
            "example": "ready(limit=5, priority=1)"
        },
        "list": {
            "name": "list",
            "description": "List all issues with optional filters",
            "parameters": {
                "status": "open|in_progress|blocked|closed (optional)",
                "priority": "int 0-4 (optional)",
                "issue_type": "bug|feature|task|epic|chore (optional)",
                "assignee": "str (optional)",
                "limit": "int (1-100, default 20)",
                "workspace_root": "str (optional)"
            },
            "returns": "List of issues (compacted if >20 results)",
            "example": "list(status='open', priority=1, limit=10)"
        },
        "show": {
            "name": "show",
            "description": "Show full details for a specific issue including dependencies",
            "parameters": {
                "issue_id": "str (required) - e.g., 'bd-a1b2'",
                "workspace_root": "str (optional)"
            },
            "returns": "Full Issue object with dependencies and dependents",
            "example": "show(issue_id='bd-a1b2')"
        },
        "create": {
            "name": "create",
            "description": "Create a new issue",
            "parameters": {
                "title": "str (required)",
                "description": "str (default '')",
                "priority": "int 0-4 (default 2)",
                "issue_type": "bug|feature|task|epic|chore (default task)",
                "assignee": "str (optional)",
                "labels": "list[str] (optional)",
                "deps": "list[str] (optional) - dependency IDs",
                "brief": "bool (default true) - Return OperationResult instead of full Issue",
                "workspace_root": "str (optional)"
            },
            "returns": "OperationResult {id, action} or full Issue if brief=False",
            "example": "create(title='Fix auth bug', priority=1, issue_type='bug')"
        },
        "update": {
            "name": "update",
            "description": "Update an existing issue",
            "parameters": {
                "issue_id": "str (required)",
                "status": "open|in_progress|blocked|deferred|closed (optional)",
                "priority": "int 0-4 (optional)",
                "assignee": "str (optional)",
                "title": "str (optional)",
                "description": "str (optional)",
                "brief": "bool (default true) - Return OperationResult instead of full Issue",
                "workspace_root": "str (optional)"
            },
            "returns": "OperationResult {id, action} or full Issue if brief=False",
            "example": "update(issue_id='bd-a1b2', status='in_progress')"
        },
        "close": {
            "name": "close",
            "description": "Close or reopen issues (action='close' or 'reopen')",
            "parameters": {
                "issue_id": "str (required for close)",
                "issue_ids": "list[str] (for reopen multiple)",
                "action": "str (default 'close') - 'close' or 'reopen'",
                "reason": "str (default 'Completed')",
                "brief": "bool (default true) - Return OperationResult instead of full Issue",
                "workspace_root": "str (optional)"
            },
            "returns": "OperationResult or full Issue if brief=False",
            "example": "close(issue_id='bd-a1b2', reason='Fixed in PR #123')"
        },
        "dep": {
            "name": "dep",
            "description": "Manage dependencies (action='add', 'remove', or 'tree')",
            "parameters": {
                "action": "str (required) - 'add', 'remove', or 'tree'",
                "issue_id": "str (required) - Issue that has the dependency",
                "depends_on_id": "str (required for add/remove) - Issue it depends on",
                "dep_type": "blocks|related|parent-child|discovered-from (default blocks)",
                "max_depth": "int (default 3) - For tree action",
                "reverse": "bool (default true) - For tree: show dependents vs dependencies",
                "brief": "bool (default true) - Return minimal output",
                "workspace_root": "str (optional)"
            },
            "returns": "OperationResult for add/remove, tree structure for tree",
            "example": "dep(action='add', issue_id='bd-f1a2', depends_on_id='bd-a1b2')"
        },
        "stats": {
            "name": "stats",
            "description": "Get issue statistics",
            "parameters": {"workspace_root": "str (optional)"},
            "returns": "Stats object with counts and metrics",
            "example": "stats()"
        },
        "blocked": {
            "name": "blocked",
            "description": "Show blocked issues and what blocks them",
            "parameters": {"workspace_root": "str (optional)"},
            "returns": "List of blocked issues with blocker info",
            "example": "blocked()"
        },
    }
    
    if tool_name not in tool_details:
        available = list(tool_details.keys())
        return {
            "error": f"Unknown tool: {tool_name}",
            "available_tools": available,
            "hint": "Use discover_tools() to see all available tools"
        }
    
    return tool_details[tool_name]


# Context management tools
@mcp.tool(
    name="context",
    description="""Manage workspace context.
Actions:
- set: Set workspace root directory (required before write operations)
- show: Show current workspace context and database path
- init: Initialize new beads database (creates .beads/ directory)""",
)
async def context(
    action: str,  # "set", "show", "init"
    workspace_root: str | None = None,
    prefix: str | None = None,
) -> str:
    """Manage workspace context."""

    if action == "set":
        if not workspace_root:
            raise ValueError("workspace_root required for set action")

        # Resolve to git repo root if possible
        try:
            resolved_root = await asyncio.wait_for(
                asyncio.to_thread(_resolve_workspace_root, workspace_root),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.error(f"Git detection timed out after 5s for: {workspace_root}")
            return (
                f"Error: Git repository detection timed out.\n"
                f"  Provided path: {workspace_root}\n"
                f"  This may indicate a slow filesystem or git configuration issue."
            )

        # Store in persistent context
        _workspace_context["BEADS_WORKING_DIR"] = resolved_root
        _workspace_context["BEADS_CONTEXT_SET"] = "1"
        os.environ["BEADS_WORKING_DIR"] = resolved_root
        os.environ["BEADS_CONTEXT_SET"] = "1"

        # Find beads database
        db_path = _find_beads_db(resolved_root)

        if db_path is None:
            _workspace_context.pop("BEADS_DB", None)
            os.environ.pop("BEADS_DB", None)
            return (
                f"Context set successfully:\n"
                f"  Workspace root: {resolved_root}\n"
                f"  Database: Not found (run context(action='init') to create)"
            )

        _workspace_context["BEADS_DB"] = db_path
        os.environ["BEADS_DB"] = db_path

        return (
            f"Context set successfully:\n"
            f"  Workspace root: {resolved_root}\n"
            f"  Database: {db_path}"
        )

    elif action == "show":
        context_set = (
            _workspace_context.get("BEADS_CONTEXT_SET")
            or os.environ.get("BEADS_CONTEXT_SET")
        )

        if not context_set:
            return (
                "Context not set. Call context(action='set', workspace_root='...') first.\n"
                f"Current process CWD: {os.getcwd()}\n"
                f"BEADS_WORKING_DIR: {_workspace_context.get('BEADS_WORKING_DIR', 'NOT SET')}\n"
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

    elif action == "init":
        return await beads_init(prefix=prefix)

    else:
        raise ValueError(f"Unknown action: {action}. Use 'set', 'show', or 'init'")


# Register all tools
# =============================================================================
# CONTEXT ENGINEERING: Optimized List Tools with Compaction
# =============================================================================

def _to_minimal(issue: Issue) -> IssueMinimal:
    """Convert full Issue to minimal format for context efficiency."""
    return IssueMinimal(
        id=issue.id,
        title=issue.title,
        status=issue.status,
        priority=issue.priority,
        issue_type=issue.issue_type,
        assignee=issue.assignee,
        labels=issue.labels,
        dependency_count=issue.dependency_count,
        dependent_count=issue.dependent_count,
    )


@mcp.tool(name="ready", description="Find tasks that have no blockers and are ready to be worked on. Returns minimal format for context efficiency.")
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
):
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
    description="""List all issues with optional filters. When status='blocked', returns BlockedIssue with blocked_by info.""",
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
):
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
    description="""Show detailed information about a specific issue including dependencies and dependents.

Output modes:
- Default: Full issue with full dependency objects
- brief=True: Just {id, title, status}
- brief_deps=True: Full issue but deps as {id, title, status, dependency_type}
- fields=["id", "dependencies"]: Only specified fields""",
)
@with_workspace
async def show_issue(
    issue_id: str,
    # Output control
    brief: bool = False,
    brief_deps: bool = False,  # True=deps as {id,title,status,dep_type}, False=full Issue objects
    fields: list[str] | None = None,
    max_description_length: int | None = None,
    workspace_root: str | None = None,
):
    """Show detailed information about a specific issue."""
    issue = await beads_show_issue(issue_id=issue_id)

    # Apply output control
    if brief:
        return BriefIssue(id=issue.id, title=issue.title, status=issue.status)

    if fields:
        result = {k: getattr(issue, k, None) for k in fields if hasattr(issue, k)}
        # Apply brief_deps to fields output if dependencies/dependents requested
        if brief_deps:
            if "dependencies" in result and result["dependencies"]:
                result["dependencies"] = [
                    BriefDep(
                        id=d.id, title=d.title, status=d.status,
                        dependency_type=getattr(d, "dependency_type", None)
                    ) for d in result["dependencies"]
                ]
            if "dependents" in result and result["dependents"]:
                result["dependents"] = [
                    BriefDep(
                        id=d.id, title=d.title, status=d.status,
                        dependency_type=getattr(d, "dependency_type", None)
                    ) for d in result["dependents"]
                ]
        return result

    if max_description_length:
        if issue.description and len(issue.description) > max_description_length:
            issue.description = issue.description[:max_description_length] + "..."

    # Convert deps to brief format if requested
    if brief_deps:
        # Convert to dict so we can modify deps
        issue_dict = issue.model_dump()
        if issue_dict.get("dependencies"):
            issue_dict["dependencies"] = [
                {"id": d["id"], "title": d["title"], "status": d["status"],
                 "dependency_type": d.get("dependency_type")}
                for d in issue_dict["dependencies"]
            ]
        if issue_dict.get("dependents"):
            issue_dict["dependents"] = [
                {"id": d["id"], "title": d["title"], "status": d["status"],
                 "dependency_type": d.get("dependency_type")}
                for d in issue_dict["dependents"]
            ]
        return issue_dict

    return issue


@mcp.tool(
    name="create",
    description="""Create a new issue (bug, feature, task, epic, or chore) with optional design,
acceptance criteria, and dependencies. Returns brief confirmation by default; use brief=False for full Issue.""",
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
    brief: bool = True,
    workspace_root: str | None = None,
):
    """Create a new issue.

    Args:
        brief: If True (default), return minimal OperationResult; if False, return full Issue
    """
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
    if not brief:
        return issue
    return OperationResult(id=issue.id, action="created")


@mcp.tool(
    name="update",
    description="""Update an existing issue's status, priority, assignee, description, design notes,
acceptance criteria, labels, or time estimate. Use this to claim work (set status=in_progress).
Returns brief confirmation by default; use brief=False for full Issue.""",
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
    brief: bool = True,
    workspace_root: str | None = None,
):
    """Update an existing issue.

    Args:
        brief: If True (default), return minimal OperationResult; if False, return full Issue
    """
    # If trying to close via update, redirect to close_issue to preserve approval workflow
    if status == "closed":
        issues = await beads_close_issue(issue_id=issue_id, reason="Closed via update")
        if brief:
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
    if not brief:
        return issue
    return OperationResult(id=issue_id, action="updated")


@mcp.tool(
    name="close",
    description="""Close or reopen issues.
- action="close" (default): Mark issue complete.
- action="reopen": Reopen closed issues (use issue_ids for multiple).
Returns brief confirmation by default; use brief=False for full Issue.""",
)
@with_workspace
@require_context
async def close_issue(
    issue_id: str | None = None,
    issue_ids: list[str] | None = None,
    action: str = "close",  # "close" or "reopen"
    reason: str = "Completed",
    brief: bool = True,
    workspace_root: str | None = None,
):
    """Close or reopen issues.

    Args:
        brief: If True (default), return minimal OperationResult; if False, return full Issues
    """

    # Handle reopen action
    if action == "reopen":
        ids_to_reopen = issue_ids or ([issue_id] if issue_id else [])
        if not ids_to_reopen:
            raise ValueError("issue_id or issue_ids required for reopen action")
        issues = await beads_reopen_issue(issue_ids=ids_to_reopen, reason=reason if reason != "Completed" else None)
        if not brief:
            return issues
        ids_str = ", ".join(ids_to_reopen)
        return OperationResult(id=ids_str, action="reopened", message=f"{len(issues)} issue(s)")

    # Handle close action (default)
    if not issue_id:
        raise ValueError("issue_id required for close action")

    # Close the issue
    issues = await beads_close_issue(issue_id=issue_id, reason=reason)

    if not brief:
        return issues

    return OperationResult(id=issue_id, action="closed", message=reason)


@mcp.tool(
    name="dep",
    description="""Manage dependencies between issues.
Actions:
- add: Create dependency (issue depends on depends_on). Types: blocks, related, parent-child, discovered-from
- remove: Remove dependency
- tree: Show dependency tree (use brief=True for minimal output, reverse=True for children)

Examples:
- dep(action="add", issue_id="bd-1", depends_on_id="bd-2")
- dep(action="tree", issue_id="bd-1", brief=True)""",
)
@with_workspace
async def dep(
    action: str,  # "add", "remove", "tree"
    issue_id: str,
    depends_on_id: str | None = None,
    dep_type: DependencyType = "blocks",
    max_depth: int = 3,
    reverse: bool = True,  # True=show dependents (children), False=show dependencies (blockers)
    brief: bool = True,
    workspace_root: str | None = None,
):
    """Manage dependencies between issues.

    Args:
        brief: If True (default), return minimal OperationResult for add/remove, or BriefTreeNode for tree
    """

    if action == "add":
        if not depends_on_id:
            raise ValueError("depends_on_id required for add action")
        result = await beads_add_dependency(
            issue_id=issue_id,
            depends_on_id=depends_on_id,
            dep_type=dep_type,
        )
        if not brief:
            return result
        return OperationResult(id=f"{issue_id}->{depends_on_id}", action="dep_added")

    elif action == "remove":
        if not depends_on_id:
            raise ValueError("depends_on_id required for remove action")
        result = await beads_remove_dependency(
            issue_id=issue_id,
            depends_on_id=depends_on_id,
            dep_type=dep_type if dep_type != "blocks" else None,
        )
        if not brief:
            return result
        return OperationResult(id=f"{issue_id}->{depends_on_id}", action="dep_removed")

    elif action == "tree":
        result = await beads_dep_tree(
            issue_id=issue_id,
            max_depth=max_depth,
            reverse=reverse,
        )
        if brief:
            # Convert to minimal format
            nodes = result.get("nodes", [])
            return [
                BriefTreeNode(
                    id=n["id"],
                    title=n["title"],
                    status=n["status"],
                    depth=n.get("depth", 0),
                    truncated=n.get("truncated", False),
                )
                for n in nodes
            ]
        return result

    else:
        raise ValueError(f"Unknown action: {action}. Use 'add', 'remove', or 'tree'")


@mcp.tool(
    name="comment",
    description="""Manage comments on issues.
Actions:
- add: Add a comment (requires text parameter)
- list: List all comments on an issue""",
)
@with_workspace
async def comment(
    action: str,  # "add" or "list"
    issue_id: str,
    text: str | None = None,
    author: str | None = None,
    brief: bool = True,
    workspace_root: str | None = None,
):
    """Manage comments on issues.

    Args:
        brief: If True (default), return minimal OperationResult for add; if False, return full result
    """

    if action == "add":
        if not text:
            raise ValueError("text required for add action")
        result = await beads_comment_add(issue_id=issue_id, text=text, author=author)
        if not brief:
            return result
        return OperationResult(id=issue_id, action="comment_added")

    elif action == "list":
        return await beads_comment_list(issue_id=issue_id)

    else:
        raise ValueError(f"Unknown action: {action}. Use 'add' or 'list'")


@mcp.tool(
    name="stats",
    description="Get statistics: total issues, open, in_progress, closed, blocked, ready, and average lead time.",
)
@with_workspace
async def stats(workspace_root: str | None = None):
    """Get statistics about tasks."""
    return await beads_stats()


@mcp.tool(
    name="admin",
    description="""Administrative and diagnostic operations.
Actions:
- validate: Run database health checks (checks=orphans,duplicates,pollution,conflicts)
- repair: Fix orphaned dependency references (fix=True to apply)
- schema: Show database schema info
- debug: Show environment and working directory info
- migration: Get migration plan and database state
- pollution: Detect/clean test issues (clean=True to delete)""",
)
@with_workspace
async def admin(
    action: str,  # validate, repair, schema, debug, migration, pollution
    checks: str | None = None,
    fix_all: bool = False,
    fix: bool = False,
    clean: bool = False,
    workspace_root: str | None = None,
):
    """Administrative and diagnostic operations."""

    if action == "validate":
        return await beads_validate(checks=checks, fix_all=fix_all)

    elif action == "repair":
        return await beads_repair_deps(fix=fix)

    elif action == "schema":
        return await beads_get_schema_info()

    elif action == "debug":
        info = []
        info.append("=== Working Directory Debug Info ===\n")
        info.append(f"os.getcwd(): {os.getcwd()}\n")
        info.append(f"PWD env var: {os.environ.get('PWD', 'NOT SET')}\n")
        info.append(f"BEADS_WORKING_DIR env var: {os.environ.get('BEADS_WORKING_DIR', 'NOT SET')}\n")
        info.append(f"BEADS_PATH env var: {os.environ.get('BEADS_PATH', 'NOT SET')}\n")
        info.append(f"BEADS_DB env var: {os.environ.get('BEADS_DB', 'NOT SET')}\n")
        info.append(f"HOME: {os.environ.get('HOME', 'NOT SET')}\n")
        info.append(f"USER: {os.environ.get('USER', 'NOT SET')}\n")
        return "".join(info)

    elif action == "migration":
        return await beads_inspect_migration()

    elif action == "pollution":
        return await beads_detect_pollution(clean=clean)

    else:
        raise ValueError(f"Unknown action: {action}. Use 'validate', 'repair', 'schema', 'debug', 'migration', or 'pollution'")


async def async_main() -> None:
    """Async entry point for the MCP server."""
    await mcp.run_async(transport="stdio")


def main() -> None:
    """Entry point for the MCP server."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
