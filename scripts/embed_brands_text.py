"""BGE-m3 brand text embedding + brand_similar graph builder.

Encodes the canonical text pool of every brand_nodes row with BGE-m3
(1024-dim, multilingual, KO/EN strong). Updates brand_nodes.embedding
and rebuilds brand_similar (top-K cosine neighbors).

Idempotent: rows whose canonical text hash matches embedding_text_hash
are skipped (override with --force). brand_similar rows are deleted
and re-inserted per source brand_id, so partial reruns are safe.

환경변수: `.env` 의 `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` 자동 로드.

사용:
    # 첫 1회 — sentence-transformers / torch / numpy 설치 (~2GB BGE-m3 가중치 포함)
    uv sync --group embed-text

    # 작게 테스트
    uv run python scripts/embed_brands_text.py --limit 20 --dry-run

    # 실 실행
    uv run python scripts/embed_brands_text.py

    # 옵션
    uv run python scripts/embed_brands_text.py --top-k 30
    uv run python scripts/embed_brands_text.py --force          # 해시 무시
    uv run python scripts/embed_brands_text.py --skip-similar   # 임베딩만
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

# 프로젝트 root 를 sys.path 에 추가 — scripts/ 에서 실행 시 app.* import 가능
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402

MODEL_ID = "BAAI/bge-m3"
DIM = 1024
DEFAULT_TOP_K = 20
DEFAULT_BATCH = 32


def build_text_pool(brand: dict) -> str:
    """Deterministic, sorted text pool. Same input → same hash → skip re-embed."""
    attrs = brand.get("attributes") or {}

    def joined(values: list[str] | None) -> str:
        return ", ".join(sorted(values)) if values else "(없음)"

    parts = [
        f"브랜드 이름: {brand['brand_name']}",
        f"카테고리: {brand.get('category_type') or '의류'}",
        f"스타일 노드: {brand.get('style_node') or '(없음)'}",
        f"감성 태그: {joined(brand.get('sensitivity_tags'))}",
        f"브랜드 키워드: {joined(brand.get('brand_keywords'))}",
        f"실루엣: {joined(attrs.get('silhouette'))}",
        f"팔레트: {joined(attrs.get('palette'))}",
        f"소재: {joined(attrs.get('material'))}",
        f"디테일: {joined(attrs.get('detail'))}",
        f"무드: {joined(attrs.get('vibe'))}",
        f"유통 플랫폼: {joined(brand.get('source_platforms'))}",
    ]
    return "\n".join(parts)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cosine_top_k(embeddings, ids: list[str], k: int):
    """L2-normalized embeddings → cosine top-K per row.

    Returns list of (source_id, [(target_id, similarity, rank), ...]).
    Negative-similarity neighbors are filtered out.
    """
    import numpy as np

    sim = embeddings @ embeddings.T  # (N, N), cosine since L2-normalized
    np.fill_diagonal(sim, -1.0)
    n = len(ids)
    eff_k = min(k, n - 1)
    out: list[tuple[str, list[tuple[str, float, int]]]] = []
    for i in range(n):
        row = sim[i]
        top_idx = np.argpartition(row, -eff_k)[-eff_k:]
        top_idx = top_idx[np.argsort(-row[top_idx])]  # sorted desc
        pairs: list[tuple[str, float, int]] = []
        for rank, j in enumerate(top_idx, start=1):
            score = float(row[j])
            if score <= 0.0:
                continue
            pairs.append((ids[j], min(score, 1.0), rank))
        out.append((ids[i], pairs))
    return out


def parse_embedding(raw) -> list[float]:
    """Supabase가 vector 컬럼을 list 또는 string으로 반환할 수 있음."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    raise ValueError(f"unexpected embedding type: {type(raw)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, default=None, help="브랜드 수 제한 (테스트용)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--dry-run", action="store_true", help="DB 쓰지 않고 결과만 출력")
    parser.add_argument("--force", action="store_true", help="text_hash 일치해도 재임베딩")
    parser.add_argument("--skip-similar", action="store_true", help="brand_similar 갱신 생략")
    args = parser.parse_args()

    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
        print("ERROR: SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY 미설정 (.env 확인)", file=sys.stderr)
        return 2

    from supabase import create_client

    sb = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

    # ─── 1. brand_nodes 로드 ─────────────────────────────────
    print("[1/5] brand_nodes 로드 중...")
    query = sb.table("brand_nodes").select(
        "id, brand_name, brand_keywords, style_node, sensitivity_tags, attributes, "
        "category_type, source_platforms, embedding_text_hash"
    )
    if args.limit:
        query = query.limit(args.limit)
    brands = query.execute().data
    print(f"     {len(brands)}개 로드")

    # ─── 2. text pool + hash + dedup ────────────────────────
    to_embed: list[tuple[str, str, str]] = []  # (id, text, hash)
    skip_count = 0
    for b in brands:
        text = build_text_pool(b)
        h = text_hash(text)
        if not args.force and b.get("embedding_text_hash") == h:
            skip_count += 1
            continue
        to_embed.append((b["id"], text, h))
    print(f"[2/5] 신규/변경: {len(to_embed)}개, 건너뜀(idempotent): {skip_count}개")

    embeddings = None
    if to_embed:
        # ─── 3. BGE-m3 인코딩 ───────────────────────────────
        print("[3/5] BGE-m3 로드 중 (첫 1회 ~2GB 다운로드)...")
        import torch
        from sentence_transformers import SentenceTransformer

        if torch.backends.mps.is_available():
            device = "mps"
        elif torch.cuda.is_available():
            device = "cuda"
        else:
            device = "cpu"

        model = SentenceTransformer(MODEL_ID, device=device)
        print(f"     ready on {device}")

        texts = [t for (_, t, _) in to_embed]
        t0 = time.time()
        embeddings = model.encode(
            texts,
            batch_size=args.batch_size,
            show_progress_bar=True,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        print(f"     인코딩 완료 ({time.time() - t0:.1f}s)")
        if embeddings.shape != (len(texts), DIM):
            print(f"ERROR: shape mismatch {embeddings.shape} != ({len(texts)}, {DIM})", file=sys.stderr)
            return 3

        # ─── 4. brand_nodes UPDATE ─────────────────────────
        if args.dry_run:
            print("[4/5] DRY RUN — DB 쓰지 않음 (sample 1):")
            sample_id, _, sample_hash = to_embed[0]
            print(f"     id={sample_id} hash={sample_hash[:16]}... emb[:4]={embeddings[0][:4].tolist()}")
        else:
            print(f"[4/5] brand_nodes UPDATE ({len(to_embed)} rows)...")
            failed = 0
            for i, (bid, _, h) in enumerate(to_embed):
                try:
                    sb.table("brand_nodes").update(
                        {
                            "embedding": embeddings[i].tolist(),
                            "embedding_model": MODEL_ID,
                            "embedding_text_hash": h,
                        }
                    ).eq("id", bid).execute()
                    # embedded_at 은 server_default 가 없어서 별도 처리 — RPC now() 안 통해서 ISO 문자열로
                    sb.table("brand_nodes").update({"embedded_at": "now()"}).eq("id", bid).execute()
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    if failed <= 3:
                        print(f"     UPDATE 실패 [{bid}]: {e}", file=sys.stderr)
                if (i + 1) % 100 == 0:
                    print(f"     ... {i + 1}/{len(to_embed)}")
            if failed:
                print(f"     ⚠ {failed}건 실패")
            print("     완료")
    else:
        print("[3-4/5] 변경 없음 — 인코딩/UPDATE 생략")

    # ─── 5. brand_similar 갱신 ──────────────────────────────
    if args.skip_similar:
        print("[5/5] brand_similar 갱신 SKIP")
        return 0

    print("[5/5] 모든 brand_nodes.embedding 로드 → cosine 그래프...")
    rows = (
        sb.table("brand_nodes")
        .select("id, embedding, brand_name, category_type")
        .not_.is_("embedding", "null")
        .execute()
        .data
    )
    print(f"     임베딩 보유 브랜드: {len(rows)}")

    if len(rows) < 2:
        print("     (2개 미만 — graph 갱신 생략)")
        return 0

    import numpy as np

    # 제외 카테고리는 그래프에서 분리
    valid_rows = [r for r in rows if (r.get("category_type") or "의류") not in ("제외",)]
    ids = [r["id"] for r in valid_rows]
    names = {r["id"]: r["brand_name"] for r in valid_rows}
    emb_list = [parse_embedding(r["embedding"]) for r in valid_rows]
    emb_arr = np.asarray(emb_list, dtype=np.float32)
    print(f"     valid shape={emb_arr.shape} (제외 카테고리 분리)")

    result = cosine_top_k(emb_arr, ids, args.top_k)

    if args.dry_run:
        print("     DRY RUN — top-K 샘플 출력:")
        for src_id, pairs in result[:5]:
            print(f"\n     [{names[src_id]}]")
            for tgt_id, sim, rank in pairs[:5]:
                print(f"       {rank}. {names[tgt_id]} ({sim:.3f})")
        return 0

    print(f"     brand_similar 갱신 (per source, {len(result)} brands)...")
    written = 0
    for i, (src_id, pairs) in enumerate(result):
        if not pairs:
            continue
        sb.table("brand_similar").delete().eq("brand_id", src_id).execute()
        insert_rows = [
            {
                "brand_id": src_id,
                "similar_brand_id": tgt_id,
                "similarity": round(sim, 4),
                "rank": rank,
            }
            for tgt_id, sim, rank in pairs
        ]
        sb.table("brand_similar").insert(insert_rows).execute()
        written += len(insert_rows)
        if (i + 1) % 100 == 0:
            print(f"     ... {i + 1}/{len(result)}")
    print(f"     완료 — edges 총 {written}개")

    # ─── 검증 샘플 출력 ─────────────────────────────────────
    print()
    print("================== Anchor pair 검증 샘플 ==================")
    anchors = ["Arcteryx", "Studio Nicholson", "Acne", "Lemaire", "Baserange", "Asics"]
    for anchor in anchors:
        match_id = next((r["id"] for r in valid_rows if anchor.lower() in r["brand_name"].lower()), None)
        if not match_id:
            continue
        res = (
            sb.table("brand_similar")
            .select("similar_brand_id, similarity, rank")
            .eq("brand_id", match_id)
            .order("rank")
            .limit(10)
            .execute()
        )
        print(f"\n[{names[match_id]}] top-10:")
        for row in res.data:
            tgt = names.get(row["similar_brand_id"], "(unknown)")
            print(f"  {row['rank']:>2}. {tgt:<32} sim={row['similarity']:.3f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
