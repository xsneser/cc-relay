"""Offline long CLI history geometry; output counts, hashes, and aliases only."""
import argparse
import collections
import datetime
import hashlib
import json
import sqlite3
import statistics
from pathlib import Path

from diagnostics.cache_drop_audit import digest, encoded, prefix, rollup, sequence


def ordered_digest(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def geometry(contents):
    counts = collections.Counter()
    sizes = collections.Counter()
    largest = []
    for index, content in enumerate(contents):
        for part in content.get("parts", []):
            kind = ("functionResponse" if "functionResponse" in part else
                    "functionCall" if "functionCall" in part else
                    "inlineData" if "inlineData" in part else
                    "thought" if part.get("thought") else
                    "text" if "text" in part else "other")
            counts[kind] += 1
            sizes[kind] += len(encoded(part))
            if "thoughtSignature" in part:
                counts["signed_parts"] += 1
                sizes["signature_string_bytes"] += len(part["thoughtSignature"].encode())
            largest.append({"content_index": index, "kind": kind, "bytes": len(encoded(part))})
    return {"part_counts": dict(counts), "part_serialized_bytes": dict(sizes),
            "largest_parts": sorted(largest, key=lambda p: p["bytes"], reverse=True)[:8]}


def image_metadata(contents):
    images = [(index, part["inlineData"]) for index, content in enumerate(contents)
              for part in content.get("parts", []) if "inlineData" in part]
    return {"image_count": len(images),
            "first_image_content_index": images[0][0] if images else None,
            "redacted_image_count": sum(str(value.get("data", "")).startswith(
                ("[base64 image:", "[inline data omitted:")) for _, value in images)}


def summarize(rows):
    result = rollup(rows)
    known = [r for r in rows if r["cached"] is not None]
    positive = [r["cached"] for r in known if r["cached"] > 0]
    result["known_field_weighted_hit"] = rollup(known)["weighted_reported_hit"]
    result["positive_cached_median"] = statistics.median(positive) if positive else None
    result["positive_cached_range"] = [min(positive), max(positive)] if positive else None
    return result


def run(database):
    connection = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    groups = collections.defaultdict(list)
    previous = {}
    aliases = collections.defaultdict(dict)
    shapes = {}

    def alias(kind, value):
        if value is None:
            return None
        return aliases[kind].setdefault(value, kind + str(len(aliases[kind]) + 1))

    sql = """SELECT timestamp,model,mapped_model,input_tokens,cached_tokens,
                    account_email,session_id,request_body,upstream_request_body,
                    request_headers FROM request_logs
             WHERE status=200 AND input_tokens>=200000 AND url NOT LIKE '%count_tokens%'
             ORDER BY timestamp"""
    try:
        for record in connection.execute(sql):
            headers = {k.lower(): v for k, v in json.loads(record["request_headers"] or "{}").items()}
            if "external, cli" not in headers.get("user-agent", ""):
                continue
            client = json.loads(record["request_body"] or "{}")
            upstream = json.loads(record["upstream_request_body"] or "{}")
            contents = upstream.get("contents") or []
            messages = client.get("messages") or []
            key = (headers.get("x-claude-code-session-id") or record["session_id"],
                   record["mapped_model"] or record["model"], digest(messages[:1]))
            group = alias("history", key)
            items = sequence(contents)
            ordered = [(ordered_digest(c), n) for c, (_, n) in zip(contents, items)]
            controls = {k: digest(upstream.get(k)) for k in
                        ("systemInstruction", "tools", "generationConfig", "toolConfig", "tool_config", "safetySettings")}
            ordered_controls = {k: ordered_digest(upstream.get(k)) for k in controls}
            row = {"time": datetime.datetime.fromtimestamp(record["timestamp"] / 1000).isoformat(),
                   "input": record["input_tokens"], "cached": record["cached_tokens"],
                   "account": alias("account", record["account_email"]),
                   "session": alias("session", upstream.get("sessionId")),
                   "contents": len(contents), "contents_bytes": sum(n for _, n in items),
                   "controls": controls, "ordered_controls": ordered_controls}
            row.update(image_metadata(contents))
            if group in previous:
                old, old_items, old_ordered = previous[group]
                row["comparison"] = {"prefix": prefix(old_items, items),
                                     "ordered_prefix": prefix(old_ordered, ordered),
                                     "changed_controls": [k for k in controls if controls[k] != old["controls"][k]],
                                     "changed_ordered_controls": [k for k in controls if ordered_controls[k] != old["ordered_controls"][k]],
                                     "account_changed": row["account"] != old["account"],
                                     "session_changed": row["session"] != old["session"]}
            previous[group] = (row, items, ordered)
            groups[group].append(row)
            shapes[group] = geometry(contents)
    finally:
        connection.close()
    result = {}
    for group, rows in groups.items():
        comparisons = [r["comparison"] for r in rows if "comparison" in r]
        bins = {}
        for low, high in ((200000, 250000), (250000, 300000), (300000, 400000),
                          (400000, 500000), (500000, 1000000)):
            bins[str(low) + "_" + str(high)] = summarize([r for r in rows if low <= r["input"] < high])
        result[group] = {"summary": summarize(rows), "first": rows[0]["time"], "last": rows[-1]["time"],
                         "prefix_scope": "Logged simplified payload only; redacted images are not byte-verifiable.",
                         "bins": bins, "comparisons": len(comparisons),
                         "full_prefix_preserved": sum(c["prefix"]["previous_fully_preserved"] for c in comparisons),
                         "full_ordered_prefix_preserved": sum(c["ordered_prefix"]["previous_fully_preserved"] for c in comparisons),
                         "control_changes": sum(bool(c["changed_controls"]) for c in comparisons),
                         "ordered_control_changes": sum(bool(c["changed_ordered_controls"]) for c in comparisons),
                         "account_changes": sum(c["account_changed"] for c in comparisons),
                         "session_changes": sum(c["session_changed"] for c in comparisons),
                         "latest_geometry": shapes[group],
                         "image_additions": [{"time": r["time"], "previous_input": old["input"],
                                              "input": r["input"], "cached": r["cached"],
                                              "image_count": r["image_count"]}
                                             for old, r in zip(rows, rows[1:])
                                             if r["image_count"] > old["image_count"]],
                         "rows": rows}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path.home() / ".antigravity_tools/proxy_logs.db")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.database)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: {f: v for f, v in g.items() if f != "rows"} for k, g in result.items()}, indent=2))
