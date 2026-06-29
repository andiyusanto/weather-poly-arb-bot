"""Live execution via polymarket-client (Polymarket/py-sdk) for the weather bot.

This module is the bridge between the weather bot's sync trading loop and
Polymarket's async deposit-wallet SDK (``AsyncSecureClient``). The legacy
direct-EOA path through ``py-clob-client`` stopped working for fresh wallets
after the Polymarket V2 cutover (2026-04-28): every new EOA now returns
``400 maker address not allowed, please use the deposit wallet flow``. The
deposit-wallet flow is implemented exclusively in the official unified SDK.

This file is selected by ``polymarket_client.place_market_order`` when
``settings.use_sdk_executor`` is true; otherwise the legacy path runs unchanged.

Pattern + critical numbers adapted from the sister project at
``bear-oracle-confirmed-sniper/execution/sdk_executor.py`` (already live +
profitable). Notable mirrors:
  * AsyncSecureClient.create init signature (signer key + deposit wallet + BuilderApiKey)
  * ``_scale`` helper for raw-vs-human-unit response fields
  * Min 5-share order bump when a flat USDC amount would otherwise FOK-cancel
  * Folding taker fee into the recorded ``size_usdc`` (the SDK's making_amount
    is pre-fee — without this we overstate every win/loss by the fee)

Async lifecycle: this module creates a fresh ``AsyncSecureClient`` per order
via ``asyncio.run()`` at the sync call site in ``polymarket_client.py``. The
weather bot trades at ~10-20 orders/day, so the ~1-2s per-call init overhead
is irrelevant. If volume grows by 10x+, upgrade to a persistent event loop
in a background thread (see CLAUDE.md "ThreadPoolExecutor not asyncio").
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any, Dict, Optional

from loguru import logger

from config.settings import settings


# Minimum shares per Polymarket order. Below this and the FOK is killed
# server-side. Bumping a flat $stake at high ask prices keeps small orders alive.
MIN_SHARES = 5


def _scale(v: Any) -> float:
    """Normalize an SDK amount field to human units.

    The SDK sometimes returns raw 6-decimal numbers (USDC base units) and
    sometimes human-readable Decimals. Heuristic: values above 1000 are
    almost certainly raw 6-decimal (an actual size_usdc above $1000 would be
    far beyond our $50 cap), so we divide by 1e6.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f / 1e6 if f > 1000 else f


async def _ensure_sdk_client():
    """Create and authenticate an AsyncSecureClient for the deposit wallet.

    Returns None if any required credential is missing — the caller logs an
    explicit error and treats the order as rejected rather than crashing.
    """
    if not (settings.poly_private_key and settings.poly_funder_address):
        logger.error(
            "SDK live requires POLY_PRIVATE_KEY (Magic signer EOA) + "
            "POLY_FUNDER_ADDRESS (deposit wallet)"
        )
        return None
    if not (
        settings.poly_builder_api_key
        and settings.poly_builder_secret
        and settings.poly_builder_passphrase
    ):
        logger.error(
            "SDK live requires POLY_BUILDER_API_KEY / POLY_BUILDER_SECRET / "
            "POLY_BUILDER_PASSPHRASE in .env"
        )
        return None
    try:
        from polymarket.auth import BuilderApiKey
        from polymarket.clients import AsyncSecureClient
    except ImportError as e:
        logger.error(
            f"polymarket-client not importable in current venv: {e}. "
            f"Are you running from ~/weatherlive (`pip install -r requirements-sdk.txt`)?"
        )
        return None

    try:
        client = await AsyncSecureClient.create(
            private_key=settings.poly_private_key,
            wallet=settings.poly_funder_address,
            api_key=BuilderApiKey(
                settings.poly_builder_api_key,
                settings.poly_builder_secret,
                settings.poly_builder_passphrase,
            ),
        )
        logger.info(
            f"polymarket-client AsyncSecureClient ready (wallet={settings.poly_funder_address})"
        )
        return client
    except Exception as e:
        logger.error(f"Failed to init polymarket-client: {e}")
        return None


def _classify_order_error(e: Exception) -> str:
    """Classify SDK order errors so the caller can decide retry vs circuit-break."""
    s = str(e).lower()
    if any(t in s for t in ("not enough balance", "allowance", "insufficient")):
        return "insufficient_balance"
    if any(
        f in s
        for f in (
            "geoblock",
            "restricted in your region",
            "403",
            "unauthorized",
            "forbidden",
            "invalid api key",
        )
    ):
        return "auth_or_geoblock"
    return "transient"


async def sdk_place_market_order(
    token_id: str,
    side: str,
    size_usdc: float,
) -> Optional[Dict[str, Any]]:
    """Place a live market BUY through the deposit-wallet SDK.

    Args:
        token_id: Token to BUY (YES or NO clobTokenId).
        side: "yes" or "no" — used only for logging. The CLOB side is always BUY;
              picking YES vs NO is controlled by ``token_id`` itself.
        size_usdc: Dollar amount to spend, in human USDC (e.g. 2.0 for $2).

    Returns:
        ``{"status": "placed", "token_id": ..., "size_usdc": post_fee_spend,
           "shares": filled_shares, "fill_price": entry_price,
           "order_id": str, "response": <SDK response>}`` on a successful fill,
        ``{"status": "rejected", ...}`` on a clear reject,
        ``{"status": "error", "error": ...}`` on unhandled exceptions,
        ``None`` only when the SDK client could not even be initialized.
    """
    client = await _ensure_sdk_client()
    if client is None:
        return None

    amount = Decimal(str(size_usdc))

    # Best-effort price estimate — bumps small orders that would otherwise be
    # killed for failing the min-share floor. The SDK still caps fills with
    # ``max_price`` so the estimate is advisory, not a gate.
    est: Optional[float] = None
    try:
        est = float(
            await client.estimate_market_price(
                token_id=token_id, side="BUY", amount=amount, order_type="FOK"
            )
        )
    except Exception as e:
        logger.warning(
            f"estimate_market_price failed for {token_id} ({e}); proceeding without bump"
        )

    if est and est > 0 and float(amount) / est < MIN_SHARES:
        # Bump to clear Polymarket's minimum (+2% buffer for estimate drift).
        bumped = Decimal(str(round(MIN_SHARES * est * 1.02, 2)))
        if bumped > amount:
            logger.info(
                f"LIVE BUMP: ${float(amount):.2f} -> ${float(bumped):.2f} "
                f"for {MIN_SHARES}-share min @ est ${est:.4f} (token {token_id})"
            )
            amount = bumped

    try:
        resp = await client.place_market_order(
            token_id=token_id,
            side="BUY",
            amount=amount,
            order_type="FOK",
            builder_code=settings.poly_builder_code or None,
        )
    except Exception as e:
        kind = _classify_order_error(e)
        logger.error(f"LIVE ORDER error ({kind}) on token {token_id}: {e}")
        try:
            await client.close()
        except Exception:
            pass
        return {"status": "error", "error": str(e), "kind": kind}

    logger.info(f"LIVE ORDER RESP ({side.upper()} {token_id}): {resp!r}")

    taking = _scale(getattr(resp, "taking_amount", 0))  # shares received
    making = _scale(getattr(resp, "making_amount", 0))  # pUSDC spent (pre-fee)
    order_id = getattr(resp, "order_id", None) or getattr(resp, "id", None)
    status = str(getattr(resp, "status", "") or "").lower()

    if not (taking and taking > 0):
        reason = (
            getattr(resp, "error", None)
            or getattr(resp, "reason", None)
            or status
            or "no match"
        )
        logger.warning(f"LIVE ORDER not filled ({side.upper()} {token_id}): {reason}")
        try:
            await client.close()
        except Exception:
            pass
        return {
            "status": "rejected",
            "token_id": token_id,
            "side": side,
            "reason": str(reason),
            "response": repr(resp),
        }

    # Fold taker fee into recorded spend so downstream PnL is post-fee.
    # The SDK's making_amount excludes the fee Polymarket charges on top.
    gross = making if (making and making > 0) else taking * (est or 0.5)
    spend_with_fee = gross * (1.0 + settings.taker_fee_pct / 100.0)
    spend_q = Decimal(str(spend_with_fee)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    fill_price = round(float(spend_q) / taking, 4) if taking else 0.0

    logger.success(
        f"LIVE FILLED: {side.upper()} {token_id} @ ${fill_price:.4f} "
        f"size=${float(spend_q):.2f} shares={taking:.2f} order={order_id} status={status or 'ok'}"
    )

    try:
        await client.close()
    except Exception:
        pass

    return {
        "status": "placed",
        "token_id": token_id,
        "side": side,
        "size_usdc": float(spend_q),
        "shares": float(taking),
        "fill_price": fill_price,
        "order_id": str(order_id) if order_id else "",
        "response": repr(resp),
    }
