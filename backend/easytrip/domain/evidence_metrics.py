"""Primary coverage for source-validated evidence; attitude votes remain separate."""

UNRESOLVED = frozenset(
    {
        "unknown",
        "unresolved",
        "n/a",
        "none",
        "null",
        "未知",
        "未知对象",
        "未知维度",
        "未知地点",
        "无法判断",
        "不明确",
        "未判断",
        "不确定",
    }
)


def object_dimension_judged(item: dict) -> bool:
    """Count explicit text objects/dimensions even when nature or stance is unknown."""
    return (
        item.get("citation_kind", "text") == "text"
        and item.get("link_status") in ("linked", "unlinked")
        and all(
            isinstance(value, str)
            and bool(value.strip())
            and value.strip().casefold() not in UNRESOLVED
            for value in (item.get("object_text"), item.get("dimension"))
        )
    )
