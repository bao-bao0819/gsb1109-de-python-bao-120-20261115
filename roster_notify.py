#!/usr/bin/env python3
"""Print merged roster-change notifications for the people actually affected."""

from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from dateutil.tz import gettz

from icalevents.rosterdiff import (
    ORGANIZER_ROLE,
    Change,
    Person,
    PersonNotification,
    build_notifications,
    normalize_address,
    parse_roster,
)

ROLE_LABELS = {
    ORGANIZER_ROLE: "组织者",
    "REQ-PARTICIPANT": "必到值班员",
    "OPT-PARTICIPANT": "可选值班员",
    "CHAIR": "主持",
}


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role.lower())


def parse_window(value: str) -> datetime:
    return datetime.fromisoformat(value)


def fmt_moment(value: datetime, zone: ZoneInfo) -> str:
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def fmt_slot(change: Change, zone: ZoneInfo) -> str:
    start = change.new_start if change.new_start is not None else change.old_start
    end = change.new_end if change.new_end is not None else change.old_end
    if start is None:
        return ""
    span = f"{fmt_moment(start, zone)}-{fmt_moment(end, zone).split(' ')[1]}"
    return span


def build_name_registry(
    old_content: str, new_content: str, start, end, reference
) -> dict[str, str]:
    names: dict[str, str] = {}
    for content in (old_content, new_content):
        slots = parse_roster(content, start, end, reference)
        for slot in slots.values():
            for participation in slot.participations:
                if participation.person.name:
                    names[participation.person.key] = participation.person.name
    return names


def delegate_label(address: str, names: dict[str, str]) -> str:
    key = normalize_address(address)
    name = names.get(key)
    return f"{name} <{key}>" if name else key


def delegation_suffix(change: Change, names: dict[str, str]) -> str:
    parts = []
    if change.delegates_to:
        targets = "、".join(
            delegate_label(item, names) for item in sorted(change.delegates_to)
        )
        parts.append(f"委派给 {targets}")
    if change.delegates_from:
        sources = "、".join(
            delegate_label(item, names) for item in sorted(change.delegates_from)
        )
        parts.append(f"受 {sources} 委派")
    return f"（{'，'.join(parts)}）" if parts else ""


def person_title(person: Person) -> str:
    if not person.identified:
        label = person.name or "未定身份"
        return f"{label}（未定身份，地址 {person.address}，不与任何人合并）"
    if person.name:
        return f"{person.name} <{person.key}>"
    return person.key


def print_change(change: Change, zone: ZoneInfo, names: dict[str, str]) -> str:
    span = fmt_slot(change, zone)
    summary = change.summary or change.old_summary or "(无主题)"
    return (
        f"  - {span} {summary} [{role_label(change.role)}]"
        f"{delegation_suffix(change, names)}"
    )


def print_notification(
    notification: PersonNotification, zone: ZoneInfo, names: dict[str, str]
) -> list[str]:
    lines = [person_title(notification.person)]
    if notification.added:
        lines.append("  新增：")
        lines.extend(
            print_change(change, zone, names) for change in notification.added
        )
    if notification.removed:
        lines.append("  取消：")
        lines.extend(
            print_change(change, zone, names) for change in notification.removed
        )
    if notification.time_changed:
        lines.append("  时间变化：")
        for change in notification.time_changed:
            lines.append(
                f"  - {change.old_summary or change.summary} "
                f"[{role_label(change.role)}] "
                f"{fmt_moment(change.old_start, zone)}-"
                f"{fmt_moment(change.old_end, zone).split(' ')[1]} → "
                f"{fmt_moment(change.new_start, zone)}-"
                f"{fmt_moment(change.new_end, zone).split(' ')[1]}"
                f"{delegation_suffix(change, names)}"
            )
    if notification.summary_only:
        lines.append("  仅主题改动（单独口径，时间/人员未变）：")
        for change in notification.summary_only:
            lines.append(
                f"  - {fmt_slot(change, zone)} "
                f"“{change.old_summary}” → “{change.new_summary}” "
                f"[{role_label(change.role)}]"
            )
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--start", type=parse_window, required=True)
    parser.add_argument("--end", type=parse_window, required=True)
    parser.add_argument("--tz", default="Asia/Shanghai")
    parser.add_argument(
        "--include-organizer",
        action="store_true",
        help="also emit organizer-role changes (excluded by default)",
    )
    args = parser.parse_args()

    zone = ZoneInfo(args.tz)
    reference = gettz(args.tz)
    with open(args.old, encoding="utf-8") as handle:
        old_content = handle.read()
    with open(args.new, encoding="utf-8") as handle:
        new_content = handle.read()

    notifications = build_notifications(
        old_content,
        new_content,
        args.start,
        args.end,
        reference,
        include_organizer=args.include_organizer,
    )
    names = build_name_registry(
        old_content, new_content, args.start, args.end, reference
    )

    print(
        f"窗口 {args.start.isoformat()} ~ {args.end.isoformat()}，"
        f"共 {len(notifications)} 名受影响人员/未定身份："
    )
    for notification in notifications:
        for line in print_notification(notification, zone, names):
            print(line)
        print()


if __name__ == "__main__":
    main()
