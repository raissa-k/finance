import json

from app.obligation_match import find_matching_subsets


class _FakeTx:
    def __init__(self, tid, amount):
        self.transaction_id = tid
        self.amount = amount


def test_find_matching_subsets_exact_combo_wins():
    # 100 + 1000 + 400 == 1500, with decoys that should score worse.
    candidates = [_FakeTx(1, -100.0), _FakeTx(2, -1000.0), _FakeTx(3, -400.0), _FakeTx(4, -250.0)]
    results = find_matching_subsets(candidates, target_amount=1500.0)
    assert results
    best = results[0]
    assert set(best["transaction_ids"]) == {1, 2, 3}
    assert best["difference"] < 0.01


def test_find_matching_subsets_no_target_returns_empty():
    assert find_matching_subsets([_FakeTx(1, -100.0)], target_amount=None) == []


def test_find_matching_subsets_no_candidates_returns_empty():
    assert find_matching_subsets([], target_amount=100.0) == []


def _create_category(client, name):
    res = client.post("/api/accounts/categories/", json={"name": name})
    assert res.status_code == 201
    return res.json()["category_id"]


def test_suggest_matches_route_without_ai_provider(client):
    cat_id = _create_category(client, "MatchCat")
    res = client.post(
        "/api/obligations/",
        json={"name": "Match Bill", "category_id": cat_id, "estimated_amount": 500.0, "first_due_date": "2026-11-01"},
    )
    occ_id = res.json()["occurrences"][0]["obligation_occurrence_id"]

    res_sm = client.get(f"/api/obligation-occurrences/{occ_id}/suggest-matches/")
    assert res_sm.status_code == 200
    data = res_sm.json()
    assert data["ai_explanation"] is None
    assert "candidates" in data
    assert "suggested_single" in data
    assert "suggested_combinations" in data


def test_suggest_categories_requires_ai_provider(client):
    res = client.post(
        "/api/obligations/ai/suggest-categories/",
        json={"obligations": [{"index": 0, "name": "Some Bill", "note": None}]},
    )
    assert res.status_code == 400


def test_match_categories_requires_ai_provider(client):
    res = client.post("/api/obligations/ai/match-categories/", json={"labels": ["Faculdade"]})
    assert res.status_code == 400


# ── Auto-match-on-import (app.obligation_match.auto_match_occurrences) ────────

_IMPORT_FMT = json.dumps(
    {
        "file_type": "csv",
        "header_row": 1,
        "fields": [
            {"target_field": "name", "source_column": "Name"},
            {"target_field": "amount", "source_column": "Amount"},
            {"target_field": "due_date", "source_column": "DueDate"},
        ],
    }
)

_IMPORT_FMT_WITH_DIRECTION = json.dumps(
    {
        "file_type": "csv",
        "header_row": 1,
        "fields": [
            {"target_field": "name", "source_column": "Name"},
            {"target_field": "amount", "source_column": "Amount"},
            {"target_field": "due_date", "source_column": "DueDate"},
            {"target_field": "direction", "source_column": "Type"},
        ],
    }
)


def _setup_account_and_payee(client):
    res = client.post("/api/accounts/titulars/", json={"name": "Primary"})
    tit_id = res.json()["titular_id"]
    res = client.post(
        "/api/accounts/currencies/", json={"name": "Real", "iso_code": "BRL", "symbol": "R$", "order": 1}
    )
    cur_id = res.json()["currency_id"]
    res = client.post("/api/accounts/account-types/", json={"name": "Checking", "code": 10})
    type_id = res.json()["account_type_id"]
    res = client.post(
        "/api/accounts/",
        json={
            "name": "Main",
            "titular_id": tit_id,
            "currency_id": cur_id,
            "account_type_id": type_id,
            "entry": "2026-01-01",
            "initial_balance": 0.0,
        },
    )
    acc_id = res.json()["account_id"]
    res = client.post("/api/accounts/payees/", json={"name": "Some Payee", "comment": ""})
    payee_id = res.json()["payee_id"]
    return acc_id, payee_id


def _create_tx(client, acc_id, payee_id, amount, cash_date, transaction_type="withdrawal"):
    res = client.post(
        "/api/transactions/",
        json={
            "accountId": acc_id,
            "amount": amount,
            "transactionType": transaction_type,
            "payee_id": payee_id,
            "cash": cash_date,
        },
    )
    assert res.status_code == 201
    return res.json()["transaction_id"]


def test_import_apply_auto_matches_unambiguous_expense_transaction(client):
    acc_id, payee_id = _setup_account_and_payee(client)
    _create_tx(client, acc_id, payee_id, 80.0, "2026-08-04", "withdrawal")

    csv_content = "Name,Amount,DueDate\nWater,80,2026-08-05\n".encode("utf-8")
    res = client.post(
        "/api/obligation-import/apply/",
        files={"file": ("water.csv", csv_content, "text/csv")},
        data={"format_json": _IMPORT_FMT},
    )
    assert res.status_code == 200
    assert res.json()["auto_matched_transactions"] == 1

    ob_id = res.json()["obligation_ids"][0]
    occ = client.get(f"/api/obligations/{ob_id}/").json()["occurrences"][0]
    assert occ["assigned_transaction_count"] == 1
    assert occ["assigned_total"] == 80.0


def test_import_apply_auto_matches_unambiguous_income_transaction_for_salary(client):
    # A receivable (e.g. salary) occurrence must match an incoming
    # (positive-amount) deposit, not an outgoing one -- driven by the "Type"
    # column mapped to "direction", translated ("Receita" -> receivable).
    acc_id, payee_id = _setup_account_and_payee(client)
    _create_tx(client, acc_id, payee_id, 3000.0, "2026-08-04", "deposit")

    csv_content = "Name,Amount,DueDate,Type\nSalary,3000,2026-08-05,Receita\n".encode("utf-8")
    res = client.post(
        "/api/obligation-import/apply/",
        files={"file": ("salary.csv", csv_content, "text/csv")},
        data={"format_json": _IMPORT_FMT_WITH_DIRECTION},
    )
    assert res.status_code == 200
    assert res.json()["auto_matched_transactions"] == 1

    ob_id = res.json()["obligation_ids"][0]
    ob = client.get(f"/api/obligations/{ob_id}/").json()
    assert ob["direction"] == "receivable"
    occ = ob["occurrences"][0]
    assert occ["assigned_transaction_count"] == 1
    assert occ["assigned_total"] == 3000.0


def test_receivable_only_matches_incoming_not_outgoing_transactions(client):
    acc_id, payee_id = _setup_account_and_payee(client)
    _create_tx(client, acc_id, payee_id, 500.0, "2026-08-04", "withdrawal")  # wrong sign for a receivable
    _create_tx(client, acc_id, payee_id, 500.0, "2026-08-04", "deposit")  # right sign

    csv_content = "Name,Amount,DueDate,Type\nBonus,500,2026-08-05,Receita\n".encode("utf-8")
    res = client.post(
        "/api/obligation-import/apply/",
        files={"file": ("bonus.csv", csv_content, "text/csv")},
        data={"format_json": _IMPORT_FMT_WITH_DIRECTION},
    )
    assert res.json()["auto_matched_transactions"] == 1
    ob_id = res.json()["obligation_ids"][0]
    occ_id = client.get(f"/api/obligations/{ob_id}/").json()["occurrences"][0]["obligation_occurrence_id"]
    assigned = client.get(f"/api/obligation-occurrences/{occ_id}/assigned-transactions/").json()["results"]
    assert len(assigned) == 1
    assert assigned[0]["amount"] > 0  # picked the deposit, never the withdrawal


def test_payable_only_matches_outgoing_not_incoming_transactions(client):
    acc_id, payee_id = _setup_account_and_payee(client)
    _create_tx(client, acc_id, payee_id, 200.0, "2026-08-04", "deposit")  # wrong sign for a payable
    _create_tx(client, acc_id, payee_id, 200.0, "2026-08-04", "withdrawal")  # right sign

    csv_content = "Name,Amount,DueDate\nElectric,200,2026-08-05\n".encode("utf-8")
    res = client.post(
        "/api/obligation-import/apply/",
        files={"file": ("electric.csv", csv_content, "text/csv")},
        data={"format_json": _IMPORT_FMT},
    )
    assert res.json()["auto_matched_transactions"] == 1
    ob_id = res.json()["obligation_ids"][0]
    occ_id = client.get(f"/api/obligations/{ob_id}/").json()["occurrences"][0]["obligation_occurrence_id"]
    assigned = client.get(f"/api/obligation-occurrences/{occ_id}/assigned-transactions/").json()["results"]
    assert len(assigned) == 1
    assert assigned[0]["amount"] < 0  # picked the withdrawal, never the deposit


def test_import_apply_does_not_auto_match_ambiguous_candidates(client):
    acc_id, payee_id = _setup_account_and_payee(client)
    _create_tx(client, acc_id, payee_id, 50.0, "2026-08-04", "withdrawal")
    _create_tx(client, acc_id, payee_id, 50.0, "2026-08-06", "withdrawal")

    csv_content = "Name,Amount,DueDate\nGym,50,2026-08-05\n".encode("utf-8")
    res = client.post(
        "/api/obligation-import/apply/",
        files={"file": ("gym.csv", csv_content, "text/csv")},
        data={"format_json": _IMPORT_FMT},
    )
    assert res.status_code == 200
    assert res.json()["auto_matched_transactions"] == 0

    ob_id = res.json()["obligation_ids"][0]
    occ = client.get(f"/api/obligations/{ob_id}/").json()["occurrences"][0]
    assert occ["assigned_transaction_count"] == 0
