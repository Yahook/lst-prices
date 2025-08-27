#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import sys
from decimal import Decimal, getcontext
from typing import Optional, Dict, Any
from datetime import datetime

# pip install web3
from web3 import Web3
from web3.exceptions import BadFunctionCallOutput, ContractLogicError

getcontext().prec = 50  # высокая точность вычислений

RPC_DEFAULT = os.environ.get("ZQ2_RPC", "https://api.zq2-mainnet.zilliqa.com")

# Минимальные ABI для делегационного прокси (включаем оба варианта имени функции LST)
DELEGATION_ABI = [
    {"inputs": [], "name": "getPrice", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getLST",   "outputs": [{"internalType": "address",  "name": "", "type": "address"}],  "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "lst",      "outputs": [{"internalType": "address",  "name": "", "type": "address"}],  "stateMutability": "view", "type": "function"},
]

# Базовый ERC-20 ABI
ERC20_ABI = [
    {"inputs": [], "name": "symbol",   "outputs": [{"internalType": "string", "name": "", "type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "name",     "outputs": [{"internalType": "string", "name": "", "type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"internalType": "uint8",  "name": "", "type": "uint8"}],  "stateMutability": "view", "type": "function"},
]

# Доп. ERC-20 вариант с bytes32 символом/именем (редко, но встречается)
ERC20_BYTES32_ABI = [
    {"inputs": [], "name": "symbol",   "outputs": [{"internalType": "bytes32", "name": "", "type": "bytes32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "name",     "outputs": [{"internalType": "bytes32", "name": "", "type": "bytes32"}], "stateMutability": "view", "type": "function"},
]

def to_checksum(w3: Web3, addr: str) -> str:
    return Web3.to_checksum_address(addr)

def load_pools_from_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    pools = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        name  = item.get("name") or item.get("title") or f"Pool {i+1}"
        proxy = item.get("proxy") or item.get("delegation") or item.get("delegationProxy") or item.get("address")
        if proxy:
            pools.append({"name": name, "proxy": proxy})
    return pools

def autodiscover_pools():
    candidates = [
        "pools.json",
        os.path.join("public", "pools.json"),
        os.path.join("src", "data", "pools.json"),
        os.path.join("src", "shared", "constants", "pools.json"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            try:
                return load_pools_from_json(p)
            except Exception:
                pass
    return []

def try_call(fn):
    """Выполнить web3 call, вернуть None при ошибке/реверте."""
    try:
        return fn.call()
    except Exception:
        return None

def detect_lst_address(w3: Web3, proxy_addr: str) -> Optional[str]:
    c = w3.eth.contract(address=proxy_addr, abi=DELEGATION_ABI)
    # Сначала пробуем getLST(), если реверт — пробуем lst()
    for method in ("getLST", "lst"):
        try:
            fn = getattr(c.functions, method)()
            addr = fn.call()
            if addr and int(addr, 16) != 0:
                return to_checksum(w3, addr)
        except Exception:
            continue
    return None

def fetch_price(w3: Web3, proxy_addr: str) -> Optional[Decimal]:
    c = w3.eth.contract(address=proxy_addr, abi=DELEGATION_ABI)
    try:
        raw = c.functions.getPrice().call()
        return Decimal(raw) / Decimal(10**18)
    except Exception:
        return None

def decode_bytes32(b: bytes) -> str:
    try:
        # срез до первого \x00 и декод
        return b.rstrip(b"\x00").decode("utf-8", errors="replace")
    except Exception:
        return ""

def fetch_token_meta(w3: Web3, token_addr: str) -> Dict[str, Any]:
    t = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
    symbol = None
    name = None
    decimals = None

    # Пытаемся string-версии
    try:
        symbol = t.functions.symbol().call()
    except Exception:
        symbol = None
    try:
        name = t.functions.name().call()
    except Exception:
        name = None
    try:
        decimals = t.functions.decimals().call()
    except Exception:
        decimals = None

    # Если symbol/name не удалось — пробуем bytes32 ABI
    if symbol is None or name is None:
        t_b = w3.eth.contract(address=token_addr, abi=ERC20_BYTES32_ABI)
        if symbol is None:
            try:
                s = t_b.functions.symbol().call()
                if isinstance(s, (bytes, bytearray)):
                    symbol = decode_bytes32(s)
            except Exception:
                pass
        if name is None:
            try:
                n = t_b.functions.name().call()
                if isinstance(n, (bytes, bytearray)):
                    name = decode_bytes32(n)
            except Exception:
                pass

    # Значения по умолчанию
    if not symbol:
        symbol = "LST"
    if decimals is None:
        decimals = 18

    return {
        "symbol": str(symbol),
        "name": str(name) if name else "",
        "decimals": int(decimals),
    }

def fetch_one(w3: Web3, pool_name: str, proxy_addr: str) -> Dict[str, Any]:
    proxy_cs = to_checksum(w3, proxy_addr)
    lst_addr = detect_lst_address(w3, proxy_cs)
    if not lst_addr:
        return {
            "pool": pool_name,
            "proxy": proxy_cs,
            "lst": None,
            "type": "non-liquid-or-unknown",
            "error": "No LST detected (getLST/lst reverted)",
        }

    rate = fetch_price(w3, proxy_cs)
    if rate is None:
        # LST есть, но цена не вернулась — отметим ошибку
        meta = fetch_token_meta(w3, lst_addr)
        return {
            "pool": pool_name,
            "proxy": proxy_cs,
            "lst": lst_addr,
            "symbol": meta["symbol"],
            "decimals": meta["decimals"],
            "error": "getPrice reverted/failed",
        }

    meta = fetch_token_meta(w3, lst_addr)
    return {
        "pool": pool_name,
        "proxy": proxy_cs,
        "lst": lst_addr,
        "symbol": meta["symbol"],
        "decimals": meta["decimals"],
        "rate_zil_per_lst": str(rate),
    }

def print_table(results):    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"Time: {now}")
    print()

    name_w = max([len(r.get("pool","")) for r in results] + [4])
    sym_w  = max([len(r.get("symbol","?")) for r in results] + [6])
    print(f"{'Pool'.ljust(name_w)}  {'Symbol'.ljust(sym_w)}  {'Rate (1 LST ≃ X ZIL)'.ljust(24)}  Proxy")
    print("-"*(name_w + 2 + sym_w + 2 + 24 + 2 + 42))
    for r in results:
        pool = r.get("pool","")
        sym  = r.get("symbol","?")
        proxy = r.get("proxy","")
        if "error" in r and "rate_zil_per_lst" not in r:
            msg = r.get("error","ERROR")
            print(f"{pool.ljust(name_w)}  {sym.ljust(sym_w)}  {msg.ljust(24)}  {proxy}")
        else:
            rate = r.get("rate_zil_per_lst")
            rate_str = f"{Decimal(rate):.6f}" if rate is not None else "N/A"
            print(f"{pool.ljust(name_w)}  {sym.ljust(sym_w)}  {rate_str.ljust(24)}  {proxy}")

def main():
    ap = argparse.ArgumentParser(description="Fetch ZQ2 LST prices (1 LST ≃ X ZIL) from delegation proxies.")
    ap.add_argument("--rpc", default=RPC_DEFAULT, help=f"RPC URL (default: {RPC_DEFAULT})")
    ap.add_argument("--pools-json", help="JSON file: [{'name': 'Amazing Pool', 'proxy': '0x...'}, ...]")
    ap.add_argument("--json", action="store_true", help="Output JSON instead of table")
    ap.add_argument("proxies", nargs="*", help="Delegation proxy addresses (0x...)")
    args = ap.parse_args()

    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        print(f"ERROR: cannot connect to RPC {args.rpc}", file=sys.stderr)
        sys.exit(2)

    # Источник пулов
    if args.pools_json:
        try:
            pools = load_pools_from_json(args.pools_json)
        except Exception as e:
            print(f"ERROR: failed to read {args.pools_json}: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.proxies:
        pools = [{"name": f"Pool {i+1}", "proxy": addr} for i, addr in enumerate(args.proxies)]
    else:
        pools = autodiscover_pools()
        if not pools:
            print("No pools provided. Use --pools-json or pass proxy addresses.", file=sys.stderr)
            sys.exit(1)

    results = []
    for p in pools:
        try:
            info = fetch_one(w3, p["name"], p["proxy"])
            results.append(info)
        except Exception as e:
            results.append({"pool": p.get("name","Pool"), "proxy": p.get("proxy",""), "error": str(e)})

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print_table(results)

if __name__ == "__main__":
    main()
