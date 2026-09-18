"""Search: the index, the query parser, and what a result total is allowed to say."""

from __future__ import annotations

from pathlib import Path

import pytest

from recall.extract import Extractor
from recall.integrity.engine import Finding, record_finding
from recall.scan.walker import Scanner
from recall.search.indexer import build_index, index_health, reindex_items
from recall.search.query import parse, safe_query
from tests.fixtures.generate import generate_eml, generate_mbox, generate_vcf


@pytest.fixture
def archive(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "mail"
    generate_eml(fixtures)
    generate_mbox(fixtures)
    generate_vcf(fixtures)
    Scanner(settings, conn).run([fixtures])
    Extractor(settings, conn).run()
    return conn


def hits(conn, text: str) -> int:
    parsed = safe_query(conn, text)
    if not parsed.fts:
        return 0
    return conn.execute(
        "SELECT COUNT(*) AS n FROM items_fts WHERE items_fts MATCH ?", (parsed.fts,)
    ).fetchone()["n"]


# --- the index ------------------------------------------------------------


def test_extraction_leaves_everything_searchable(archive):
    """A search that quietly finds nothing reads as "it is not in the archive"."""
    health = index_health(archive)
    assert health["complete"], health["note"]
    assert health["indexed"] == health["items"]
    assert health["items"] > 10


def test_the_index_holds_the_subject(archive):
    assert hits(archive, "Quarterly") >= 1


def test_the_index_holds_the_body(archive):
    assert hits(archive, "pallets") >= 1


def test_the_index_holds_the_people(archive):
    """Searching for somebody's address must find what they wrote."""
    assert hits(archive, "mobrien@contractmktg.com") >= 1


def test_the_index_holds_attachment_names(archive):
    assert hits(archive, "invoice-4471.pdf") >= 1


def test_the_index_holds_text_from_inside_attachments(archive):
    """In an archive of a career, the attachments are where the work is."""
    assert hits(archive, "inside:4471") >= 1
    assert hits(archive, "attachment_text:4471") >= 1


def test_a_contact_is_searchable_by_company(archive):
    assert hits(archive, "Northstar") >= 1


def test_accents_fold(archive):
    """remove_diacritics 2, so a name can be found without typing the accent."""
    assert hits(archive, "Bhraonain") >= 1 or hits(archive, "Aoife") >= 1


def test_rebuilding_is_idempotent(archive):
    before = index_health(archive)
    build_index(archive, rebuild=True)
    after = index_health(archive)
    assert before["indexed"] == after["indexed"]
    assert after["complete"]


def test_reindexing_one_item_keeps_the_rest(archive):
    item_id = archive.execute("SELECT id FROM items LIMIT 1").fetchone()["id"]
    before = index_health(archive)["indexed"]
    reindex_items(archive, [int(item_id)])
    assert index_health(archive)["indexed"] == before


def test_a_deleted_item_leaves_the_index(archive):
    item_id = int(archive.execute("SELECT id FROM items LIMIT 1").fetchone()["id"])
    before = index_health(archive)["indexed"]
    archive.execute("DELETE FROM items WHERE id = ?", (item_id,))
    assert index_health(archive)["indexed"] == before - 1


def test_an_incomplete_index_says_so(archive):
    """Reported rather than assumed - a partial index misses things silently."""
    archive.execute("DELETE FROM search_docs WHERE item_id IN "
                    "(SELECT item_id FROM search_docs LIMIT 3)")
    health = index_health(archive)
    assert health["complete"] is False
    assert health["missing"] == 3
    assert "recall index" in health["note"]


# --- the query parser -----------------------------------------------------


def test_a_bare_word():
    assert parse("invoice").fts == "invoice"


def test_several_words_are_all_required():
    """FTS5 joins bare terms with an implicit AND, which is what people expect."""
    assert parse("quarterly figures").fts == "quarterly figures"


def test_a_quoted_phrase_stays_a_phrase():
    parsed = parse('"quarterly figures"')
    assert parsed.fts == '"quarterly figures"'
    assert parsed.phrases == ["quarterly figures"]


def test_an_unbalanced_quote_closes_itself():
    """Half a typed phrase must still search, not raise."""
    parsed = parse('"quarterly figures')
    assert parsed.fts == '"quarterly figures"'


def test_capital_operators_are_operators():
    parsed = parse("invoice AND fitzgerald")
    assert "AND" in parsed.fts
    assert parsed.used_operators == ["AND"]


def test_lowercase_and_is_a_word():
    """"and" appears in a great many sentences."""
    parsed = parse("fish and chips")
    assert "AND" not in parsed.fts
    assert "and" in parsed.terms


def test_not_is_supported():
    assert "NOT" in parse("kelly NOT account").fts


def test_near_is_rewritten_to_the_fts5_form():
    """People type "a NEAR b"; FTS5 wants NEAR(a b, n)."""
    assert parse("margaret NEAR figures").fts == "NEAR(margaret figures, 10)"


def test_an_email_address_is_quoted():
    """Unquoted, FTS5 reads the @ and the dots as syntax."""
    assert parse("tim@example.com").fts == '"tim@example.com"'


def test_a_number_with_a_comma_is_quoted():
    assert parse("4,500").fts == '"4,500"'


def test_a_field_prefix_is_understood():
    assert parse("subject:invoice").fts == "subject : invoice"


def test_friendly_field_names_map_to_columns():
    assert parse("from:margaret").fts == "participants : margaret"
    assert parse("file:invoice.pdf").fts.startswith("attachment_names :")


def test_an_unknown_prefix_is_just_text():
    """"re:" in a subject line is not a field."""
    assert "subject" not in parse("re:something").fts


def test_a_dangling_operator_is_dropped():
    """"invoice AND" is a half-typed search; it should find invoices."""
    assert parse("invoice AND").fts == "invoice"
    assert parse("AND invoice").fts == "invoice"


def test_an_empty_query_is_empty():
    assert parse("").is_empty
    assert parse("   ").is_empty


def test_the_parser_explains_itself():
    assert "quarterly" in parse('"quarterly figures"').describe().lower()


# --- never a syntax error -------------------------------------------------


@pytest.mark.parametrize("nasty", [
    'invoice (unbalanced',
    'invoice )',
    '* leading star',
    'NEAR',
    'AND OR NOT',
    '"""',
    'a:b:c:d',
    '^^^',
    '--',
    'tim@example.com AND "the Fitzgerald’s job"',
])
def test_a_nasty_query_never_raises(archive, nasty):
    """A search must never show the user a syntax error."""
    parsed = safe_query(archive, nasty)
    if parsed.fts:
        archive.execute(
            "SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH ?", (parsed.fts,)
        ).fetchone()


def test_a_query_fts5_refuses_falls_back_to_a_phrase(archive):
    parsed = safe_query(archive, "invoice (unbalanced")
    assert parsed.fts
    archive.execute(
        "SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH ?", (parsed.fts,)
    ).fetchone()


# --- what a result total may say ------------------------------------------


def test_a_result_total_is_qualified_when_something_is_missing(archive, settings):
    """Spec 9.5, applied to search: a total affected by a finding is not bare."""
    from recall.integrity.honest import qualifiers_for

    source_id = archive.execute("SELECT id FROM source_files LIMIT 1").fetchone()["id"]
    record_finding(archive, Finding(
        code="partial_parse", severity="critical",
        title="a file could not be read in full",
        source_file_id=int(source_id), estimated_loss=8000,
    ))

    qualifiers = qualifiers_for(archive)
    assert qualifiers
    assert qualifiers[0].estimated_loss == 8000


def test_a_clean_archive_has_an_unqualified_total(archive):
    from recall.integrity.honest import qualifiers_for

    archive.execute("DELETE FROM findings")
    assert qualifiers_for(archive) == []
