"""Tool schemas — what the LLM reads to decide when and how to call each tool."""

_CHAIN_IDS = {
    "type": "array",
    "items": {"type": "integer"},
    "description": "EVM chain ids to include (1 Ethereum, 8453 Base, 42161 Arbitrum, 10 Optimism, "
                   "137 Polygon, 56 BNB, 59144 Linea). Omit for the configured default / all chains.",
}

MM_STATUS = {
    "name": "mm_status",
    "description": (
        "Check the MetaMask Agent Wallet state: is the mm CLI installed, is the user signed in, is a "
        "wallet initialised, which wallet mode (server-wallet or BYOK) and trading mode (guard or "
        "beast), and the active address. Call this FIRST whenever the user mentions their MetaMask "
        "wallet, before any other mm_* tool. The result includes a `next_step` telling you exactly "
        "what to do if something is missing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "include_policy": {"type": "boolean",
                               "description": "Also return the Guard Mode policy YAML (allowlists, outflow limit). Read-only."},
        },
        "required": [],
    },
}

MM_SETUP = {
    "name": "mm_setup",
    "description": (
        "Guided, chat-only setup of MetaMask Agent Wallet. The user never types shell commands: you "
        "drive each step and relay what MetaMask returns.\n"
        "Steps, in order:\n"
        "1. action='install_cli' — installs the mm CLI with npm (asks the user to approve).\n"
        "2. action='login_start' — returns a sign-in URL. Show it to the user and ask them to open it, "
        "sign in with Google, email or MetaMask, then paste back the CLI token the page shows.\n"
        "3. action='login_complete' with token=<pasted token> — finishes sign-in.\n"
        "4. action='init' with wallet_mode ('server-wallet' recommended, keys stay in MetaMask's TEE) "
        "and trading_mode ('guard' recommended: allowlists + 24h outflow limit; 'beast' = threat scan only).\n"
        "5. action='create_wallet' if init reports no wallet yet.\n"
        "Ask the user which options they want before steps 4-5; never pick 'beast' or 'byok' on your own. "
        "action='logout' signs out and clears local credentials."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["install_cli", "login_start", "login_complete", "init", "create_wallet", "logout"],
            },
            "token": {"type": "string", "description": "login_complete only: the CLI token copied from the sign-in page."},
            "wallet_mode": {"type": "string", "enum": ["server-wallet", "byok"],
                            "description": "init only. 'byok' requires the user to have set MM_MNEMONIC in the environment themselves."},
            "trading_mode": {"type": "string", "enum": ["guard", "beast"], "description": "init / create_wallet. Default guard."},
            "name": {"type": "string", "description": "create_wallet only: display name for the new wallet."},
        },
        "required": ["action"],
    },
}

MM_BALANCE = {
    "name": "mm_balance",
    "description": (
        "Show the MetaMask Agent Wallet balances (native + tokens, fiat values) on the mainnets mm tracks. "
        "If the mainnets are empty, testnets (Sepolia, Arbitrum Sepolia, Polygon Amoy, incl. Circle USDC) are "
        "probed automatically and returned under `testnet`. The result is authoritative: no need to double-check "
        "with RPC calls or shell commands. Read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chain_ids": _CHAIN_IDS,
            "token": {"type": "string", "description": "Only this token (symbol like USDC, or 0x contract address)."},
            "currency": {"type": "string", "description": "Fiat code for conversion (usd, eur). Defaults to the plugin setting."},
            "address": {"type": "string", "description": "Look up another 0x address instead of the active wallet."},
            "testnet": {"type": "boolean", "description": "Read testnet balances via RPC (Sepolia, Arbitrum Sepolia, Amoy). Natives only unless token_contracts is given."},
            "token_contracts": {"type": "array", "items": {"type": "string"},
                                "description": "testnet only: ERC-20 contract addresses to read as well (e.g. Sepolia USDC 0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238)."},
        },
        "required": [],
    },
}

MM_MARKET = {
    "name": "mm_market",
    "description": (
        "Market data through MetaMask (read-only, no wallet needed beyond sign-in).\n"
        "action='price': spot price of a token — give symbol + chain_id (e.g. ETH on 8453) or explicit "
        "CAIP-19 asset_ids. action='token_search': find tokens by symbol/name to get their contract "
        "address. action='chains': list supported networks. action='trending' / 'popular': token lists "
        "for the active chain."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["price", "token_search", "chains", "trending", "popular"]},
            "symbol": {"type": "string", "description": "price: token symbol (ETH, USDC, POL...)."},
            "chain_id": {"type": "integer", "description": "price: chain the token lives on. Defaults to the plugin default chain."},
            "asset_ids": {"type": "array", "items": {"type": "string"},
                          "description": "price: explicit CAIP-19 ids, e.g. eip155:1/slip44:60 or eip155:8453/erc20:0x..."},
            "vs": {"type": "string", "description": "price: quote currency (usd default)."},
            "market_data": {"type": "boolean", "description": "price: include market cap, supply, 24h change."},
            "query": {"type": "string", "description": "token_search: symbol or name to search."},
            "chain_ids": _CHAIN_IDS,
            "limit": {"type": "integer", "description": "token_search: max results (default 10)."},
        },
        "required": ["action"],
    },
}

MM_HISTORY = {
    "name": "mm_history",
    "description": (
        "Transaction inspection (read-only). action='list': recent transactions of the active wallet. "
        "action='tx': one transaction by hash. action='decode': explain EVM calldata (0x...) in plain "
        "words before the user signs anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "tx", "decode"]},
            "chain_ids": _CHAIN_IDS,
            "limit": {"type": "integer", "description": "list: 1-50, default 20."},
            "type": {"type": "string", "description": "list: filter in | out | self."},
            "hash": {"type": "string", "description": "tx: 0x transaction hash."},
            "chain_id": {"type": "integer", "description": "tx: chain of the transaction (probed when omitted)."},
            "payload": {"type": "string", "description": "decode: hex calldata starting with 0x."},
        },
        "required": ["action"],
    },
}

MM_SWAP_QUOTE = {
    "name": "mm_swap_quote",
    "description": (
        "Preview a token swap or cross-chain bridge through MetaMask: expected output, fees, route, "
        "slippage, and the quoteId to execute later. Compare-only, NOTHING is executed. Always show the "
        "quote to the user first; executing is the separate, approval-gated mm_swap_execute."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "from_token": {"type": "string", "description": "Source token symbol (ETH, USDC...)."},
            "to_token": {"type": "string", "description": "Destination token symbol."},
            "amount": {"type": "string", "description": "Human-readable amount of from_token, e.g. '0.5' or '100'."},
            "from_chain_id": {"type": "integer", "description": "Source chain id. Defaults to the plugin default chain."},
            "to_chain_id": {"type": "integer", "description": "Destination chain id for a bridge; defaults to from_chain_id."},
            "slippage": {"type": "number", "description": "Max slippage percent (default 0.5)."},
        },
        "required": ["from_token", "to_token", "amount"],
    },
}

MM_TRANSFER = {
    "name": "mm_transfer",
    "description": (
        "MOVES FUNDS. Send native currency or an ERC-20 token from the MetaMask Agent Wallet to a 0x "
        "address. Before calling: confirm recipient, amount, token and chain with the user in plain words. "
        "Hermes then shows its own approval prompt (the user must accept), and MetaMask may additionally ask "
        "for 2FA on their phone or email (status AWAITING_MFA). Never call this in a loop or on your own "
        "initiative. ENS names are not accepted; resolve them first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient 0x address (40 hex chars)."},
            "amount": {"type": "string", "description": "Human-readable amount, e.g. '0.05' or '25'."},
            "token": {"type": "string", "description": "Token symbol (ETH, USDC, POL) or 0x contract address."},
            "chain_id": {"type": "integer", "description": "Chain to send on. Ask the user if not obvious; defaults to the plugin default chain."},
            "gas_speed": {"type": "string", "enum": ["low", "medium", "high"],
                          "description": "Optional fee tier. Any gas option routes the transfer through send-transaction with explicit EIP-1559 fees."},
            "max_fee_gwei": {"type": "number", "description": "Optional maxFeePerGas in gwei (e.g. 5). Use after an rpc_fee_too_low failure, with the user's go."},
            "priority_fee_gwei": {"type": "number", "description": "Optional maxPriorityFeePerGas in gwei (e.g. 1.5)."},
        },
        "required": ["to", "amount", "token"],
    },
}

MM_SWAP_EXECUTE = {
    "name": "mm_swap_execute",
    "description": (
        "MOVES FUNDS. Execute a swap or bridge through MetaMask, preferably by quote_id from a mm_swap_quote "
        "the user has just seen and accepted. Hermes asks the user to approve; MetaMask may add 2FA. "
        "Without quote_id, give from_token/to_token/amount/from_chain_id and MetaMask re-quotes at the "
        "current price."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "quote_id": {"type": "string", "description": "quoteId returned by mm_swap_quote (preferred)."},
            "from_token": {"type": "string"},
            "to_token": {"type": "string"},
            "amount": {"type": "string", "description": "Amount of from_token, e.g. '0.1'."},
            "from_chain_id": {"type": "integer"},
            "to_chain_id": {"type": "integer", "description": "Bridge destination; omit for a same-chain swap."},
            "slippage": {"type": "number", "description": "Max slippage percent (default 0.5)."},
        },
        "required": [],
    },
}

MM_SIGN = {
    "name": "mm_sign",
    "description": (
        "SIGNS WITH THE WALLET KEY. kind='message': sign a plain-text message. kind='typed_data': sign "
        "EIP-712 typed data (payload JSON with types, primaryType, domain, message). A signature can "
        "authorise token spending or log-ins, so explain to the user what they are signing and why; Hermes "
        "then asks for approval, and MetaMask may add 2FA."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["message", "typed_data"]},
            "message": {"type": "string", "description": "kind=message: the text to sign."},
            "payload": {"type": "string", "description": "kind=typed_data: EIP-712 JSON string."},
            "intent": {"type": "string", "description": "kind=typed_data: one-line human summary of what is signed (forwarded to MetaMask)."},
            "chain_id": {"type": "integer", "description": "Chain context for the signature. Defaults to the plugin default chain."},
        },
        "required": ["kind"],
    },
}

MM_REQUESTS = {
    "name": "mm_requests",
    "description": (
        "Pending MetaMask wallet requests (server-wallet mode). action='list': every pending transfer / swap "
        "/ signature with its intent and status. action='watch' with polling_id: wait up to `timeout` seconds "
        "for one request to finish and return its result (tx hash, signature, or failure). Read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "watch"]},
            "polling_id": {"type": "string", "description": "watch: id returned by a previous request."},
            "timeout": {"type": "integer", "description": "watch: seconds to wait, 5-600 (default 60)."},
        },
        "required": ["action"],
    },
}

ALL = (MM_STATUS, MM_SETUP, MM_BALANCE, MM_MARKET, MM_HISTORY, MM_SWAP_QUOTE,
       MM_TRANSFER, MM_SWAP_EXECUTE, MM_SIGN, MM_REQUESTS)
