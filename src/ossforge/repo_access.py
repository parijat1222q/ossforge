"""Current repository PR-creation policy, separate from health and user authority."""
from . import gh


def pr_restriction(repo: str) -> str:
    info = gh.api(f"repos/{repo}", fresh=True)
    if not isinstance(info, dict):
        raise gh.GhError("repository PR-creation policy unavailable")
    rule = info.get("pull_request_creation_policy")
    if rule is None or rule == "all":
        return ""  # older GitHub responses omit this optional capability field
    if rule != "collaborators_only":
        return "unknown repository PR-creation policy; review before contribution"
    permissions = info.get("permissions") or {}
    if not isinstance(permissions, dict):
        return "collaborator access unavailable; review before contribution"
    if any(permissions.get(k) is True for k in ("push", "maintain", "admin")):
        return ""
    return "repository allows PRs from collaborators only; current account lacks confirmed collaborator access"
