"""Read-only checks of a re-chunked offline copy against its exact backup."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from scripts.backup_databases import check_databases, digest


def connection(folder):
    return sqlite3.connect((folder / "tunnel_qa.db").resolve().as_uri() + "?mode=ro", uri=True)


def verify(before: Path, after: Path, plan: dict) -> dict:
    before, after = before.resolve(), after.resolve()
    counts = check_databases(after)
    assert digest(before / "original_documents.db") == digest(after / "original_documents.db"), "Original archive changed"
    with closing(connection(before)) as old, closing(connection(after)) as new:
        tables = {row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        mutable = {"source_documents", "chunks", "ontology_types", "evidence", "knowledge_entities",
                   "knowledge_relations", "knowledge_evidence_links", "knowledge_reviews", "knowledge_releases"}
        protected = sorted(tables - mutable)
        for name in protected:
            assert old.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() == new.execute(
                f'SELECT * FROM "{name}" ORDER BY rowid').fetchall(), f"Protected table changed: {name}"
        old_chunks = old.execute("SELECT * FROM chunks ORDER BY id").fetchall()
        columns = [r[1] for r in new.execute("PRAGMA table_info(chunks)")]
        id_column = columns.index("id")
        new_rows = {row[id_column]: row for row in new.execute("SELECT * FROM chunks")}
        assert all(row == new_rows.get(row[id_column]) for row in old_chunks), "Historical chunk changed"
        # Compare user-authored facts including their properties, aliases and
        # review status. A new evidence pointer/update timestamp is permissible.
        manual_checked = {}
        for kind, table in (("entity", "knowledge_entities"), ("relation", "knowledge_relations")):
            ids = {r[0] for r in old.execute(f"""SELECT f.id FROM {table} f
                JOIN evidence e ON e.id=f.evidence_id JOIN chunks c ON c.id=e.chunk_id
                JOIN source_documents d ON d.id=c.document_id WHERE d.source_id='SYS-GRAPH-MANUAL'
                UNION SELECT l.target_id FROM knowledge_evidence_links l
                JOIN source_documents d ON d.id=l.document_id
                WHERE l.target_type=? AND d.source_id='SYS-GRAPH-MANUAL'""", (kind,))}
            names = [r[1] for r in old.execute(f"PRAGMA table_info({table})") if r[1] not in {"evidence_id", "updated_at"}]
            fields = ",".join(names)
            for fact_id in ids:
                assert old.execute(f"SELECT {fields} FROM {table} WHERE id=?", (fact_id,)).fetchone() == new.execute(
                    f"SELECT {fields} FROM {table} WHERE id=?", (fact_id,)).fetchone(), "Manual fact changed"
            manual_checked[kind] = len(ids)
        for entry in plan["documents"]:
            row = new.execute("SELECT processing_version, parsed_text, metadata_extra FROM source_documents WHERE id=?", (entry["document_id"],)).fetchone()
            assert row[0] == entry["old_version"] + 1
            assert row[1] == old.execute("SELECT parsed_text FROM source_documents WHERE id=?", (entry["document_id"],)).fetchone()[0]
            current = new.execute("SELECT text FROM chunks WHERE document_id=? AND processing_version=? ORDER BY ordinal", (entry["document_id"], row[0])).fetchall()
            assert [r[0] for r in current] == [c["text"] for c in entry["chunks"]]
        active_count = new.execute("""SELECT COUNT(*) FROM chunks c JOIN source_documents d ON d.id=c.document_id
            WHERE d.source_id!='SYS-GRAPH-MANUAL' AND c.processing_version=d.processing_version""").fetchone()[0]
        assert active_count == plan["summary"]["new_chunks"]
        broken = new.execute("""SELECT COUNT(*) FROM knowledge_relations r
            JOIN knowledge_entities s ON r.source_entity_id=s.id JOIN knowledge_entities t ON r.target_entity_id=t.id
            WHERE r.status='published' AND (s.status!='published' OR t.status!='published')""").fetchone()[0]
        assert broken == 0, "Published edge has an unpublished endpoint"
        for table in ("knowledge_entities", "knowledge_relations"):
            stale_primary = new.execute(f"""SELECT COUNT(*) FROM {table} f
                JOIN evidence e ON e.id=f.evidence_id JOIN chunks c ON c.id=e.chunk_id
                JOIN source_documents d ON d.id=c.document_id
                WHERE f.status='published' AND d.source_id!='SYS-GRAPH-MANUAL'
                AND c.processing_version!=d.processing_version""").fetchone()[0]
            assert stale_primary == 0, "Published fact retains historical primary evidence"
        graph_counts = {name: new.execute(f"SELECT COUNT(*) FROM {name} WHERE status='published'").fetchone()[0]
                        for name in ("knowledge_entities", "knowledge_relations")}
    return {"passed": True, "mirror_integrity": counts, "active_chunks": active_count,
            "historical_chunks_unchanged": len(old_chunks), "protected_tables_unchanged": protected,
            "manual_facts_unchanged": manual_checked, "published_graph": graph_counts,
            "published_primary_evidence_current": True,
            "quarantined_pages": sum(len(d.get("quarantined_pages", [])) for d in plan["documents"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--report-to", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.before, args.after, json.loads(args.plan.read_text(encoding="utf-8")))
    args.report_to.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
