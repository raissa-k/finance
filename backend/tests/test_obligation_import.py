import json
import os


def _read_fixture():
    path = os.path.join(os.path.dirname(__file__), "fixtures", "obligations_sample.csv")
    with open(path, "rb") as f:
        return f.read()


_FORMAT_JSON = json.dumps(
    {
        "file_type": "csv",
        "header_row": 1,
        "default_recurrence": "monthly",
        "fields": [
            {"target_field": "name", "source_column": "Name"},
            {"target_field": "amount", "source_column": "Amount"},
            {"target_field": "due_date", "source_column": "DueDate"},
            {"target_field": "category", "source_column": "Category"},
        ],
    }
)


def test_analyze_upload(client):
    res = client.post(
        "/api/obligation-import/analyze/",
        files={"file": ("obligations_sample.csv", _read_fixture(), "text/csv")},
        data={"file_type": "csv"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "Name" in data["headers"]
    assert "name" in data["target_fields"]


def test_preview_upload(client):
    res = client.post(
        "/api/obligation-import/preview/",
        files={"file": ("obligations_sample.csv", _read_fixture(), "text/csv")},
        data={"format_json": _FORMAT_JSON},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["new"] == 2
    assert data["summary"]["duplicates"] == 0
    assert "Housing" in data["unmatched_categories"]


def test_apply_upload_creates_obligations_then_flags_reimport_as_duplicates(client):
    res = client.post(
        "/api/obligation-import/apply/",
        files={"file": ("obligations_sample.csv", _read_fixture(), "text/csv")},
        data={"format_json": _FORMAT_JSON},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["created"] == 2
    assert data["blocked"] == 0
    assert len(data["obligation_ids"]) == 2

    res2 = client.post(
        "/api/obligation-import/apply/",
        files={"file": ("obligations_sample.csv", _read_fixture(), "text/csv")},
        data={"format_json": _FORMAT_JSON},
    )
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["created"] == 0
    assert data2["blocked"] == 2


def test_preview_groups_same_bill_rows_into_one_obligation(client):
    # A spreadsheet with one row per month for the same recurring bill (the
    # real-world shape that broke the old per-row-is-its-own-obligation logic).
    csv_content = (
        "Name,Amount,DueDate,Category\n"
        "Aluguel,1800,2026-08-15,Moradia\n"
        "Aluguel,1800,2026-09-15,Moradia\n"
        "Aluguel,1800,2026-10-15,Moradia\n"
    ).encode("utf-8")
    res = client.post(
        "/api/obligation-import/preview/",
        files={"file": ("rent.csv", csv_content, "text/csv")},
        data={"format_json": _FORMAT_JSON},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["new"] == 1  # one obligation group, not three
    assert data["summary"]["duplicates"] == 0
    assert len(data["rows"]) == 3  # still one preview row per occurrence
    assert all(not r["is_duplicate"] for r in data["rows"])
    assert all(r["is_recurring"] for r in data["rows"])


def test_apply_groups_same_bill_rows_into_one_obligation_with_occurrences(client):
    csv_content = (
        "Name,Amount,DueDate,Category\n"
        "Aluguel,1800,2026-08-15,Moradia\n"
        "Aluguel,1800,2026-09-15,Moradia\n"
        "Aluguel,1800,2026-10-15,Moradia\n"
    ).encode("utf-8")
    res = client.post(
        "/api/obligation-import/apply/",
        files={"file": ("rent.csv", csv_content, "text/csv")},
        data={"format_json": _FORMAT_JSON},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["created"] == 1
    assert data["blocked"] == 0
    assert len(data["obligation_ids"]) == 1

    ob_id = data["obligation_ids"][0]
    res_get = client.get(f"/api/obligations/{ob_id}/")
    ob = res_get.json()
    assert ob["is_recurring"] is True
    assert ob["recurrence"] == "monthly"
    assert ob["occurrence_count"] == 3
    assert not ob["is_blocked"]
    due_dates = sorted(o["due_date"] for o in ob["occurrences"])
    assert due_dates == ["2026-08-15", "2026-09-15", "2026-10-15"]
    assert all(not o["is_blocked"] for o in ob["occurrences"])


def test_apply_upload_persists_note_per_occurrence(client):
    csv_content = (
        "Name,Amount,DueDate,Category,Note\n"
        "Water,80,2026-08-05,Utilities,Estimated from last year\n"
    ).encode("utf-8")
    fmt = json.dumps(
        {
            "file_type": "csv",
            "header_row": 1,
            "fields": [
                {"target_field": "name", "source_column": "Name"},
                {"target_field": "amount", "source_column": "Amount"},
                {"target_field": "due_date", "source_column": "DueDate"},
                {"target_field": "category", "source_column": "Category"},
                {"target_field": "note", "source_column": "Note"},
            ],
        }
    )
    res = client.post(
        "/api/obligation-import/apply/",
        files={"file": ("water.csv", csv_content, "text/csv")},
        data={"format_json": fmt},
    )
    assert res.status_code == 200
    ob_id = res.json()["obligation_ids"][0]

    res_ob = client.get(f"/api/obligations/{ob_id}/")
    occ = res_ob.json()["occurrences"][0]
    assert occ["note"] == "Estimated from last year"


def test_apply_defaults_paid_date_to_due_date_only_when_paid(client):
    # Backfilled historical rows: a row already marked paid defaults its
    # paid_date to its own due_date (there's no real "today" to use for
    # historical data); an unpaid row stays blank.
    csv_content = (
        "Name,Amount,DueDate,Paid\n"
        "Rent,1800,2026-08-05,yes\n"
        "Rent,1800,2026-09-05,\n"
    ).encode("utf-8")
    fmt = json.dumps(
        {
            "file_type": "csv",
            "header_row": 1,
            "fields": [
                {"target_field": "name", "source_column": "Name"},
                {"target_field": "amount", "source_column": "Amount"},
                {"target_field": "due_date", "source_column": "DueDate"},
                {"target_field": "paid", "source_column": "Paid"},
            ],
        }
    )
    res = client.post(
        "/api/obligation-import/apply/",
        files={"file": ("rent.csv", csv_content, "text/csv")},
        data={"format_json": fmt},
    )
    assert res.status_code == 200
    ob_id = res.json()["obligation_ids"][0]

    occs = {o["due_date"]: o for o in client.get(f"/api/obligations/{ob_id}/").json()["occurrences"]}
    assert occs["2026-08-05"]["paid"] is True
    assert occs["2026-08-05"]["paid_date"] == "2026-08-05"
    assert occs["2026-09-05"]["paid"] is False
    assert occs["2026-09-05"]["paid_date"] is None


def test_apply_with_ai_new_category_resolution_creates_translated_category(client):
    # Simulates the "Preview with AI" flow: AI found nothing existing that
    # fits "Adiantamento" (a salary advance) and proposed a new, translated
    # category under the existing "Income" top-level category, instead of the
    # raw untranslated label.
    income_id = _create_category(client, "Income")

    csv_content = "Name,Amount,DueDate,Category\nAdiantamento,300,2026-08-05,Adiantamento\n".encode("utf-8")
    res_preview = client.post(
        "/api/obligation-import/preview/",
        files={"file": ("adiant.csv", csv_content, "text/csv")},
        data={"format_json": _FORMAT_JSON},
    )
    row = res_preview.json()["rows"][0]

    resolutions = json.dumps(
        {str(row["occurrence_of_row"]): {"category_name": "Salary Advance", "category_parent": "Income"}}
    )
    res_apply = client.post(
        "/api/obligation-import/apply/",
        files={"file": ("adiant.csv", csv_content, "text/csv")},
        data={"format_json": _FORMAT_JSON, "resolutions": resolutions},
    )
    assert res_apply.status_code == 200
    ob_id = res_apply.json()["obligation_ids"][0]

    res_ob = client.get(f"/api/obligations/{ob_id}/")
    ob = res_ob.json()
    assert ob["category_name"] == "Salary Advance"

    res_cats = client.get("/api/accounts/categories/")
    by_name = {c["name"]: c for c in res_cats.json()["results"]}
    assert "Salary Advance" in by_name
    assert by_name["Salary Advance"]["parent_category_id"] == income_id
    assert "Adiantamento" not in by_name  # translated name used, not the raw literal text


def _create_category(client, name):
    res = client.post("/api/accounts/categories/", json={"name": name})
    assert res.status_code == 201
    return res.json()["category_id"]


def test_format_crud_roundtrip(client):
    res = client.post(
        "/api/obligation-import/formats/",
        json={
            "name": "My Format",
            "file_type": "csv",
            "header_row": 1,
            "decimal_separator": ".",
            "default_recurrence": "monthly",
            "fields": [
                {"target_field": "name", "source_column": "Name"},
                {"target_field": "amount", "source_column": "Amount"},
            ],
        },
    )
    assert res.status_code == 201
    fmt_id = res.json()["obligation_import_format_id"]
    assert len(res.json()["fields"]) == 2

    res_get = client.get(f"/api/obligation-import/formats/{fmt_id}/")
    assert res_get.status_code == 200

    res_put = client.put(
        f"/api/obligation-import/formats/{fmt_id}/",
        json={
            "name": "My Format v2",
            "file_type": "csv",
            "header_row": 1,
            "decimal_separator": ".",
            "fields": [{"target_field": "name", "source_column": "Name"}],
        },
    )
    assert res_put.status_code == 200
    assert res_put.json()["name"] == "My Format v2"
    assert len(res_put.json()["fields"]) == 1

    res_del = client.delete(f"/api/obligation-import/formats/{fmt_id}/")
    assert res_del.status_code == 204
