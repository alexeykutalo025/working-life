"""Identity resolution: what merges automatically, and what only ever gets proposed."""

from __future__ import annotations

import pytest

from recall.config import Settings
from recall.models import AddressType
from recall.normalize.merge import (
    MergeError,
    apply_merge,
    jaro_winkler,
    name_parts,
    normalize_name,
    recount_all,
    suggest_merges,
    undo_merge,
)
from recall.normalize.people import (
    PeopleResolver,
    is_legacy_dn,
    legacy_dn_name,
    normalize_address,
    resolve_person,
)

LEGACY_DN = "/o=CONTRACTMKTG/ou=First Administrative Group/cn=Recipients/cn=tmccarthy"


# --- address normalisation ------------------------------------------------


def test_case_and_spacing_do_not_matter():
    a = normalize_address("  Tim@Example.COM  ")
    assert a.address == "tim@example.com"
    assert a.address_type == AddressType.SMTP


def test_display_name_is_separated_from_the_address():
    a = normalize_address("Tim McCarthy <tim@example.com>")
    assert a.address == "tim@example.com"


def test_plus_tags_are_stripped():
    assert normalize_address("tim+invoices@example.com").address == "tim@example.com"


def test_dots_are_removed_only_for_providers_that_ignore_them():
    """At most providers j.smith@ and jsmith@ are two different people."""
    gmail = normalize_address(
        "first.last@gmail.com", dot_insensitive_domains=["gmail.com"]
    )
    assert gmail.address == "firstlast@gmail.com"

    other = normalize_address(
        "first.last@contractmktg.com", dot_insensitive_domains=["gmail.com"]
    )
    assert other.address == "first.last@contractmktg.com", (
        "removing dots here would merge two real people"
    )


def test_a_legacy_dn_is_kept_exactly_and_never_turned_into_an_address():
    """Spec 9.3: keep the raw DN visible; do not fabricate an address."""
    a = normalize_address(LEGACY_DN)
    assert a.address_type == AddressType.EX
    assert a.address == LEGACY_DN.upper()
    assert "@" not in a.address


def test_legacy_dn_account_name_is_a_label_not_an_address():
    assert legacy_dn_name(LEGACY_DN) == "tmccarthy"
    assert is_legacy_dn(LEGACY_DN)
    assert not is_legacy_dn("tim@example.com")


def test_a_name_with_no_address_is_still_an_identity():
    """"Bob from the printers" with no address is still a real correspondent."""
    a = normalize_address(None, "Bob from the printers")
    assert a.address_type == AddressType.NONE
    assert a.display_name == "Bob from the printers"


def test_a_phone_number_is_recognised():
    a = normalize_address("+353 1 555 0101")
    assert a.address_type == AddressType.PHONE
    assert a.address == "+35315550101"


def test_nothing_at_all_gives_nothing():
    assert normalize_address(None, None) is None


# --- the one automatic rule -----------------------------------------------


def test_the_same_address_is_always_the_same_person(conn, settings: Settings):
    """The only merge Recall performs without being asked."""
    resolver = PeopleResolver(conn, settings)
    first = resolver.identity_id(
        resolver.normalize("Tim McCarthy <tim@example.com>", "Tim McCarthy"),
        "2003-01-01T00:00:00Z",
    )
    second = resolver.identity_id(
        resolver.normalize("TIM@EXAMPLE.COM", "T. McCarthy"), "2005-01-01T00:00:00Z"
    )
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1


def test_different_addresses_are_different_people_until_told_otherwise(conn, settings):
    resolver = PeopleResolver(conn, settings)
    resolver.identity_id(resolver.normalize("a@x.com", "Margaret"), None)
    resolver.identity_id(resolver.normalize("b@y.com", "Margaret"), None)
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 2, (
        "the same name is not evidence enough to merge automatically"
    )


def test_configured_self_addresses_are_marked(conn, settings):
    settings.identity.me = ["tim@example.com"]
    resolver = PeopleResolver(conn, settings)
    resolver.identity_id(resolver.normalize("tim@example.com", "Tim"), None)
    row = conn.execute("SELECT is_self FROM people").fetchone()
    assert row["is_self"] == 1


def test_first_and_last_seen_widen(conn, settings):
    resolver = PeopleResolver(conn, settings)
    norm = resolver.normalize("tim@example.com", "Tim")
    resolver.identity_id(norm, "2003-01-01T00:00:00Z")
    resolver.identity_id(norm, "1997-01-01T00:00:00Z")
    resolver.identity_id(norm, "2011-01-01T00:00:00Z")
    row = conn.execute("SELECT first_seen_utc, last_seen_utc FROM identities").fetchone()
    assert row["first_seen_utc"] == "1997-01-01T00:00:00Z"
    assert row["last_seen_utc"] == "2011-01-01T00:00:00Z"


# --- Jaro-Winkler ---------------------------------------------------------


def test_identical_names_score_one():
    assert jaro_winkler("margaret obrien", "margaret obrien") == 1.0


def test_a_missing_apostrophe_still_scores_high():
    assert jaro_winkler("margaret obrien", "margaret o brien") > 0.9


def test_a_shared_surname_with_a_different_first_name_scores_lower():
    same = jaro_winkler("margaret obrien", "margaret obrien")
    different = jaro_winkler("margaret obrien", "michael obrien")
    assert different < same
    assert different < 0.92, "these must not be suggested as one person on name alone"


def test_unrelated_names_score_low():
    assert jaro_winkler("tim mccarthy", "declan walsh") < 0.7


def test_empty_names():
    assert jaro_winkler("", "") == 1.0
    assert jaro_winkler("a", "") == 0.0


def test_normalize_name_drops_titles_and_punctuation():
    assert normalize_name("Dr. Margaret O'Brien, PhD") == "margaret obrien"


def test_name_parts_handles_both_orders():
    assert name_parts("Margaret O'Brien") == ("margaret", "obrien")
    assert name_parts("O'Brien, Margaret") == ("margaret", "obrien")


# --- suggestions, never applications --------------------------------------


def _person(conn, name, address, org=None, item_count=0):
    cur = conn.execute(
        "INSERT INTO people(display_name, org, item_count) VALUES (?, ?, ?)",
        (name, org, item_count),
    )
    person_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO identities(person_id, address, address_type, raw_display_name) "
        "VALUES (?, ?, 'smtp', ?)",
        (person_id, address, name),
    )
    return person_id


def test_identical_names_are_proposed_not_merged(conn, settings):
    a = _person(conn, "Margaret O'Brien", "mobrien@contractmktg.com")
    b = _person(conn, "Margaret O'Brien", "margaret.obrien@eircom.net")

    proposals = suggest_merges(conn, settings)
    assert len(proposals) == 1
    assert proposals[0].key == (min(a, b), max(a, b))

    # And nothing was applied.
    assert conn.execute(
        "SELECT COUNT(*) FROM people WHERE merged_into IS NOT NULL"
    ).fetchone()[0] == 0


def test_the_proposal_carries_the_reason_to_doubt_it(conn, settings):
    _person(conn, "John Murphy", "jmurphy@a.com")
    _person(conn, "John Murphy", "john@b.com")
    proposal = suggest_merges(conn, settings)[0]
    assert proposal.evidence["caution"], (
        "a proposal on name alone must say that names are shared"
    )


def test_a_shared_domain_raises_the_confidence(conn, settings):
    _person(conn, "Margaret O'Brien", "mobrien@contractmktg.com")
    _person(conn, "Margaret O'Brien", "m.obrien@contractmktg.com")
    proposal = suggest_merges(conn, settings)[0]
    assert proposal.confidence >= 0.9
    assert proposal.evidence["shared_domain"] == ["contractmktg.com"]


def test_a_single_short_name_is_not_enough(conn, settings):
    """"bob" matching "bob" would merge every Bob in forty years."""
    _person(conn, "Bob", "bob@a.com")
    _person(conn, "Bob", "bob@b.com")
    assert suggest_merges(conn, settings) == []


def test_a_role_mailbox_is_never_proposed_for_merging(conn, settings):
    _person(conn, "Northstar Print", "info@northstarprint.co.uk")
    _person(conn, "Northstar Print", "info@northstar.com")
    assert suggest_merges(conn, settings) == [], (
        "a shared office mailbox is not a person and must not be merged as one"
    )


def test_two_different_first_names_at_one_company_are_not_proposed(conn, settings):
    """A family or two colleagues, not one person with two addresses."""
    _person(conn, "Margaret O'Brien", "margaret@contractmktg.com")
    _person(conn, "Michael O'Brien", "michael@contractmktg.com")
    proposals = suggest_merges(conn, settings)
    assert proposals == []


def test_an_initial_form_is_proposed_with_a_caution(conn, settings):
    _person(conn, "Margaret O'Brien", "margaret@contractmktg.com")
    _person(conn, "M O'Brien", "m.obrien@contractmktg.com")
    proposals = suggest_merges(conn, settings)
    assert proposals
    assert "initial" in proposals[0].evidence.get("caution", "").lower()


def test_similar_names_need_a_shared_correspondent(conn, settings):
    """The spec requires the shared correspondent as well as the similarity."""
    _person(conn, "Margaret OBrien", "a@x.com")
    _person(conn, "Margarete OBrien", "b@y.com")
    assert suggest_merges(conn, settings) == [], (
        "a similar name with nothing else is not evidence"
    )


def test_similar_names_with_a_shared_correspondent_are_proposed(conn, settings):
    a = _person(conn, "Margaret OBrien", "a@x.com")
    b = _person(conn, "Margarete OBrien", "b@y.com")
    shared = _person(conn, "Declan Walsh", "declan@z.com")

    for item_no, person_id in ((1, a), (1, shared), (2, b), (2, shared)):
        conn.execute(
            "INSERT OR IGNORE INTO items(id, kind, dedup_key) VALUES (?, 'message', ?)",
            (item_no, f"k{item_no}"),
        )
        identity = conn.execute(
            "SELECT id FROM identities WHERE person_id = ?", (person_id,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO participations(item_id, person_id, identity_id, role) "
            "VALUES (?, ?, ?, 'to')",
            (item_no, person_id, identity),
        )

    proposals = suggest_merges(conn, settings)
    assert proposals
    assert proposals[0].evidence["shared_count"] == 1
    assert "Declan Walsh" in proposals[0].evidence["shared_correspondents"]


# --- applying and undoing -------------------------------------------------


def test_merging_sets_merged_into_and_deletes_nothing(conn, settings):
    a = _person(conn, "Margaret O'Brien", "mobrien@contractmktg.com", item_count=10)
    b = _person(conn, "Margaret O'Brien", "margaret@eircom.net", item_count=3)

    apply_merge(conn, a, b)

    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 2, (
        "a people row is never deleted"
    )
    assert conn.execute("SELECT merged_into FROM people WHERE id=?", (b,)).fetchone()[0] == a
    assert conn.execute(
        "SELECT COUNT(*) FROM identities WHERE person_id = ?", (a,)
    ).fetchone()[0] == 2


def test_a_merge_can_always_be_undone(conn, settings):
    a = _person(conn, "Margaret O'Brien", "mobrien@contractmktg.com")
    b = _person(conn, "Margaret O'Brien", "margaret@eircom.net")

    apply_merge(conn, a, b)
    undo_merge(conn, b)

    assert conn.execute("SELECT merged_into FROM people WHERE id=?", (b,)).fetchone()[0] is None


def test_merging_someone_into_themselves_is_refused(conn, settings):
    a = _person(conn, "Tim", "tim@x.com")
    with pytest.raises(MergeError, match="into themselves"):
        apply_merge(conn, a, a)


def test_merging_an_unknown_person_is_refused(conn, settings):
    a = _person(conn, "Tim", "tim@x.com")
    with pytest.raises(MergeError, match="no person with id"):
        apply_merge(conn, a, 9999)


def test_merging_an_already_merged_person_is_refused(conn, settings):
    a = _person(conn, "A", "a@x.com")
    b = _person(conn, "B", "b@x.com")
    c = _person(conn, "C", "c@x.com")
    apply_merge(conn, a, b)
    with pytest.raises(MergeError, match="already been merged"):
        apply_merge(conn, c, b)


def test_undoing_a_merge_that_never_happened_is_refused(conn, settings):
    a = _person(conn, "Tim", "tim@x.com")
    with pytest.raises(MergeError, match="not been merged"):
        undo_merge(conn, a)


def test_resolve_person_follows_the_merge(conn, settings):
    a = _person(conn, "A", "a@x.com")
    b = _person(conn, "B", "b@x.com")
    apply_merge(conn, a, b)
    assert resolve_person(conn, b) == a


def test_merging_carries_is_self_forward(conn, settings):
    a = _person(conn, "Tim work", "tim@work.com")
    b = _person(conn, "Tim home", "tim@home.com")
    conn.execute("UPDATE people SET is_self = 1 WHERE id = ?", (b,))
    apply_merge(conn, a, b)
    assert conn.execute("SELECT is_self FROM people WHERE id=?", (a,)).fetchone()[0] == 1


def test_recount_all_is_derived_not_incremental(conn, settings):
    a = _person(conn, "Tim", "tim@x.com", item_count=999)
    conn.execute("INSERT INTO items(id, kind, dedup_key, occurred_utc) "
                 "VALUES (1, 'message', 'k1', '2003-01-01T00:00:00Z')")
    identity = conn.execute(
        "SELECT id FROM identities WHERE person_id = ?", (a,)
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO participations(item_id, person_id, identity_id, role) "
        "VALUES (1, ?, ?, 'from')",
        (a, identity),
    )
    recount_all(conn)
    row = conn.execute(
        "SELECT item_count, first_seen_utc FROM people WHERE id = ?", (a,)
    ).fetchone()
    assert row["item_count"] == 1, "a drifted count is worse than none"
    assert row["first_seen_utc"] == "2003-01-01T00:00:00Z"
