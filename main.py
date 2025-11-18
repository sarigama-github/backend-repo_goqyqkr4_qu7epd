import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Literal, Optional, Dict
from datetime import datetime

from database import db, create_document, get_documents

app = FastAPI(title="Leverage DEX API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {"message": "Leverage DEX Backend Running"}


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


# ---------------------------
# Price feed (mock) endpoint
# ---------------------------
SUPPORTED_ASSETS = ["BTC", "ETH", "SOL", "BNB", "ADA"]

# In production this would read from on-chain or an oracle network
PRICES: Dict[str, float] = {
    "BTC": 70000.0,
    "ETH": 3500.0,
    "SOL": 150.0,
    "BNB": 620.0,
    "ADA": 0.6,
}


@app.get("/api/prices")
def get_prices():
    return {"prices": PRICES, "timestamp": datetime.utcnow().isoformat()}


# ---------------------------
# Leverage/positions endpoints
# ---------------------------
class OpenPositionRequest(BaseModel):
    wallet: str
    symbol: Literal["BTC", "ETH", "SOL", "BNB", "ADA"]
    side: Literal["long", "short"]
    leverage: int
    margin_usd: float


class ClosePositionRequest(BaseModel):
    position_id: str
    wallet: str


def compute_liquidation_price(symbol: str, side: str, entry_price: float, leverage: int) -> float:
    # Simplified cross margin liquidation formula: liq at ~ (entry * (1 - 1/leverage)) for long
    # and (entry * (1 + 1/leverage)) for short, ignoring fees/funding.
    if leverage <= 0:
        raise HTTPException(status_code=400, detail="Leverage must be positive")
    if side == "long":
        return round(entry_price * (1 - 1 / leverage), 4)
    else:
        return round(entry_price * (1 + 1 / leverage), 4)


@app.post("/api/positions/open")
def open_position(req: OpenPositionRequest):
    if req.symbol not in SUPPORTED_ASSETS:
        raise HTTPException(status_code=400, detail="Unsupported asset")
    price = PRICES.get(req.symbol)
    if not price:
        raise HTTPException(status_code=400, detail="Price unavailable")
    if req.leverage < 1 or req.leverage > 100:
        raise HTTPException(status_code=400, detail="Leverage out of bounds")
    if req.margin_usd <= 0:
        raise HTTPException(status_code=400, detail="Margin must be > 0")

    notional = req.margin_usd * req.leverage
    size = round(notional / price, 8)
    liq = compute_liquidation_price(req.symbol, req.side, price, req.leverage)

    position_doc = {
        "wallet": req.wallet,
        "symbol": req.symbol,
        "side": req.side,
        "leverage": req.leverage,
        "size_usd": round(notional, 2),
        "entry_price": price,
        "margin_usd": req.margin_usd,
        "size": size,
        "liquidation_price": liq,
        "status": "open",
    }

    position_id = create_document("position", position_doc)

    trade_doc = {
        "position_id": position_id,
        "wallet": req.wallet,
        "symbol": req.symbol,
        "side": req.side,
        "size_usd": round(notional, 2),
        "price": price,
        "fee_usd": round(notional * 0.0008, 4),
        "type": "open",
    }
    _ = create_document("trade", trade_doc)

    return {"position_id": position_id, "position": position_doc}


@app.get("/api/positions")
def list_positions(wallet: Optional[str] = None):
    filt = {"status": "open"}
    if wallet:
        filt["wallet"] = wallet
    positions = get_documents("position", filt, limit=100)

    # Convert ObjectId to string and remove internal fields for safety in UI
    def sanitize(doc):
        d = {k: v for k, v in doc.items() if k != "_id"}
        d["id"] = str(doc.get("_id"))
        return d

    return {"positions": [sanitize(p) for p in positions]}


@app.post("/api/positions/close")
def close_position(req: ClosePositionRequest):
    from bson import ObjectId

    # Fetch existing position
    pos = db.position.find_one({"_id": ObjectId(req.position_id), "wallet": req.wallet, "status": "open"})
    if not pos:
        raise HTTPException(status_code=404, detail="Position not found")

    price = PRICES.get(pos["symbol"]) or pos["entry_price"]

    pnl = (price - pos["entry_price"]) * (pos["size_usd"] / pos["entry_price"])  # approx
    if pos["side"] == "short":
        pnl = -pnl

    fee = pos["size_usd"] * 0.0008

    # Update position
    db.position.update_one({"_id": ObjectId(req.position_id)}, {"$set": {"status": "closed", "exit_price": price, "pnl_usd": round(pnl - fee, 4)}})

    trade_doc = {
        "position_id": req.position_id,
        "wallet": req.wallet,
        "symbol": pos["symbol"],
        "side": pos["side"],
        "size_usd": pos["size_usd"],
        "price": price,
        "fee_usd": round(fee, 4),
        "type": "close",
    }
    _ = create_document("trade", trade_doc)

    return {"ok": True, "exit_price": price, "pnl_usd": round(pnl - fee, 4)}


# Expose schemas for viewer tools (optional helper)
@app.get("/schema")
def get_schema_info():
    from schemas import User, Product, Market, Position, Trade
    return {
        "collections": [
            {"name": "user", "model": User.model_json_schema()},
            {"name": "product", "model": Product.model_json_schema()},
            {"name": "market", "model": Market.model_json_schema()},
            {"name": "position", "model": Position.model_json_schema()},
            {"name": "trade", "model": Trade.model_json_schema()},
        ]
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
