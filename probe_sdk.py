#!/usr/bin/env python3
"""Probe live trading via polymarket-client (the unified deposit-wallet SDK).

Builds an authenticated AsyncSecureClient from your Magic EOA key + deposit
wallet + Relayer/Builder API creds, then:

  STAGE 1 (no funds spent): confirm auth — is_gasless_ready + collateral balance.
      If these succeed, the deposit-wallet auth wall is CROSSED.

  STAGE 2 (--token-id): place ONE tiny limit BUY well below market so it RESTS
      (doesn't fill), print AcceptedOrder/RejectedOrder, then cancel it.
      AcceptedOrder = we can place live orders → wire the bear executor to this.

Run in the venv that has polymarket-client installed:
    ~/pmtest/bin/python ~/bear-oracle-confirmed-sniper/probe_sdk.py
    ~/pmtest/bin/python ~/bear-oracle-confirmed-sniper/probe_sdk.py --auth relayer
    ~/pmtest/bin/python ~/bear-oracle-confirmed-sniper/probe_sdk.py --token-id <NO_ID> --price 0.10

.env (next to this file) needs:
    POLY_PRIVATE_KEY      = Magic EOA key (0x7777…)
    POLY_FUNDER_ADDRESS   = deposit wallet (0x02F6AcEB…)
    RELAYER_API_KEY       + RELAYER_ADDRESS
    BUILDER_API_KEY + BUILDER_SECRET + BUILDER_PASSPHRASE
"""

import argparse
import asyncio
import sys
import traceback
from decimal import Decimal
from pathlib import Path


def load_env() -> dict:
    env: dict[str, str] = {}
    p = Path(__file__).resolve().parent / ".env"
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0]  # strip inline comment, e.g. "0xABC…  # note"
        env[k.strip()] = v.strip().strip("'\"")
    return env


def pick(env: dict, *names: str):
    for n in names:
        if env.get(n):
            return env[n]
    return None


async def run(args, env) -> None:
    from polymarket.auth import BuilderApiKey, RelayerApiKey
    from polymarket.clients import AsyncSecureClient

    magic = pick(env, "POLY_PRIVATE_KEY")
    wallet = pick(env, "POLY_FUNDER_ADDRESS")
    relayer_key = pick(env, "RELAYER_API_KEY", "POLY_RELAYER_API_KEY")
    relayer_addr = pick(
        env, "RELAYER_ADDRESS", "RELAYER_API_ADDRESS", "POLY_RELAYER_ADDRESS"
    )
    b_key = pick(env, "BUILDER_API_KEY", "POLY_BUILDER_API_KEY")
    b_secret = pick(env, "BUILDER_SECRET", "POLY_BUILDER_SECRET")
    b_pass = pick(env, "BUILDER_PASSPHRASE", "POLY_BUILDER_PASSPHRASE")

    def ok(x):
        return "✓" if x else "✗ MISSING"

    print("credentials present:")
    print(f"  POLY_PRIVATE_KEY (Magic):   {ok(magic)}")
    print(f"  POLY_FUNDER_ADDRESS:        {ok(wallet)}  {wallet or ''}")
    print(f"  RELAYER_API_KEY:            {ok(relayer_key)}")
    print(f"  RELAYER_ADDRESS:            {ok(relayer_addr)}")
    print(f"  BUILDER_API_KEY:            {ok(b_key)}")
    print(f"  BUILDER_SECRET:             {ok(b_secret)}")
    print(f"  BUILDER_PASSPHRASE:         {ok(b_pass)}")

    if not magic or not wallet:
        print("\n!! need POLY_PRIVATE_KEY + POLY_FUNDER_ADDRESS at minimum.")
        sys.exit(1)

    if args.auth == "relayer":
        if not (relayer_key and relayer_addr):
            print("\n!! --auth relayer needs RELAYER_API_KEY + RELAYER_ADDRESS in .env")
            sys.exit(1)
        api_key = RelayerApiKey(key=relayer_key, address=relayer_addr)
    else:
        if not (b_key and b_secret and b_pass):
            print(
                "\n!! --auth builder needs BUILDER_API_KEY + BUILDER_SECRET + BUILDER_PASSPHRASE"
            )
            sys.exit(1)
        api_key = BuilderApiKey(b_key, b_secret, b_pass)

    print(f"\n--- creating AsyncSecureClient (auth={args.auth}, wallet={wallet}) ---")
    client = await AsyncSecureClient.create(
        private_key=magic, wallet=wallet, api_key=api_key
    )
    print("  ✓ client created")

    try:
        print("\n--- STAGE 1: auth checks (no funds) ---")
        try:
            ready = await client.is_gasless_ready()
            print(f"  is_gasless_ready: {ready}")
        except Exception as e:
            print(f"  is_gasless_ready -> {type(e).__name__}: {e}")

        try:
            bal = await client.get_balance_allowance(asset_type="COLLATERAL")
            print(f"  collateral balance/allowance: {bal}")
            print("  >> if this printed your ~$6.94, the auth wall is CROSSED.")
        except Exception as e:
            print(f"  get_balance_allowance -> {type(e).__name__}: {e}")

        if not args.token_id:
            print("\nStage 2 skipped (no --token-id).")
            return

        print("\n--- STAGE 2: tiny resting limit BUY, then cancel ---")
        resp = await client.place_limit_order(
            token_id=args.token_id,
            price=Decimal(str(args.price)),
            size=args.size,
            side="BUY",
        )
        print(f"  ORDER RESPONSE: {resp!r}")
        print("  >> AcceptedOrder = LIVE ORDERS WORK. RejectedOrder = read the reason.")
        oid = getattr(resp, "order_id", None) or getattr(resp, "id", None)
        if oid:
            print(f"  cancelling test order {oid} ...")
            print(f"  cancel: {await client.cancel_order(order_id=oid)}")
    finally:
        await client.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auth", choices=["builder", "relayer"], default="builder")
    ap.add_argument("--token-id", default=None, help="NO token id for Stage 2")
    ap.add_argument("--price", type=float, default=0.10, help="resting BUY price")
    ap.add_argument("--size", type=int, default=5, help="shares")
    args = ap.parse_args()
    env = load_env()
    try:
        asyncio.run(run(args, env))
    except Exception:
        print("\n=== TRACEBACK ===")
        traceback.print_exc()
        sys.exit(2)


if __name__ == "__main__":
    main()
