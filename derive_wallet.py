#!/usr/bin/env python3
"""Show what deposit/proxy/safe wallets the SDK derives for our signer.

The SDK rejected wallet=0x02F6AcEB because it doesn't match any deterministic
wallet of signer 0x7777. This computes 0x7777's actual derived addresses (and
dumps the classifier source) so we learn whether (a) 0x7777's real deposit wallet
is a different address, or (b) 0x02F6AcEB belongs to a different signer.

Run in the polymarket-client venv:
    ~/pmtest/bin/python ~/bear-oracle-confirmed-sniper/derive_wallet.py
"""

import inspect
from pathlib import Path

from eth_account import Account


def load_env() -> dict:
    env: dict[str, str] = {}
    p = Path(__file__).resolve().parent / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


def main() -> None:
    env = load_env()
    magic = (env.get("POLY_PRIVATE_KEY") or "").strip()
    funder = (env.get("POLY_FUNDER_ADDRESS") or "").strip()
    signer = Account.from_key(magic).address if magic else "?"

    print(f"signer (from POLY_PRIVATE_KEY): {signer}")
    print(f"target wallet (POLY_FUNDER):    {funder}")

    import polymarket
    from polymarket._internal import wallet as W

    wd = polymarket.PRODUCTION.wallet_derivation
    print("\nwallet_derivation config:")
    for f in (
        "proxy_factory",
        "proxy_implementation",
        "safe_factory",
        "deposit_wallet_factory",
        "deposit_wallet_implementation",
        "deposit_wallet_beacon",
    ):
        print(f"  {f}: {getattr(wd, f, '?')}")

    print("\npolymarket._internal.wallet members:")
    for n in sorted(x for x in dir(W) if not x.startswith("_")):
        o = getattr(W, n)
        if inspect.isfunction(o):
            try:
                print(f"  def {n}{inspect.signature(o)}")
            except Exception:
                print(f"  def {n}(...)")
        elif inspect.isclass(o):
            print(f"  class {n}")

    # dump the classifier (shows exactly what it derives + compares)
    for name in ("classify_wallet_type",):
        fn = getattr(W, name, None)
        if fn:
            print(f"\n===== source: {name} =====")
            try:
                print(inspect.getsource(fn))
            except Exception as e:
                print(f"(no source: {e})")

    # dump + try every derive/predict/compute address helper for our signer
    print("\n===== derivation helpers (source + result for our signer) =====")
    for n in sorted(x for x in dir(W) if not x.startswith("_")):
        if not any(
            k in n.lower()
            for k in (
                "derive",
                "deposit",
                "proxy",
                "safe",
                "predict",
                "compute",
                "address",
            )
        ):
            continue
        o = getattr(W, n)
        if not inspect.isfunction(o):
            continue
        print(f"\n--- {n}{_sig(o)} ---")
        try:
            print(inspect.getsource(o))
        except Exception:
            pass
        # best-effort: call with (signer, wallet_derivation) in a few arg orders
        for call in (
            lambda: o(signer, wd),
            lambda: o(wd, signer),
            lambda: o(signer),
            lambda: o(address=signer, derivation=wd),
            lambda: o(signer=signer, derivation=wd),
        ):
            try:
                res = call()
                print(f"   => {res}")
                break
            except Exception:
                continue


def _sig(o) -> str:
    try:
        return str(inspect.signature(o))
    except Exception:
        return "(...)"


if __name__ == "__main__":
    main()
