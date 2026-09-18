from pathlib import Path


def test_v6_rpc_scopes_every_candidate_path_by_platform() -> None:
    sql = Path("sql/functions/search_products_v6.sql").read_text(encoding="utf-8")

    assert "p_platform      text DEFAULT NULL::text" in sql
    assert sql.count("AND (p_platform IS NULL OR p.platform = p_platform)") == 5
    assert "AND v_target_family <> 'other' THEN" in sql


def test_hybrid_rpc_scopes_every_candidate_source_by_platform() -> None:
    sql = Path("sql/functions/search_products_hybrid_v1.sql").read_text(encoding="utf-8")

    assert "p_platform      text DEFAULT NULL::text" in sql
    assert sql.count("AND (p_platform IS NULL OR p.platform = p_platform)") == 3
