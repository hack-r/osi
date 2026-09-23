#!/usr/bin/env python3
"""Full logical replication of one D1 database into another.

Rebuilds the target to match the source exactly: schema (tables, indexes,
views), then every row of every table. The target is replaced, not merged, so a
run always leaves a faithful point-in-time copy rather than an accumulation of
whatever happened to be there before.

Why this exists: `whoneedsit-backup` was created by hand and ended up holding
the schema plus a stale `people`/`authors` but *zero* articles, which would not
have restored anything. A backup nobody has verified is not a backup, so this
script checks row counts per table at the end and exits non-zero on any
mismatch.

Usage:
    export CLOUDFLARE_API_TOKEN=...      # needs D1:Edit on the account
    export CLOUDFLARE_ACCOUNT_ID=...
    python d1_replicate.py                       # whoneedsit -> whoneedsit-backup
    python d1_replicate.py --dry-run             # report what would change
    python d1_replicate.py --source X --target Y # explicit database uuids

Exit codes: 0 verified, 1 verification failed, 2 configuration/API error.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

API_ROOT = "https://api.cloudflare.com/client/v4"

# Defaults for this project. Override with --source/--target.
SOURCE_DEFAULT = "82c55bae-2ae2-4502-bd8e-1a790ba981c4"  # whoneedsit
TARGET_DEFAULT = "104526a8-4e12-4147-997e-1fe9ceff02e9"  # whoneedsit-backup

# Internal D1 bookkeeping table: present in every database, never ours to copy.
SKIP_TABLES = {"_cf_KV", "sqlite_sequence"}

# Rows per INSERT. D1 caps bound parameters per statement, so the batch size is
# reduced further at runtime for wide tables (see rows_per_insert).
MAX_BOUND_PARAMS = 100
BATCH_ROWS = 200
PAGE_ROWS = 500


class D1Error(RuntimeError):
    pass


class D1:
    """Thin client over the D1 HTTP query endpoint."""

    def __init__(self, account_id: str, token: str, database_id: str):
        self.url = (f"{API_ROOT}/accounts/{account_id}"
                    f"/d1/database/{database_id}/query")
        self.token = token
        self.database_id = database_id

    def query(self, sql: str, params: Optional[List[Any]] = None,
              idempotent: bool = False, retries: int = 4) -> List[dict]:
        """Run `sql`. Only retries when the caller says replaying is safe.

        A 5xx, a 429 or a dropped connection does not tell us whether the
        statement committed -- only that we did not see the response. Replaying
        a write on that evidence can insert a batch twice or trip a UNIQUE
        constraint, and either way the run ends up neither clean nor cleanly
        failed. So writes get one attempt and surface the error; reads, which
        replay harmlessly, get the backoff.
        """
        payload = {"sql": sql}
        if params:
            payload["params"] = params
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})

        attempts = retries if idempotent else 0
        delay = 2.0
        for attempt in range(attempts + 1):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read())
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                # 429/5xx are worth retrying; 4xx client errors are not.
                if e.code in (429, 500, 502, 503, 504) and attempt < attempts:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise D1Error(f"HTTP {e.code} on {self.database_id}: {detail}")
            except urllib.error.URLError as e:
                if attempt < attempts:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise D1Error(f"network error on {self.database_id}: {e}")
        else:  # pragma: no cover - loop always breaks or raises
            raise D1Error("exhausted retries")

        if not data.get("success"):
            raise D1Error(f"{self.database_id}: {data.get('errors')}\n  sql: {sql[:200]}")
        return data.get("result", [])

    def rows(self, sql: str, params: Optional[List[Any]] = None) -> List[dict]:
        # Reads are safe to replay, so these carry the retry budget.
        result = self.query(sql, params, idempotent=True)
        return result[0].get("results", []) if result else []

    def scalar(self, sql: str) -> Any:
        rows = self.rows(sql)
        return next(iter(rows[0].values())) if rows else None


def schema_objects(db: D1) -> Dict[str, List[dict]]:
    """Source DDL, split by kind and ordered so dependencies resolve.

    Views are created last because they reference tables; indexes come after
    their table. Rows with a NULL `sql` are SQLite's implicit indexes
    (autoindex for UNIQUE/PK), which are recreated automatically by the table
    DDL and must not be issued directly.
    """
    rows = db.rows(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 "
        "WHEN 'trigger' THEN 2 ELSE 3 END, name")
    out: Dict[str, List[dict]] = {"table": [], "index": [], "trigger": [], "view": []}
    for r in rows:
        if r["type"] in out and r["name"] not in SKIP_TABLES:
            out[r["type"]].append(r)
    return out


def table_columns(db: D1, table: str) -> List[str]:
    rows = db.rows(f'PRAGMA table_info("{table}")')
    return [r["name"] for r in rows]


def load_order(db: D1, tables: List[str]) -> List[str]:
    """Order tables so every parent loads before its children.

    `PRAGMA defer_foreign_keys` lasts only for the transaction that sets it,
    and each HTTP request here is its own transaction -- so the pragma cannot
    hold rows back across the many INSERTs a copy takes. Loading parents first
    is what actually satisfies the constraints: `authors.person_id` references
    `people`, `articles.org_id` references `orgs`, and so on.

    Cycles (and self-references, which SQLite allows) fall back to source
    order rather than raising, since a cycle cannot be satisfied by ordering
    alone and the copy may still succeed row by row.
    """
    deps: Dict[str, set] = {t: set() for t in tables}
    known = set(tables)
    for t in tables:
        for fk in db.rows(f'PRAGMA foreign_key_list("{t}")'):
            parent = fk.get("table")
            if parent in known and parent != t:
                deps[t].add(parent)

    ordered: List[str] = []
    placed = set()
    # Kahn's algorithm, ties broken by the source ordering for reproducibility.
    while len(ordered) < len(tables):
        ready = [t for t in tables
                 if t not in placed and deps[t] <= placed]
        if not ready:                      # cycle: emit the rest as-is
            ordered.extend(t for t in tables if t not in placed)
            break
        for t in ready:
            ordered.append(t)
            placed.add(t)
    return ordered


def rows_per_insert(ncols: int) -> int:
    """Keep each INSERT under the bound-parameter ceiling."""
    if ncols <= 0:
        return BATCH_ROWS
    return max(1, min(BATCH_ROWS, MAX_BOUND_PARAMS // ncols))


def copy_table(src: D1, dst: D1, table: str, verbose: bool = True) -> int:
    """Page every row of `table` from source to target. Returns rows written."""
    cols = table_columns(src, table)
    if not cols:
        return 0
    collist = ", ".join(f'"{c}"' for c in cols)
    per_insert = rows_per_insert(len(cols))
    placeholder = "(" + ", ".join("?" for _ in cols) + ")"

    written = 0
    offset = 0
    while True:
        page = src.rows(
            f'SELECT {collist} FROM "{table}" LIMIT {PAGE_ROWS} OFFSET {offset}')
        if not page:
            break
        for i in range(0, len(page), per_insert):
            chunk = page[i:i + per_insert]
            params: List[Any] = []
            for row in chunk:
                params.extend(row[c] for c in cols)
            values = ", ".join(placeholder for _ in chunk)
            dst.query(
                f'INSERT INTO "{table}" ({collist}) VALUES {values}', params)
            written += len(chunk)
        offset += PAGE_ROWS
        if verbose:
            print(f"    {table}: {written} rows", end="\r", flush=True)
    if verbose and written:
        print(f"    {table}: {written} rows      ")
    return written


def drop_existing(dst: D1, verbose: bool = True) -> None:
    """Clear the target in a single request, so the FK deferral actually holds.

    `PRAGMA defer_foreign_keys` lasts only for the transaction that sets it, and
    one HTTP request is one transaction -- so the pragma and every DROP have to
    travel together. Issued as separate requests the pragma would be gone by the
    first DROP, and `article_tags`/`article_authors` (ON DELETE CASCADE against
    `articles`) could cascade rows away mid-teardown.

    Triggers and views go before tables so nothing outlives what it references;
    indexes are dropped with IF EXISTS because a table's own indexes disappear
    with it.
    """
    rows = dst.rows(
        "SELECT type, name FROM sqlite_master "
        "WHERE type IN ('view','table','index','trigger') "
        "AND name NOT LIKE 'sqlite_%'")

    stmts = ["PRAGMA defer_foreign_keys = true"]
    for kind in ("trigger", "view", "index", "table"):
        stmts += [f'DROP {kind.upper()} IF EXISTS "{r["name"]}"'
                  for r in rows
                  if r["type"] == kind and r["name"] not in SKIP_TABLES]

    if len(stmts) > 1:
        dst.query("; ".join(stmts) + ";")
    if verbose:
        print(f"  dropped {len(stmts) - 1} existing objects")


def counts(db: D1, tables: Iterable[str]) -> Dict[str, int]:
    return {t: db.scalar(f'SELECT COUNT(*) FROM "{t}"') or 0 for t in tables}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=os.environ.get("D1_SOURCE", SOURCE_DEFAULT))
    ap.add_argument("--target", default=os.environ.get("D1_TARGET", TARGET_DEFAULT))
    ap.add_argument("--dry-run", action="store_true",
                    help="report source/target row counts and exit without writing")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not token or not account:
        print("error: set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID",
              file=sys.stderr)
        return 2
    if args.source == args.target:
        print("error: source and target are the same database", file=sys.stderr)
        return 2

    verbose = not args.quiet
    src = D1(account, token, args.source)
    dst = D1(account, token, args.target)

    try:
        schema = schema_objects(src)
        tables = [t["name"] for t in schema["table"]]
        src_counts = counts(src, tables)
        total = sum(src_counts.values())
        if verbose:
            print(f"source {args.source}: {len(tables)} tables, {total:,} rows")

        if args.dry_run:
            try:
                dst_tables = [r["name"] for r in dst.rows(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'") if r["name"] not in SKIP_TABLES]
                dst_counts = counts(dst, dst_tables)
            except D1Error as e:
                print(f"  target unreadable: {e}")
                dst_counts = {}
            print(f"\n{'table':<24}{'source':>10}{'target':>10}  drift")
            for t in sorted(set(tables) | set(dst_counts)):
                s, d = src_counts.get(t, 0), dst_counts.get(t, 0)
                print(f"{t:<24}{s:>10,}{d:>10,}  {'' if s == d else 'DIFFERS'}")
            return 0

        if verbose:
            print(f"target {args.target}: rebuilding")
        drop_existing(dst, verbose)

        # Schema, in dependency order.
        for kind in ("table", "index", "view", "trigger"):
            for obj in schema[kind]:
                dst.query(obj["sql"])
            if verbose and schema[kind]:
                print(f"  created {len(schema[kind])} {kind}s")

        # Data, parents before children. Each request is its own transaction,
        # so a deferral pragma would not survive to the next INSERT.
        order = load_order(src, tables)
        if verbose:
            print("  copying rows")
        for t in order:
            copy_table(src, dst, t, verbose)

        # Verify: per-table counts must match exactly.
        dst_counts = counts(dst, tables)
        bad = {t: (src_counts[t], dst_counts.get(t, 0))
               for t in tables if src_counts[t] != dst_counts.get(t, 0)}

        fk = dst.rows("PRAGMA foreign_key_check")
        if bad or fk:
            print("\nVERIFICATION FAILED", file=sys.stderr)
            for t, (s, d) in sorted(bad.items()):
                print(f"  {t}: source {s:,} != target {d:,}", file=sys.stderr)
            if fk:
                print(f"  {len(fk)} foreign key violations", file=sys.stderr)
            return 1

        print(f"OK: {len(tables)} tables, {sum(dst_counts.values()):,} rows "
              f"replicated and verified")
        return 0

    except D1Error as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
