"""
Polymarket USDC.e On-Chain Approval
====================================
Directly submits an ERC20 approve() transaction to Polygon.

The py-clob-client update_balance_allowance() API call is informational
only and doesn't write on-chain. This script does the real approval.

Approves both required spenders:
  1. CTF Exchange (main CLOB contract)
  2. NegRisk CTF Exchange (for neg-risk markets)

Usage:
    python3 approve_usdc.py
"""

import sys
from dotenv import dotenv_values
from web3 import Web3

# ── Polygon contracts ─────────────────────────────────────────────────
POLYGON_RPCS = [
    "https://rpc.ankr.com/polygon",
    "https://polygon-mainnet.public.blastapi.io",
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://polygon-rpc.com",
]

USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

SPENDERS = [
    ("CTF Exchange",         "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
    ("NegRisk CTF Exchange", "0xC5d563A36AE78145C45a50134d48A1215220f80a"),
]

MAX_UINT256 = 2**256 - 1

ERC20_ABI = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner",   "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]


def main():
    env = dotenv_values(".env")
    pk = env.get("POLY_PRIVATE_KEY", "").strip()
    funder = env.get("POLY_FUNDER_ADDRESS", "").strip()

    if not pk:
        print("❌ POLY_PRIVATE_KEY not found in .env")
        sys.exit(1)

    w3 = None
    for rpc in POLYGON_RPCS:
        try:
            candidate = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if candidate.is_connected():
                print(f"  RPC:     {rpc}")
                w3 = candidate
                break
        except Exception:
            continue

    if w3 is None:
        print("❌ Could not connect to any Polygon RPC. Check network.")
        sys.exit(1)

    account = w3.eth.account.from_key(pk)
    wallet = Web3.to_checksum_address(funder if funder else account.address)
    print(f"  Wallet:  {wallet}")

    usdc = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)

    # Show current balance
    raw_bal = usdc.functions.balanceOf(wallet).call()
    balance = raw_bal / 1e6
    print(f"  Balance: ${balance:.2f} USDC\n")

    for name, spender_raw in SPENDERS:
        spender = Web3.to_checksum_address(spender_raw)

        # Check current allowance
        current = usdc.functions.allowance(wallet, spender).call()
        current_usdc = current / 1e6
        print(f"--- {name} ---")
        print(f"  Spender:   {spender}")
        print(f"  Allowance: ${current_usdc:.2f} USDC")

        if current >= MAX_UINT256 // 2:
            print("  ✅ Already approved (max). Skipping.\n")
            continue

        # Build and send approve tx
        print("  Submitting approve() transaction...")
        try:
            nonce = w3.eth.get_transaction_count(wallet)
            gas_price = w3.eth.gas_price

            tx = usdc.functions.approve(spender, MAX_UINT256).build_transaction({
                "from":     wallet,
                "nonce":    nonce,
                "gas":      100_000,
                "gasPrice": gas_price,
                "chainId":  137,
            })

            signed = w3.eth.account.sign_transaction(tx, pk)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"  Tx sent: {tx_hash.hex()}")
            print("  Waiting for confirmation...")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt.status == 1:
                print(f"  ✅ Approved! Block: {receipt.blockNumber}")
            else:
                print(f"  ❌ Transaction reverted. Hash: {tx_hash.hex()}")
        except Exception as e:
            print(f"  ❌ Error: {e}")
        print()

    # Final verification
    print("--- Final Allowance Check ---")
    for name, spender_raw in SPENDERS:
        spender = Web3.to_checksum_address(spender_raw)
        current = usdc.functions.allowance(wallet, spender).call()
        status = "✅" if current > 0 else "❌"
        print(f"  {status} {name}: ${current/1e6:.2f} USDC")


if __name__ == "__main__":
    main()
