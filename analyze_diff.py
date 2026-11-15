"""Compare the old (v2.8.1) and new (v2.8.2) hospital constraint packages."""
import importlib

OLD = importlib.import_module("hl7apy.v2_8_1")
NEW = importlib.import_module("hl7apy.v2_8_2")


def card_max(card):
    return card[1]


def card_min(card):
    return card[0]


# ---------------------------------------------------------------- FIELDS
def compare_fields():
    old, new = OLD.FIELDS, NEW.FIELDS
    ok, remap, incomp, display_only, added, removed = [], [], [], [], [], []
    for key in sorted(set(old) | set(new)):
        if key in old and key not in new:
            removed.append((key, old[key]))
            continue
        if key in new and key not in old:
            added.append((key, new[key]))
            continue
        o, n = old[key], new[key]
        # tuple: (struct_type, struct_ref, datatype, display_name, table, length)
        o_dt, o_name, o_tab, o_len = o[2], o[3], o[4], o[5]
        n_dt, n_name, n_tab, n_len = n[2], n[3], n[4], n[5]
        reasons = []
        if o_dt != n_dt:
            reasons.append(f"datatype {o_dt}->{n_dt}")
        if o_tab != n_tab:
            reasons.append(f"table {o_tab}->{n_tab}")
        # length: -1 means unbounded
        if o_len != n_len:
            if n_len == -1:
                reasons.append(f"length {o_len}->unbounded(relaxed)")
            elif o_len == -1:
                reasons.append(f"length unbounded->{n_len}(TIGHTENED)")
            elif n_len < o_len:
                reasons.append(f"length {o_len}->{n_len}(TIGHTENED)")
            else:
                reasons.append(f"length {o_len}->{n_len}(relaxed)")
        name_changed = o_name != n_name
        if reasons:
            row = (key, o_name, "; ".join(reasons))
            # hard incompatibilities vs remappable
            hard = any("TIGHTENED" in r for r in reasons) or \
                any(r.startswith("table ") and o_tab is not None for r in reasons) or \
                any(r.startswith("datatype ") for r in reasons)
            (remap if hard else ok).append(row)
        elif name_changed:
            display_only.append((key, o_name, n_name))
        else:
            ok.append((key, o_name, "identical"))
    return dict(ok=ok, remap=remap, incomp=incomp, display_only=display_only,
                added=added, removed=removed)


# ---------------------------------------------------------------- SEGMENTS
def compare_segments():
    old, new = OLD.SEGMENTS, NEW.SEGMENTS
    added, removed, changed = [], [], []
    for key in sorted(set(old) | set(new)):
        if key in old and key not in new:
            removed.append(key)
            continue
        if key in new and key not in old:
            added.append(key)
            continue
        o_children = old[key][1]
        n_children = new[key][1]
        o_map = {c[0]: c for c in o_children}
        n_map = {c[0]: c for c in n_children}
        seg_changes = []
        for fk in sorted(set(o_map) | set(n_map)):
            if fk in o_map and fk not in n_map:
                seg_changes.append((fk, "field removed from segment",
                                    o_map[fk][2], None))
            elif fk in n_map and fk not in o_map:
                seg_changes.append((fk, "field added to segment",
                                    None, n_map[fk][2]))
            else:
                oc, nc = o_map[fk][2], n_map[fk][2]
                if oc != nc:
                    kind = []
                    if card_max(oc) != card_max(nc):
                        direction = "TIGHTENED" if (card_max(nc) != -1 and
                            (card_max(oc) == -1 or card_max(nc) < card_max(oc))) else "relaxed"
                        kind.append(f"repetition max {card_max(oc)}->{card_max(nc)} ({direction})")
                    if card_min(oc) != card_min(nc):
                        kind.append(f"min {card_min(oc)}->{card_min(nc)}")
                    seg_changes.append((fk, "; ".join(kind), oc, nc))
        if seg_changes:
            changed.append((key, seg_changes))
    return added, removed, changed


# ---------------------------------------------------------------- GROUPS + MESSAGES (hierarchy)
def hierarchy(label, old_dict, new_dict):
    added, removed, changed = [], [], []
    for key in sorted(set(old_dict) | set(new_dict)):
        if key in old_dict and key not in new_dict:
            removed.append(key)
            continue
        if key in new_dict and key not in old_dict:
            added.append(key)
            continue
        o_children = [(c[0], c[2], c[3]) for c in old_dict[key][1]]
        n_children = [(c[0], c[2], c[3]) for c in new_dict[key][1]]
        if o_children != n_children:
            o_names = [c[0] for c in o_children]
            n_names = [c[0] for c in n_children]
            details = []
            for name in sorted(set(o_names) | set(n_names)):
                oc = next((c for c in o_children if c[0] == name), None)
                nc = next((c for c in n_children if c[0] == name), None)
                if oc is None:
                    details.append(f"{name}: added {nc[1]} {nc[2]}")
                elif nc is None:
                    details.append(f"{name}: REMOVED (was {oc[1]} {oc[2]})")
                elif oc[1] != nc[1]:
                    details.append(f"{name}: cardinality {oc[1]}->{nc[1]}")
            if o_names != n_names and not any("REMOVED" in d or "added" in d for d in details):
                details.append("child order changed")
            changed.append((key, details))
    return added, removed, changed


# ---------------------------------------------------------------- TABLES
def compare_tables():
    old, new = OLD.TABLES, NEW.TABLES
    added, removed, changed, name_only = [], [], [], []
    for key in sorted(set(old) | set(new)):
        if key in old and key not in new:
            removed.append((key, old[key][0]))
            continue
        if key in new and key not in old:
            added.append((key, new[key][0], list(new[key][1])))
            continue
        o_name, o_codes = old[key]
        n_name, n_codes = new[key]
        o_set, n_set = set(o_codes), set(n_codes)
        removed_codes = sorted(o_set - n_set)
        added_codes = sorted(n_set - o_set)
        if removed_codes or added_codes:
            changed.append((key, o_name, added_codes, removed_codes))
        elif o_name != n_name:
            name_only.append((key, o_name, n_name))
    return added, removed, changed, name_only


# ---------------------------------------------------------------- DATATYPE STRUCTS
def compare_structs():
    old, new = OLD.DATATYPES_STRUCTS, NEW.DATATYPES_STRUCTS
    added, removed, changed = [], [], []
    for key in sorted(set(old) | set(new)):
        if key in old and key not in new:
            removed.append(key)
        elif key in new and key not in old:
            added.append(key)
        elif old[key] != new[key]:
            changed.append(key)
    return added, removed, changed


def main():
    print("=" * 70)
    print("FIELD-LEVEL DIFF (FIELDS dict: position -> datatype/table/length)")
    print("=" * 70)
    f = compare_fields()
    print(f"identical/relaxed (continue receiving): {len(f['ok'])}")
    print(f"display-name-only changes (NOT a data change): {len(f['display_only'])}")
    print(f"need remap (datatype/table/tightened length): {len(f['remap'])}")
    print(f"newly added fields (unknown-extension rule applies): {len(f['added'])}")
    print(f"removed fields (incompatible): {len(f['removed'])}")
    print()
    print("-- display-name-only samples --")
    for row in f["display_only"][:15]:
        print("  ", row)
    print("-- remap rows --")
    for row in f["remap"]:
        print("  ", row)
    print("-- removed fields --")
    for row in f["removed"]:
        print("  ", row[0], row[1][3], row[1][2], row[1][4])
    print("-- added fields --")
    for row in f["added"]:
        print("  ", row[0], row[1][3], row[1][2], row[1][4], "len=", row[1][5])

    print()
    print("=" * 70)
    print("SEGMENT-LEVEL DIFF (repetition limits / field membership)")
    print("=" * 70)
    sa, sr, sc = compare_segments()
    print("segments added:", sa)
    print("segments removed:", sr)
    print(f"segments with changes: {len(sc)}")
    for name, changes in sc:
        print(f"  [{name}]")
        for c in changes:
            print("    ", c)

    print()
    print("=" * 70)
    print("GROUP HIERARCHY DIFF")
    print("=" * 70)
    ga, gr, gc = hierarchy("GROUPS", OLD.GROUPS, NEW.GROUPS)
    print("groups added:", ga)
    print("groups removed:", gr)
    print(f"groups changed: {len(gc)}")
    for name, details in gc:
        print(f"  [{name}] {details}")

    print()
    print("=" * 70)
    print("MESSAGE HIERARCHY DIFF")
    print("=" * 70)
    ma, mr, mc = hierarchy("MESSAGES", OLD.MESSAGES, NEW.MESSAGES)
    print("messages added:", ma)
    print("messages removed:", mr)
    print(f"messages changed: {len(mc)}")
    for name, details in mc:
        print(f"  [{name}] {details}")

    print()
    print("=" * 70)
    print("CODE TABLE DIFF")
    print("=" * 70)
    ta, tr, tc, tn = compare_tables()
    print(f"tables added: {len(ta)}; removed: {len(tr)}; code-set changed: {len(tc)}; name-only: {len(tn)}")
    print("-- tables removed --", tr)
    print("-- tables added --")
    for t in ta:
        print("  ", t[0], t[1], "codes=", t[2])
    print("-- code-set changes (added codes / removed codes) --")
    for key, name, ac, rc in tc:
        print(f"  {key} {name}: +{ac}  -{rc}")
    print("-- table name-only --", tn[:20])

    print()
    print("=" * 70)
    print("DATATYPE STRUCT DIFF")
    print("=" * 70)
    da, dr, dc = compare_structs()
    print("structs added:", da)
    print("structs removed:", dr)
    print("structs changed:", dc)


if __name__ == "__main__":
    main()
