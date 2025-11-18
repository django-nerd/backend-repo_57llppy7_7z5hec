import os
from datetime import date, datetime
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import db, create_document, get_documents

app = FastAPI(title="Expense Tracker API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ExpenseCreate(BaseModel):
    date: date
    description: str
    amount: float
    kind: str  # 'debit' | 'credit'

class ExpenseUpdate(BaseModel):
    date: Optional[date] = None
    description: Optional[str] = None
    amount: Optional[float] = None
    kind: Optional[str] = None


def serialize_doc(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    # Convert datetime/date fields to ISO strings for JSON
    if isinstance(doc.get("date"), (datetime, date)):
        d = doc.get("date")
        doc["date"] = d.isoformat()
    for key in ["created_at", "updated_at"]:
        if isinstance(doc.get(key), (datetime, date)):
            doc[key] = doc[key].isoformat()
    return doc


@app.get("/")
def read_root():
    return {"message": "Expense Tracker Backend Running"}


@app.post("/api/expenses")
def create_expense(payload: ExpenseCreate):
    # Basic validation for kind
    if payload.kind not in ("debit", "credit"):
        raise HTTPException(status_code=422, detail="kind must be 'debit' or 'credit'")
    collection = "expense"
    # Use create_document helper (adds timestamps)
    _id = create_document(collection, payload.model_dump())
    return {"id": _id}


@app.get("/api/expenses")
def list_expenses(month: Optional[int] = None, year: Optional[int] = None):
    collection = "expense"
    query = {}
    # If month/year specified, filter by date's month/year using $expr
    if month or year:
        expr = {}
        if month:
            expr["$eq_month"] = month
        if year:
            expr["$eq_year"] = year
        # Since Mongo doesn't support $eq_month, build proper $expr
        expr_parts = []
        if month:
            expr_parts.append({"$eq": [{"$month": "$date"}, month]})
        if year:
            expr_parts.append({"$eq": [{"$year": "$date"}, year]})
        if expr_parts:
            query = {"$expr": {"$and": expr_parts}} if len(expr_parts) > 1 else {"$expr": expr_parts[0]}

    docs = get_documents(collection, query)
    return [serialize_doc(d) for d in docs]


@app.get("/api/summary")
def get_summary(month: Optional[int] = None, year: Optional[int] = None):
    collection = "expense"
    # Build aggregation pipeline for monthly totals and overall balance
    match_stage = {}
    expr_parts = []
    if month:
        expr_parts.append({"$eq": [{"$month": "$date"}, month]})
    if year:
        expr_parts.append({"$eq": [{"$year": "$date"}, year]})
    if expr_parts:
        match_stage = {"$match": {"$expr": {"$and": expr_parts}}}

    pipeline = []
    if match_stage:
        pipeline.append(match_stage)

    pipeline += [
        {
            "$group": {
                "_id": None,
                "total_debit": {
                    "$sum": {"$cond": [
                        {"$eq": ["$kind", "debit"]}, "$amount", 0
                    ]}
                },
                "total_credit": {
                    "$sum": {"$cond": [
                        {"$eq": ["$kind", "credit"]}, "$amount", 0
                    ]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "total_debit": 1,
                "total_credit": 1,
                "balance": {"$subtract": ["$total_credit", "$total_debit"]},
            }
        }
    ]

    try:
        result = list(db[collection].aggregate(pipeline))
        if not result:
            return {"total_debit": 0, "total_credit": 0, "balance": 0}
        return result[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/monthly-chart")
def monthly_chart(year: Optional[int] = None):
    """Return monthly totals per month for the requested year (defaults to current year)."""
    y = year or datetime.utcnow().year
    collection = "expense"
    pipeline = [
        {"$match": {"$expr": {"$eq": [{"$year": "$date"}, y]}}},
        {
            "$group": {
                "_id": {"month": {"$month": "$date"}},
                "debit": {"$sum": {"$cond": [{"$eq": ["$kind", "debit"]}, "$amount", 0]}},
                "credit": {"$sum": {"$cond": [{"$eq": ["$kind", "credit"]}, "$amount", 0]}},
            }
        },
        {"$project": {"_id": 0, "month": "$_id.month", "debit": 1, "credit": 1}},
        {"$sort": {"month": 1}},
    ]
    data = list(db[collection].aggregate(pipeline))
    # Ensure all months present
    month_map = {d["month"]: d for d in data}
    complete = []
    for m in range(1, 13):
        d = month_map.get(m, {"month": m, "debit": 0, "credit": 0})
        complete.append(d)
    return complete


@app.patch("/api/expenses/{expense_id}")
def update_expense(expense_id: str, payload: ExpenseUpdate):
    from bson import ObjectId

    updates = {k: v for k, v in payload.model_dump(exclude_none=True).items()}
    if not updates:
        return {"updated": False}
    updates["updated_at"] = datetime.utcnow()
    res = db["expense"].update_one({"_id": ObjectId(expense_id)}, {"$set": updates})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Expense not found")
    return {"updated": True}


@app.delete("/api/expenses/{expense_id}")
def delete_expense(expense_id: str):
    from bson import ObjectId

    res = db["expense"].delete_one({"_id": ObjectId(expense_id)})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Expense not found")
    return {"deleted": True}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }

    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"

            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"

    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    import os
    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"

    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
