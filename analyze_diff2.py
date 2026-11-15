"""Deeper diff: datatype components (length/datatype/table) and table whitespace normalization."""
import importlib

OLD = importlib.import_module("hl7apy.v2_8_1")
NEW = importlib.import_module("hl7apy.v2_8_2")


def diff_components():
    old, new = OLD.DATATYPES, NEW.DATATYPES
    added, removed, changed = [], [], []
    for key in sorted(set(old) | set(new)):
        if key in old and key not in new:
            removed.append((key, old[key]))
        elif key in new and key not in old:
            added.append((key, new[key]))
        elif old[key] != new[key]:
            changed.append((key, old[key], new[key]))
    return added, removed, changed


def norm(code):
    return code.strip() if isinstance(code, str) else code


def table_real_diff():
    """Separate whitespace-only normalization from true code add/remove."""
    old, new = OLD.TABLES, NEW.TABLES
    true_removed, true_added, ws_only = [], [], []
    for key in sorted(set(old) | set(new)):
        if key not in old or key not in new:
            continue
        o_codes, n_codes = list(old[key][1]), list(new[key][1])
        o_norm = {norm(c) for c in o_codes}
        n_norm = {norm(c) for c in n_codes}
        real_minus = sorted(o_norm - n_norm)
        real_plus = sorted(n_norm - o_norm)
        raw_minus = sorted(set(o_codes) - set(n_codes))
        if real_minus or real_plus:
            true_removed.append((key, old[key][0], real_minus, real_plus))
        elif raw_minus:
            ws_only.append((key, raw_minus[:4], len(raw_minus)))
    return true_removed, ws_only


def main():
    print("=" * 70)
    print("DATATYPE COMPONENT (leaf) DIFF")
    print("=" * 70)
    added, removed, changed = diff_components()
    print(f"components added: {len(added)}; removed: {len(removed)}; changed: {len(changed)}")
    print("-- added components --")
    for k, v in added:
        print("  ", k, v)
    print("-- removed components --")
    for k, v in removed:
        print("  ", k, v)
    print("-- changed components (old -> new) --")
    for k, o, n in changed:
        print(f"  {k}: {o} -> {n}")

    print()
    print("=" * 70)
    print("TABLE: TRUE code changes after whitespace normalization")
    print("=" * 70)
    real, ws = table_real_diff()
    print(f"tables with REAL code changes: {len(real)}; whitespace-only tables: {len(ws)}")
    print("-- real code removals/additions --")
    for key, name, minus, plus in real:
        print(f"  {key} {name}:\n      removed={minus}\n      added={plus}")
    print("-- whitespace-normalized only (not semantic) --")
    for key, sample, n in ws:
        print(f"  {key}: {n} codes trimmed, e.g. {sample}")


if __name__ == "__main__":
    main()
