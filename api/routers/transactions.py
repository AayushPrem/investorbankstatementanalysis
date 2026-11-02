"""PATCH /v1/transactions/{id}/category: manual review override."""
from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import update

from api.deps import SessionDep
from api.errors import not_found
from api.routers.runs import txn_out
from api.schemas import CategoryOverride, TransactionOut
from db.models import Categorisation, Transaction

router = APIRouter(prefix="/v1/transactions", tags=["transactions"])


@router.patch("/{txn_id}/category", response_model=TransactionOut)
def override_category(txn_id: uuid.UUID, body: CategoryOverride, session: SessionDep) -> TransactionOut:
    """Record a manual category. History is kept; only the new row is current."""
    txn = session.get(Transaction, txn_id)
    if txn is None:
        raise not_found("transaction", txn_id)
    session.execute(
        update(Categorisation)
        .where(Categorisation.transaction_id == txn_id, Categorisation.is_current)
        .values(is_current=False)
    )
    session.flush()
    manual = Categorisation(
        transaction_id=txn_id, category=str(body.category), source="manual",
        confidence=1.0, is_current=True,
    )
    session.add(manual)
    session.flush()
    return txn_out(txn, manual)
