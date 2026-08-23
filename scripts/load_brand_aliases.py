"""Load curated Korean/alternate brand aliases into public.brand_aliases.

96% of brand_nodes carry a Latin-only brand_name, so a Korean-language brand
query ('글로니') cannot resolve without a curated alias ('글로니' → GLOWNY).
brand_node_cache warms `approved AND confidence='high'` rows into its Hangul
key index, so anything loaded here becomes brand-filter-resolvable on the next
ai-server restart.

Input: a TSV with columns  `brand_id <TAB> alias [<TAB> confidence]`.
  - brand_id   : public.brand_nodes.id (FK)
  - alias      : the surface form a user types (Korean or alternate spelling)
  - confidence : 'high' (default) | 'low'   — only 'high' becomes a hard filter

`alias_normalized` is computed with the SAME normalizer the cache uses (Hangul
preserved when present, else Latin) so the (brand_id, alias_normalized) unique
constraint dedups correctly.

Two run modes:
  --emit-sql        print idempotent INSERT SQL to stdout (pipe into psql; no
                    DB driver / SG 5432 access needed — used from the dev box)
  (default)         psycopg upsert via $KIKOAI_DEVAPP_DSN

Usage:
    # inspect / apply from the dev-app box (peer-trust psql):
    uv run python scripts/load_brand_aliases.py --emit-sql scripts/data/brand_aliases_seed.tsv \
        | ssh ec2-user@<dev-app> "docker exec -i db psql -U postgres -d kikoai"

    # or direct upsert when your IP is allowed on SG 5432:
    export KIKOAI_DEVAPP_DSN='postgresql://app_user:***@<host>:5432/kikoai?sslmode=require'
    uv run python scripts/load_brand_aliases.py scripts/data/brand_aliases_seed.tsv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.infrastructure.repositories.brand_node_cache import normalize_brand, normalize_brand_ko  # noqa: E402

SOURCE = "claude-opus"


def _normalized(alias: str) -> str:
    """Prefer the Hangul key; fall back to Latin. Matches the cache's surface
    keys closely enough for the unique constraint (dedup)."""
    return normalize_brand_ko(alias) or normalize_brand(alias)


def _parse_tsv(path: Path) -> list[tuple[int, str, str, str]]:
    rows: list[tuple[int, str, str, str]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            print(f"[warn] line {lineno}: expected 'brand_id<TAB>alias', got {raw!r}", file=sys.stderr)
            continue
        brand_id = int(parts[0].strip())
        alias = parts[1].strip()
        confidence = (parts[2].strip() if len(parts) > 2 else "high") or "high"
        norm = _normalized(alias)
        if not norm:
            print(f"[warn] line {lineno}: alias {alias!r} normalizes to empty — skipped", file=sys.stderr)
            continue
        rows.append((brand_id, alias, norm, confidence))
    return rows


def _sql_lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def emit_sql(rows: list[tuple[int, str, str, str]]) -> str:
    lines = ["BEGIN;"]
    for brand_id, alias, norm, confidence in rows:
        lines.append(
            "INSERT INTO public.brand_aliases "
            "(brand_id, alias, alias_normalized, lang, source, confidence, approved) VALUES "
            f"({brand_id}, {_sql_lit(alias)}, {_sql_lit(norm)}, 'ko', {_sql_lit(SOURCE)}, "
            f"{_sql_lit(confidence)}, true) "
            "ON CONFLICT (brand_id, alias_normalized) DO UPDATE SET "
            "alias = EXCLUDED.alias, confidence = EXCLUDED.confidence, "
            "source = EXCLUDED.source, approved = EXCLUDED.approved;"
        )
    lines.append("COMMIT;")
    return "\n".join(lines)


def upsert(rows: list[tuple[int, str, str, str]], dsn: str) -> int:
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO public.brand_aliases
                (brand_id, alias, alias_normalized, lang, source, confidence, approved)
            VALUES (%s, %s, %s, 'ko', %s, %s, true)
            ON CONFLICT (brand_id, alias_normalized) DO UPDATE
              SET alias = EXCLUDED.alias, confidence = EXCLUDED.confidence,
                  source = EXCLUDED.source, approved = EXCLUDED.approved
            """,
            [(b, a, n, SOURCE, c) for (b, a, n, c) in rows],
        )
        conn.commit()
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Load curated brand aliases.")
    ap.add_argument("tsv", type=Path, help="TSV: brand_id<TAB>alias[<TAB>confidence]")
    ap.add_argument("--emit-sql", action="store_true", help="print INSERT SQL instead of connecting")
    args = ap.parse_args()

    rows = _parse_tsv(args.tsv)
    if not rows:
        print("[load_brand_aliases] no rows parsed", file=sys.stderr)
        sys.exit(1)

    if args.emit_sql:
        print(emit_sql(rows))
        return

    import os

    dsn = os.environ.get("KIKOAI_DEVAPP_DSN", "")
    if not dsn:
        print("KIKOAI_DEVAPP_DSN not set (or use --emit-sql)", file=sys.stderr)
        sys.exit(2)
    n = upsert(rows, dsn)
    print(f"[load_brand_aliases] upserted {n} aliases")


if __name__ == "__main__":
    main()
