from broker.kotak.mapping.transform_data import map_exchange
from database.token_db import get_oa_symbol, get_symbol
from utils.logging import get_logger

logger = get_logger(__name__)


def _as_int(value):
    """Tolerant int for Kotak's string-typed numbers ("75", "75.0", "", None)."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _book_rows(reply, what):
    """Normalise a Kotak book reply to a list of rows, or [] when unreadable.

    A FAILED read is a bare {"stat": "Not_Ok", "emsg": ...} dict with no "data"
    key (07-portfolio-positions.md §5, 11-order-reports.md §5.1). Indexing
    ["data"] on that raised KeyError, and iterating the dict yielded its string
    KEYS - so `order["trnsTp"]` raised TypeError. Both surfaced as a generic
    HTTP 500 from the service layer, which reads as "the server broke" rather
    than "the broker said no".

    This is the display path, so an unreadable book degrades to an empty list
    rather than raising; the trading paths use
    api/order_api.py#_read_book, which raises so a decision is never made on a
    book it could not see.
    """
    if reply is None:
        logger.warning(f"{what}: no reply from broker")
        return []
    if isinstance(reply, list):
        return [row for row in reply if isinstance(row, dict)]
    if isinstance(reply, dict):
        if reply.get("stat") == "Not_Ok":
            logger.warning(
                f"{what}: broker error ({reply.get('emsg') or reply.get('message')})"
            )
            return []
        data = reply.get("data")
        if data is None:
            logger.debug(f"{what}: no data available")
            return []
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            return [data]
        return []
    logger.warning(f"{what}: unexpected reply type {type(reply).__name__}")
    return []


def _openalgo_symbol(row, exchange):
    """Resolve one Kotak row to its OpenAlgo symbol.

    Token first, then the broker trading symbol. The fallback is not
    belt-and-braces: Kotak populates "tok" on orderbook rows but leaves it as
    an empty string on tradebook rows, so a token-only lookup silently leaks
    the raw broker symbol ("YESBANK-EQ") into the UI, which then fails every
    downstream lookup that expects an OpenAlgo symbol. brsymbol in the master
    contract is pTrdSymbol (master_contract_db.py:155), i.e. exactly the value
    Kotak sends as trdSym.
    """
    token = str(row.get("tok") or "").strip()
    if token:
        mapped = get_symbol(token, exchange)
        if mapped:
            return mapped

    broker_symbol = row.get("trdSym") or row.get("sym") or ""
    if broker_symbol:
        mapped = get_oa_symbol(broker_symbol, exchange)
        if mapped:
            return mapped

    logger.debug(
        f"No OpenAlgo symbol for token '{token}' / symbol '{broker_symbol}' on {exchange}. "
        "Keeping the broker trading symbol."
    )
    return None


def map_order_data(order_data):
    """
    Processes and modifies a list of order dictionaries based on specific conditions.

    Parameters:
    - order_data: A list of dictionaries, where each dictionary represents an order.

    Returns:
    - The modified order_data with updated 'tradingsymbol' and 'product' fields.
    """
    order_data = _book_rows(order_data, "Kotak order book")

    if order_data:
        for order in order_data:
            exchange = map_exchange(order.get("exSeg"))
            if not exchange:
                logger.debug(
                    f"Kotak order book: unknown exchange segment "
                    f"{order.get('exSeg')!r}; keeping the raw value"
                )
                exchange = order.get("exSeg")
            order["exSeg"] = exchange

            symbol_from_db = _openalgo_symbol(order, exchange)
            if symbol_from_db:
                order["trdSym"] = symbol_from_db
    return order_data


def calculate_order_statistics(order_data):
    """
    Calculates statistics from order data, including totals for buy orders, sell orders,
    completed orders, open orders, and rejected orders.

    Parameters:
    - order_data: A list of dictionaries, where each dictionary represents an order.

    Returns:
    - A dictionary containing counts of different types of orders.
    """
    # Initialize counters
    total_buy_orders = total_sell_orders = 0
    total_completed_orders = total_open_orders = total_rejected_orders = 0

    if order_data:
        for order in order_data:
            # .get() throughout: a row missing trnsTp/ordSt used to raise
            # KeyError and take the whole statistics block down with it.
            trns_tp = order.get("trnsTp")
            if trns_tp == "B":
                order["trnsTp"] = "BUY"
                total_buy_orders += 1
            elif trns_tp == "S":
                order["trnsTp"] = "SELL"
                total_sell_orders += 1

            # Normalize "trigger pending" to "open" for UI compatibility
            ord_st = order.get("ordSt")
            if ord_st == "trigger pending":
                order["ordSt"] = "open"
                ord_st = "open"

            # Count orders based on their status
            if ord_st == "complete":
                total_completed_orders += 1
            elif ord_st == "open":
                total_open_orders += 1
            elif ord_st == "rejected":
                total_rejected_orders += 1

    # Compile and return the statistics
    return {
        "total_buy_orders": total_buy_orders,
        "total_sell_orders": total_sell_orders,
        "total_completed_orders": total_completed_orders,
        "total_open_orders": total_open_orders,
        "total_rejected_orders": total_rejected_orders,
    }


def transform_order_data(orders):
    # Directly handling a dictionary assuming it's the structure we expect
    if isinstance(orders, dict):
        # Convert the single dictionary into a list of one dictionary
        orders = [orders]

    transformed_orders = []

    for order in orders:
        # Make sure each item is indeed a dictionary
        if not isinstance(order, dict):
            logger.warning(
                f"Warning: Expected a dict, but found a {type(order)}. Skipping this item."
            )
            continue
        if order.get("prcTp") == "MKT":
            order["prcTp"] = "MARKET"
        elif order.get("prcTp") == "L":
            order["prcTp"] = "LIMIT"
        elif order.get("prcTp") == "SL":
            order["prcTp"] = "SL"
        elif order.get("prcTp") == "SL-M":
            order["prcTp"] = "SL-M"

        # For limit orders, show the order price (prc) instead of average price (avgPrc)
        # avgPrc is only relevant for executed orders
        order_price = order.get("avgPrc", 0.0)
        if order.get("prcTp") in ["LIMIT", "SL"]:
            # If order is not executed/complete, use the limit price
            if order.get("ordSt") != "complete":
                order_price = order.get("prc", 0.0)

        transformed_order = {
            "symbol": order.get("trdSym", ""),
            "exchange": order.get("exSeg", ""),
            "action": order.get("trnsTp", ""),
            "quantity": order.get("qty", 0),
            "price": order_price,
            "trigger_price": order.get("trgPrc", 0.0),
            "pricetype": order.get("prcTp", ""),
            "product": order.get("prod", ""),
            "orderid": order.get("nOrdNo", ""),
            "order_status": order.get("ordSt", ""),
            "timestamp": order.get("ordEntTm", ""),
        }

        transformed_orders.append(transformed_order)

    return transformed_orders


def map_trade_data(trade_data):
    """
    Processes and modifies a list of order dictionaries based on specific conditions.

    Parameters:
    - order_data: A list of dictionaries, where each dictionary represents an order.

    Returns:
    - The modified order_data with updated 'tradingsymbol' and 'product' fields.
    """
    trade_data = _book_rows(trade_data, "Kotak trade book")

    if trade_data:
        for order in trade_data:
            exchange = map_exchange(order.get("exSeg"))
            if not exchange:
                exchange = order.get("exSeg")
            order["exSeg"] = exchange

            symbol_from_db = _openalgo_symbol(order, exchange)
            if symbol_from_db:
                order["trdSym"] = symbol_from_db

            # Map transaction type regardless of symbol lookup result
            trns_tp = order.get("trnsTp")
            if trns_tp == "B":
                order["trnsTp"] = "BUY"
            elif trns_tp == "S":
                order["trnsTp"] = "SELL"
    logger.debug(f"Mapped Kotak tradebook: {trade_data}")
    return trade_data


def transform_tradebook_data(tradebook_data):
    transformed_data = []

    for trade in tradebook_data:
        transformed_trade = {
            "symbol": trade.get("trdSym", ""),
            "exchange": trade.get("exSeg", ""),
            "product": trade.get("prod", ""),
            "action": trade.get("trnsTp", ""),
            "quantity": trade.get("fldQty", 0),
            "average_price": trade.get("avgPrc", 0.0),
            "trade_value": float(trade.get("fldQty", 0.0)) * float(trade.get("avgPrc", 0.0)),
            "orderid": trade.get("nOrdNo", ""),
            "timestamp": trade.get("exTm", ""),
        }
        transformed_data.append(transformed_trade)
    return transformed_data


def map_position_data(position_data):
    return map_order_data(position_data)


def transform_positions_data(positions_data):
    transformed_data = []
    for position in positions_data:
        transformed_position = {
            "symbol": position.get("trdSym", ""),
            "exchange": position.get("exSeg", ""),
            "product": position.get("prod", ""),
            "quantity": (_as_int(position.get("flBuyQty")) - _as_int(position.get("flSellQty")))
            + (_as_int(position.get("cfBuyQty")) - _as_int(position.get("cfSellQty"))),
            "average_price": position.get("avgnetprice", 0.0),
        }
        buy_qty = float(position.get("flBuyQty", 0))
        sell_qty = float(position.get("flSellQty", 0))

        if transformed_position["quantity"] > 0 and buy_qty > 0:
            transformed_position["average_price"] = round(
                float(position.get("buyAmt", 0)) / buy_qty, 2
            )
        elif transformed_position["quantity"] < 0 and sell_qty > 0:
            transformed_position["average_price"] = round(
                float(position.get("sellAmt", 0)) / sell_qty, 2
            )
        elif transformed_position["quantity"] != 0:
            transformed_position["average_price"] = 0.0

        transformed_data.append(transformed_position)

    return transformed_data


def transform_holdings_data(holdings_data):
    transformed_data = []
    logger.debug("Holdings Data")
    logger.debug(f"{holdings_data}")
    for holding in holdings_data:
        transformed_position = {
            "symbol": holding.get("displaySymbol", ""),
            "exchange": holding.get("exchangeSegment", ""),
            "quantity": holding.get("quantity", 0),
            "product": holding.get("instrumentType", ""),
            "pnl": round(
                (float(holding.get("mktValue", 0.0)) - float(holding.get("holdingCost", 0.0))), 2
            ),
            "pnlpercent": round(
                (
                    (float(holding.get("mktValue", 0.0)) - float(holding.get("holdingCost", 0.0)))
                    / float(holding.get("holdingCost", 0.0))
                    * 100
                )
                if float(holding.get("holdingCost", 0.0)) != 0
                else 0,
                2,
            ),
        }

        transformed_data.append(transformed_position)
    logger.debug("Holdings Data")
    logger.debug(f"{transformed_data}")
    return transformed_data


def map_portfolio_data(portfolio_data):
    """
    Processes and modifies a list of Portfolio dictionaries based on specific conditions and
    ensures both holdings and totalholding parts are transmitted in a single response.

    Parameters:
    - portfolio_data: A dictionary, where keys are 'holdings' and 'totalholding',
                      and values are lists/dictionaries representing the portfolio information.

    Returns:
    - The modified portfolio_data with 'product' fields changed for 'holdings' and 'totalholding' included.
    """
    # Check if 'data' is None or doesn't contain 'holdings'
    if portfolio_data.get("data") is None:
        logger.debug("No data available.")
        # Return an empty structure or handle this scenario as needed
        return {}

    # Directly work with 'data' for clarity and simplicity
    holdings = portfolio_data["data"]

    # Modify 'product' field for each holding if applicable

    for portfolio in holdings:
        token = portfolio.get("instrumentToken")

        exchange = map_exchange(portfolio.get("exchangeSegment"))
        if not exchange:
            exchange = portfolio.get("exchangeSegment")
        portfolio["exchangeSegment"] = exchange
        symbol_from_db = get_symbol(token, exchange) if token else None

        # Check if a symbol was found; if so, update the trading_symbol in the current order
        if symbol_from_db:
            portfolio["symbol"] = symbol_from_db
        if portfolio.get("instrumentType") == "Equity":
            portfolio["instrumentType"] = "CNC"  # Modify 'product' field
        else:
            logger.debug("Kotak Portfolio - Product Value for Delivery Not Found or Changed.")

    # The function already works with 'data', which includes 'holdings' and 'totalholding',
    # so we can return 'data' directly without additional modifications.

    return holdings


def calculate_portfolio_statistics(holdings_data):
    totalholdingvalue = sum(item["mktValue"] for item in holdings_data)
    totalinvvalue = sum(item["holdingCost"] for item in holdings_data)
    totalprofitandloss = sum(item["mktValue"] - item["holdingCost"] for item in holdings_data)

    totalpnlpercentage = (totalprofitandloss / totalinvvalue) * 100 if totalinvvalue != 0 else 0

    # To avoid division by zero in the case when total_investment_value is 0
    totalpnlpercentage = round(totalpnlpercentage, 2)

    return {
        "totalholdingvalue": totalholdingvalue,
        "totalinvvalue": totalinvvalue,
        "totalprofitandloss": totalprofitandloss,
        "totalpnlpercentage": totalpnlpercentage,
    }
