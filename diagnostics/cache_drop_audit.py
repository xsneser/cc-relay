"""Read-only cache audit. Emit fingerprints and counts, never prompt text or keys."""
import argparse
import collections
import datetime
import hashlib
import json
import sqlite3
from pathlib import Path


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()[:16]


def sequence(values):
    return [(digest(value), len(encoded(value))) for value in values]


def prefix(previous, current):
    count = 0
    for a, b in zip(previous, current):
        if a[0] != b[0]:
            break
        count += 1
    return {
        "shared_items": count,
        "previous_items": len(previous),
        "current_items": len(current),
        "previous_fully_preserved": count == len(previous),
        "shared_byte_fraction": round(sum(x[1] for x in current[:count]) /
                                      max(1, sum(x[1] for x in current)), 4),
    }


def rollup(rows):
    total = sum(r["input"] for r in rows)
    cached = sum(r["cached"] or 0 for r in rows)
    return {"requests": len(rows), "input": total, "reported_cached": cached,
            "missing_cache_field": sum(r["cached"] is None for r in rows),
            "weighted_reported_hit": round(cached / total, 4) if total else None}


def run(database, limit=160):
    aliases = collections.defaultdict(dict)

    def alias(kind, value):
        if not value:
            return None
        names = aliases[kind]
        if value not in names:
            names[value] = kind + str(len(names) + 1)
        return names[value]

    connection = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    sql = """SELECT timestamp,model,mapped_model,input_tokens,cached_tokens,
                    account_email,session_id,request_body,upstream_request_body,
                    request_headers
             FROM (SELECT * FROM request_logs WHERE status=200 AND input_tokens>0
                   AND url NOT LIKE '%count_tokens%' ORDER BY timestamp DESC LIMIT ?)
             ORDER BY timestamp ASC"""
    previous = {}
    client_previous = {}
    rows = []
    try:
        for record in connection.execute(sql, (limit,)):
            try:
                upstream = json.loads(record["upstream_request_body"] or "{}")
                client = json.loads(record["request_body"] or "{}")
                headers = {k.lower(): v for k, v in json.loads(record["request_headers"] or "{}").items()}
            except (ValueError, TypeError, AttributeError):
                continue
            if not isinstance(upstream, dict) or not isinstance(client, dict):
                continue
            # Headers and identities are reduced to local ordinal labels only.
            client_session = headers.get("x-claude-code-session-id") or record["session_id"]
            upstream_session = upstream.get("sessionId")
            model = record["mapped_model"] or record["model"]
            account = alias("account", record["account_email"])
            session = alias("client", client_session)
            outgoing_session = alias("upstream", upstream_session)
            row = {
                "time": datetime.datetime.fromtimestamp(record["timestamp"] / 1000).isoformat(),
                "model": model, "input": record["input_tokens"], "cached": record["cached_tokens"],
                "hit": round((record["cached_tokens"] or 0) / record["input_tokens"], 4),
                "account": account, "client_session": session, "upstream_session": outgoing_session,
                "system_hash": digest(upstream.get("systemInstruction")),
                "tools_hash": digest(upstream.get("tools")),
                "generation_hash": digest(upstream.get("generationConfig")),
                "client_system_hash": digest(client.get("system")),
                "client_tools_hash": digest(client.get("tools")),
                "upstream_keys": sorted(upstream),
            }
            contents = sequence(upstream.get("contents") or [])
            messages = sequence(client.get("messages") or [])
            row["contents"] = len(contents)
            row["messages"] = len(messages)
            client_key = (session or outgoing_session, model)
            immediate = client_previous.get(client_key)
            if immediate:
                row["previous_client_request"] = {k: immediate[k] for k in
                                                   ("time", "input", "hit", "contents", "upstream_session")}
            client_previous[client_key] = row
            # A CLI session also sends unrelated auxiliary requests. Compare a
            # continuing history only with requests sharing its first message.
            origin = messages[0][0] if messages else None
            key = (*client_key, origin)
            if key[0] is not None and key in previous:
                old, old_contents, old_messages = previous[key]
                row["comparison"] = {
                    "previous_time": old["time"], "previous_hit": old["hit"],
                    "account_changed": account != old["account"],
                    "upstream_session_changed": outgoing_session != old["upstream_session"],
                    "system_changed": row["system_hash"] != old["system_hash"],
                    "tools_changed": row["tools_hash"] != old["tools_hash"],
                    "generation_changed": row["generation_hash"] != old["generation_hash"],
                    "client_system_changed": row["client_system_hash"] != old["client_system_hash"],
                    "client_tools_changed": row["client_tools_hash"] != old["client_tools_hash"],
                    "upstream_prefix": prefix(old_contents, contents),
                    "client_prefix": prefix(old_messages, messages),
                }
            previous[key] = (row, contents, messages)
            rows.append(row)
    finally:
        connection.close()
    groups = {
        "below_20k": [r for r in rows if r["input"] < 20000],
        "20k_to_200k": [r for r in rows if 20000 <= r["input"] < 200000],
        "at_least_200k": [r for r in rows if r["input"] >= 200000],
    }
    return {"scope": "Latest successful generation requests with positive input tokens; missing cache counts treated as zero only for reported rate.",
            "totals": rollup(rows), "groups": {k: rollup(v) for k, v in groups.items()}, "requests": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path.home() / ".antigravity_tools/proxy_logs.db")
    parser.add_argument("--limit", type=int, default=160)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.database, args.limit)
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"totals": result["totals"], "groups": result["groups"]}, ensure_ascii=False))
