from collections import OrderedDict

from app.models import ScannedImage


def group_exact_duplicates(items: list[ScannedImage]) -> list[list[ScannedImage]]:
    groups: OrderedDict[str, list[ScannedImage]] = OrderedDict()
    for item in items:
        groups.setdefault(item.sha256, []).append(item)
    return list(groups.values())
