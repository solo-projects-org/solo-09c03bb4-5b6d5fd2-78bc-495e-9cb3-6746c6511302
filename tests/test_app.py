import base64
import concurrent.futures
import csv
import io
import os
import re
import sqlite3

import pytest

from app import create_app, get_db

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": str(tmp_path / "test.db"),
            "UPLOAD_DIR": str(tmp_path / "uploads"),
        }
    )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def login(app, username):
    client = app.test_client()
    response = client.post(
        "/login", data={"username": username, "password": "demo1234"}
    )
    assert response.status_code in (302, 303)
    return client


def card_id(app, card_no):
    with app.app_context():
        row = get_db().execute(
            "SELECT id FROM cards WHERE card_no = ?", (card_no,)
        ).fetchone()
        return row["id"]


def get_export(app):
    reviewer = login(app, "reviewer")
    response = reviewer.get("/reviewer/export.csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.data.decode("utf-8"))))
    return response, rows


def review_form(app, card_no):
    reviewer = login(app, "reviewer")
    cid = card_id(app, card_no)
    page = reviewer.get(f"/reviewer/cards/{cid}").data.decode("utf-8")
    fields = dict(
        re.findall(
            r'name="(entry_[ab]_(?:id|revision))" value="(\d+)"', page
        )
    )
    assert len(fields) == 4
    return reviewer, cid, fields, page


def workbench_token(client):
    page = client.get("/reviewer").data.decode("utf-8")
    match = re.search(
        r'name="upload_form_token" value="([0-9a-f]{32})"', page
    )
    assert match, "upload form must carry an idempotency token"
    return match.group(1)


def upload_payload(token, image_bytes=PNG_1PX, **overrides):
    data = {
        "upload_form_token": token,
        "card_no": "UPLOAD-1",
        "clerk_a_id": "1",
        "clerk_b_id": "2",
        "scan": (io.BytesIO(image_bytes), "card.png"),
    }
    data.update(overrides)
    return data


def post_upload(client, token, image_bytes=PNG_1PX, **overrides):
    # A browser resend rebuilds the whole multipart body, so each POST gets its
    # own fresh file stream even though the form fields are identical.
    return client.post(
        "/reviewer/upload",
        data=upload_payload(token, image_bytes, **overrides),
        content_type="multipart/form-data",
    )


def all_card_rows(app):
    with app.app_context():
        return get_db().execute("SELECT * FROM cards ORDER BY id").fetchall()


def list_upload_files(app):
    upload_dir = app.config["UPLOAD_DIR"]
    return sorted(name for name in os.listdir(upload_dir) if os.path.isfile(os.path.join(upload_dir, name)))


def test_demo_login_and_roles(app):
    clerk = login(app, "clerk1")
    assert clerk.get("/clerk").status_code == 200
    assert clerk.get("/reviewer").status_code == 403

    reviewer = login(app, "reviewer")
    assert reviewer.get("/reviewer").status_code == 200
    assert reviewer.get("/clerk").status_code == 403

    anonymous = app.test_client()
    assert anonymous.get("/clerk").status_code == 302


def test_clerk_cannot_see_peer_entry(app):
    clerk = login(app, "clerk1")
    cid = card_id(app, "YC-1986-0715")
    page = clerk.get(f"/clerk/cards/{cid}").data.decode("utf-8")
    # The clerk sees their own 31.0 reading, but not clerk2's 13.0 reading.
    assert "31" in page
    assert ">13<" not in page
    assert "13 mm" not in page
    assert "录入员乙" not in page


def test_two_agreeing_entries_are_auto_approved(app):
    c1 = login(app, "clerk1")
    c2 = login(app, "clerk2")
    cid = card_id(app, "YC-1986-0714")
    for client, value in ((c1, "42.5"), (c2, "42.50")):
        response = client.post(
            f"/clerk/cards/{cid}",
            data={
                "station_no": "30501200",
                "obs_date": "1986-07-14",
                "rainfall_mm": value,
            },
        )
        assert response.status_code == 302

    _, rows = get_export(app)
    matching = [r for r in rows if r["card_no"] == "YC-1986-0714"]
    assert len(matching) == 1
    assert matching[0]["station_no"] == "30501200"
    assert matching[0]["observation_date"] == "1986-07-14"
    assert matching[0]["rainfall_mm"] == "42.5"
    assert matching[0]["verification"] == "double_entry_agreement"
    # One row per approved card.
    assert len({r["card_no"] for r in rows}) == len(rows)


def test_conflict_requires_review_and_uses_selected_version(app):
    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}

    reviewer, cid, fields, page = review_form(app, "YC-1986-0715")
    assert "31" in page and "13" in page
    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )
    assert response.status_code == 302

    _, rows = get_export(app)
    match = [r for r in rows if r["card_no"] == "YC-1986-0715"]
    assert len(match) == 1
    assert match[0]["rainfall_mm"] == "31"
    assert match[0]["verification"] == "reviewer_adjudication"

    # Any later save (even textually identical) revokes the award.
    c2 = login(app, "clerk2")
    response = c2.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "13.0",
        },
    )
    assert response.status_code == 302
    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}

    with app.app_context():
        decisions = get_db().execute(
            "SELECT * FROM decisions WHERE card_id = ?", (cid,)
        ).fetchall()
    assert len(decisions) == 1
    assert decisions[0]["voided_at"] is not None
    assert decisions[0]["voided_reason"] == "entry_edited"


def test_stale_adjudication_is_refused(app):
    reviewer, cid, old_fields, _ = review_form(app, "YC-1986-0715")
    clerk2 = login(app, "clerk2")
    clerk2.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "14",
        },
    )

    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**old_fields, "selected_entry_id": old_fields["entry_a_id"]},
    )
    assert response.status_code == 409
    assert "旧裁定已拒绝" in response.data.decode("utf-8")

    with app.app_context():
        count = get_db().execute(
            "SELECT COUNT(*) FROM decisions WHERE card_id = ?", (cid,)
        ).fetchone()[0]
    assert count == 0

    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}


def test_resolution_can_be_redone_after_edit(app):
    reviewer, cid, fields, _ = review_form(app, "YC-1986-0715")
    reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )

    c2 = login(app, "clerk2")
    c2.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "12.5",
        },
    )

    reviewer2, cid2, fresh, _ = review_form(app, "YC-1986-0715")
    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}

    response = reviewer2.post(
        f"/reviewer/cards/{cid2}/decide",
        data={**fresh, "selected_entry_id": fresh["entry_b_id"]},
    )
    assert response.status_code == 302
    _, rows = get_export(app)
    match = [r for r in rows if r["card_no"] == "YC-1986-0715"]
    assert len(match) == 1
    assert match[0]["rainfall_mm"] == "12.5"


def test_agreement_after_edit_wins_and_needs_no_adjudication(app):
    reviewer, cid, fields, _ = review_form(app, "YC-1986-0715")
    reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )

    c2 = login(app, "clerk2")
    c2.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "31.0",
        },
    )

    _, rows = get_export(app)
    match = [r for r in rows if r["card_no"] == "YC-1986-0715"]
    assert len(match) == 1
    assert match[0]["verification"] == "double_entry_agreement"
    assert match[0]["rainfall_mm"] == "31"
    # A decision against the pre-edit versions cannot be added now either.
    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )
    assert response.status_code == 409


def test_upload_creates_card_and_rejects_non_image(app):
    reviewer = login(app, "reviewer")
    token = workbench_token(reviewer)
    response = post_upload(reviewer, token)
    assert response.status_code == 302

    cid = card_id(app, "UPLOAD-1")
    assert reviewer.get(f"/cards/{cid}/image").status_code == 200
    assert reviewer.get(f"/reviewer/cards/{cid}").status_code == 200
    # Exactly one image file on disk for the one card.
    assert len(list_upload_files(app)) == 1

    response = reviewer.post(
        "/reviewer/upload",
        data=upload_payload(
            workbench_token(reviewer),
            image_bytes=b"<svg><script>alert(1)</script></svg>",
            card_no="BAD-1",
        ),
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    with app.app_context():
        assert (
            get_db()
            .execute("SELECT COUNT(*) FROM cards WHERE card_no = 'BAD-1'")
            .fetchone()[0]
            == 0
        )
    # The rejected scan leaves no orphan file behind.
    assert len(list_upload_files(app)) == 1


def test_upload_without_token_is_rejected_without_side_effects(app):
    reviewer = login(app, "reviewer")
    payload = upload_payload(workbench_token(reviewer))
    del payload["upload_form_token"]
    response = reviewer.post(
        "/reviewer/upload", data=payload, content_type="multipart/form-data"
    )
    assert response.status_code == 302
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 4
        assert get_db().execute("SELECT COUNT(*) FROM upload_requests").fetchone()[0] == 0
    assert list_upload_files(app) == []


def test_resending_the_same_form_returns_the_same_card(app):
    reviewer = login(app, "reviewer")
    token = workbench_token(reviewer)

    first = post_upload(reviewer, token)
    second = post_upload(reviewer, token)
    third = post_upload(reviewer, token)

    cid = card_id(app, "UPLOAD-1")
    for response in (first, second, third):
        assert response.status_code == 302
        assert response.headers["Location"].endswith(f"/reviewer/cards/{cid}")

    assert len(all_card_rows(app)) == 5  # four seed cards + one new card
    with app.app_context():
        requests = get_db().execute(
            "SELECT * FROM upload_requests WHERE form_token = ?", (token,)
        ).fetchall()
    assert len(requests) == 1
    assert requests[0]["card_id"] == cid
    # One original image only; the resends do not write another file.
    files = list_upload_files(app)
    assert files == [requests[0]["image_filename"]]
    assert reviewer.get(f"/cards/{cid}/image").status_code == 200


def test_blank_card_number_resend_still_deduplicates(app):
    reviewer = login(app, "reviewer")
    token = workbench_token(reviewer)

    first = post_upload(reviewer, token, card_no="")
    second = post_upload(reviewer, token, card_no="")
    assert first.status_code == second.status_code == 302
    assert first.headers["Location"] == second.headers["Location"]

    cards = [c for c in all_card_rows(app) if c["card_no"].startswith("YC-")]
    # The four seed cards also start with YC-, so the new one is the fifth.
    assert len(cards) == 5
    assert len(list_upload_files(app)) == 1


def test_concurrent_duplicate_submits_create_one_card(app):
    reviewer = login(app, "reviewer")
    token = workbench_token(reviewer)

    # Separate clients sharing one reviewer session cookie, all firing the same
    # form at once. BEGIN IMMEDIATE serialises them inside the app.
    session_cookie = reviewer.get_cookie("session")

    def fire(_):
        client = app.test_client()
        client.set_cookie(
            session_cookie.key,
            session_cookie.value,
            domain=session_cookie.domain or "localhost",
        )
        return post_upload(client, token)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(fire, range(4)))

    cid = card_id(app, "UPLOAD-1")
    assert all(r.status_code == 302 for r in responses)
    assert all(
        r.headers["Location"].endswith(f"/reviewer/cards/{cid}") for r in responses
    )
    assert len([c for c in all_card_rows(app) if c["card_no"] == "UPLOAD-1"]) == 1
    with app.app_context():
        assert (
            get_db()
            .execute("SELECT COUNT(*) FROM upload_requests WHERE form_token = ?", (token,))
            .fetchone()[0]
            == 1
        )
    assert len(list_upload_files(app)) == 1


def test_idempotent_mapping_survives_app_restart(tmp_path):
    db_path = tmp_path / "restart.db"
    upload_dir = tmp_path / "uploads"
    config = {
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "DATABASE": str(db_path),
        "UPLOAD_DIR": str(upload_dir),
    }
    first_app = create_app(dict(config))
    reviewer = login(first_app, "reviewer")
    token = workbench_token(reviewer)
    response = post_upload(reviewer, token)
    assert response.status_code == 302
    cid = card_id(first_app, "UPLOAD-1")

    # Simulate a service restart: a brand-new app object on the same volume.
    second_app = create_app(dict(config))
    reviewer2 = login(second_app, "reviewer")
    retry = post_upload(reviewer2, token)
    assert retry.status_code == 302
    assert retry.headers["Location"].endswith(f"/reviewer/cards/{cid}")
    with second_app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 5
        assert (
            get_db()
            .execute("SELECT COUNT(*) FROM upload_requests WHERE form_token = ?", (token,))
            .fetchone()[0]
            == 1
        )
    assert len(os.listdir(upload_dir)) == 1
    assert reviewer2.get(f"/cards/{cid}/image").status_code == 200


def test_modified_resubmit_is_told_to_open_a_new_form(app):
    reviewer = login(app, "reviewer")
    token = workbench_token(reviewer)
    response = post_upload(reviewer, token)
    cid = card_id(app, "UPLOAD-1")
    assert response.headers["Location"].endswith(f"/reviewer/cards/{cid}")

    # Same token, but the card number changed: block it, keep the original card.
    response = post_upload(reviewer, token, card_no="UPLOAD-CHANGED")
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/reviewer/cards/{cid}")
    with app.app_context():
        assert get_db().execute(
            "SELECT COUNT(*) FROM cards WHERE card_no = 'UPLOAD-CHANGED'"
        ).fetchone()[0] == 0
    # The rejected modified attempt must not leave a second image behind.
    assert len(list_upload_files(app)) == 1

    # Changing the clerk split is likewise rejected.
    response = post_upload(reviewer, token, clerk_b_id="1", clerk_a_id="2")
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/reviewer/cards/{cid}")
    assert len(list_upload_files(app)) == 1

    # Different image bytes count as a changed form too.
    response = post_upload(
        reviewer,
        token,
        image_bytes=base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAEklEQVR42mP8z8BQz0AEYBxVSF0GAEWiAw8H+gN8AAAAAElFTkSuQmCC"
        ),
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/reviewer/cards/{cid}")
    assert len(list_upload_files(app)) == 1
    assert len(all_card_rows(app)) == 5


def test_a_fresh_form_still_creates_a_new_card(app):
    reviewer = login(app, "reviewer")
    first_token = workbench_token(reviewer)
    post_upload(reviewer, first_token, card_no="FRESH-1")

    second_token = workbench_token(reviewer)  # reopening the workbench mints a new token
    response = post_upload(reviewer, second_token, card_no="FRESH-2")
    assert response.status_code == 302
    assert card_id(app, "FRESH-1") != card_id(app, "FRESH-2")
    assert len(list_upload_files(app)) == 2


def test_failed_card_creation_leaves_nothing_behind(app):
    reviewer = login(app, "reviewer")
    # A duplicate card number violates the UNIQUE constraint inside the
    # transaction; the saved image must be rolled back as well.
    token = workbench_token(reviewer)
    response = post_upload(reviewer, token, card_no="YC-1986-0714")
    assert response.status_code == 302
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM upload_requests").fetchone()[0] == 0
        # The seed card count is unchanged.
        assert get_db().execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 4
    assert list_upload_files(app) == []

    # The same form can be retried immediately with a corrected number.
    response = post_upload(reviewer, token, card_no="RETRY-1")
    assert response.status_code == 302
    cid = card_id(app, "RETRY-1")
    assert response.headers["Location"].endswith(f"/reviewer/cards/{cid}")
    assert reviewer.get(f"/cards/{cid}/image").status_code == 200
    assert len(list_upload_files(app)) == 1


def test_startup_removes_orphan_image_files(tmp_path):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    orphan = upload_dir / "orphan.png"
    orphan.write_bytes(PNG_1PX)

    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": str(tmp_path / "test.db"),
            "UPLOAD_DIR": str(upload_dir),
        }
    )
    assert not orphan.exists()
    # And a normal upload afterwards works and survives another startup.
    reviewer = login(application, "reviewer")
    token = workbench_token(reviewer)
    reviewer.post(
        "/reviewer/upload",
        data=upload_payload(token, card_no="KEEP-1"),
        content_type="multipart/form-data",
    )
    kept = list_upload_files(application)
    assert len(kept) == 1
    create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": str(tmp_path / "test.db"),
            "UPLOAD_DIR": str(upload_dir),
        }
    )
    assert sorted(os.listdir(upload_dir)) == kept


def test_demo_scan_is_a_real_png_only_for_reviewer(app):
    reviewer = login(app, "reviewer")
    response = reviewer.get("/reviewer/demo-scan")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert "attachment" in response.headers.get("Content-Disposition", "")

    clerk = login(app, "clerk1")
    assert clerk.get("/reviewer/demo-scan").status_code == 403
    anonymous = app.test_client()
    assert anonymous.get("/reviewer/demo-scan").status_code == 302


def test_rainfall_and_date_validation(app):
    clerk = login(app, "clerk1")
    cid = card_id(app, "YC-1986-0714")
    base = {"station_no": "30501200", "obs_date": "1986-07-14"}
    for value in ("-1", "abc", "", "NaN", "100001"):
        response = clerk.post(
            f"/clerk/cards/{cid}",
            data={**base, "rainfall_mm": value},
        )
        assert response.status_code == 200
        assert "雨量" in response.data.decode("utf-8")
    response = clerk.post(
        f"/clerk/cards/{cid}",
        data={"station_no": "30501200", "obs_date": "not-a-date", "rainfall_mm": "4"},
    )
    assert response.status_code == 200
    with app.app_context():
        entry = get_db().execute(
            "SELECT * FROM entries WHERE card_id = ? AND clerk_id = 1", (cid,)
        ).fetchone()
    assert entry is None


def test_csv_is_reviewer_only(app):
    clerk = login(app, "clerk1")
    assert clerk.get("/reviewer/export.csv").status_code == 403
    anonymous = app.test_client()
    assert anonymous.get("/reviewer/export.csv").status_code == 302


def test_decision_requires_both_entries_and_current_versions(app):
    reviewer = login(app, "reviewer")
    cid = card_id(app, "YC-1986-0717")  # only clerk1 has entered
    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={
            "entry_a_id": "1",
            "entry_a_revision": "1",
            "entry_b_id": "2",
            "entry_b_revision": "1",
            "selected_entry_id": "1",
        },
    )
    assert response.status_code == 409
    with app.app_context():
        assert (
            get_db()
            .execute("SELECT COUNT(*) FROM decisions WHERE card_id = ?", (cid,))
            .fetchone()[0]
            == 0
        )
