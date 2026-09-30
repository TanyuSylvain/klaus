"""Match CCH users against the tags listed in config."""

from typing import Any


def user_tag_names(user: dict[str, Any]) -> set[str]:
    raw = user.get("tags")
    if not isinstance(raw, list):
        return set()
    names: set[str] = set()
    for tag in raw:
        if isinstance(tag, str) and tag.strip():
            names.add(tag.strip())
    return names


def user_matches_tags(user: dict[str, Any], required: tuple[str, ...]) -> bool:
    """True when no tags are required, or the user has at least one of them."""
    if not required:
        return True
    return bool(user_tag_names(user) & set(required))


def allowed_user_ids(users: list[dict[str, Any]], required: tuple[str, ...]) -> set[int]:
    allowed: set[int] = set()
    for user in users:
        user_id = user.get("id")
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            continue
        if user_matches_tags(user, required):
            allowed.add(user_id)
    return allowed
