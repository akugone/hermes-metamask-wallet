---
name: safe-flows
description: How to drive MetaMask Agent Wallet from a Hermes conversation safely (setup, reads, writes, 2FA).
---

# MetaMask Agent Wallet — safe flows

The user never types shell commands. You call the `mm_*` tools; MetaMask holds the keys.

## Always start with `mm_status`
Its `next_step` tells you what is missing (CLI, sign-in, wallet). Follow it literally.

## Setup, step by step (`mm_setup`)
1. `install_cli` — Hermes will ask the user to approve the npm install.
2. `login_start` — show the URL. The user signs in (Google, email or MetaMask) and gets a CLI token.
3. `login_complete` with the pasted token. Never store or repeat the token.
4. Ask which wallet mode and trading mode they want, explain the difference in two lines, then `init`.
   - server-wallet + guard is the recommended default. Beast mode and BYOK trigger an approval prompt.
   - Never ask for a seed phrase in the chat. BYOK reads `MM_MNEMONIC` from the environment only.
5. `create_wallet` if `init` says no wallet exists yet.

## Never call `mm` yourself
Do not run `mm …` through the terminal tool, `execute_code`, or a script you write — not even to work
around a missing option. Every wallet capability you need is an `mm_*` tool with the approval gate; if one
is missing, say so and stop. Direct `mm` write calls are escalated to the user as a bypass attempt.

## Balances
`mm_balance` answers for the mainnets (MetaMask's indexer, which may lag a few blocks after a transaction);
when they are empty it also probes the testnets over RPC and returns them under `testnet` with a `hint`. That is
the wallet's own view and is enough to answer the user — re-checking over public RPCs or the terminal tool is
optional, not expected. Test funds have no fiat value — say so instead of "$0".

## Reads are free, writes are gated twice
`mm_balance`, `mm_market`, `mm_history`, `mm_swap_quote`, `mm_requests`, `mm_policy action='get'` never move funds.
`mm_transfer`, `mm_swap_execute`, `mm_sign` and `mm_policy action='set'` each go through the Hermes approval prompt
(the user clicks) and then MetaMask's policy, which may add a 2FA on their phone or email.

## The policy decides whether a transaction needs 2FA (`mm_policy`)
A fresh server wallet has a 0 USD 24h outflow limit and an empty allowlist: EVERY transfer is out of policy and
MetaMask asks the user for 2FA. In Guard Mode, 2FA is only asked for out-of-policy transactions (recipient not
allowlisted, 24h limit exceeded) and Blockaid flags. In-policy transactions run with no approval at all.
- When a transfer ends in `AWAITING_MFA` and the user asks why, or asks to automate anything (cron), call
  `mm_policy action='get'` and relay its `hint`.
- To let routine transactions through: `mm_policy action='set'` with `outflow_limit_usd` and `allowlist_add`
  (chain_id 0 = all chains). Restate the change in plain words first. Hermes asks the user to approve; MetaMask
  then asks for ONE 2FA because the change broadens the policy. Say so, do not retry.
- Propose a limit sized to what the user actually moves per day. Never call `set` on your own initiative, never
  use `remove_outflow_limit` unless the user explicitly asks for unlimited outflow, and never suggest Beast Mode
  as a way to avoid 2FA.
- Blockaid flags always ask the user, in every mode. Do not present the policy as a way around that.

## Known failure: `TX_FAILED` / `rpc_fee_too_low`, and sending with explicit fees
`mm transfer` can fail at signing with `TX_FAILED` + `rpc_fee_too_low` (seen on Sepolia): MetaMask's fee estimate
was below what the chain's RPC accepts. The result says NOT SENT with a `hint`; retrying the same call reproduces
the same failure.

With the user's explicit go, call `mm_transfer` ONCE more with explicit fees — `gas_speed='high'`, or
`max_fee_gwei` / `priority_fee_gwei` (e.g. 5 and 1.5). The plugin then builds the transaction itself and routes it
through `send-transaction` with EIP-1559 fields, still behind the Hermes approval prompt (the sentence shows the
fees) and MetaMask's policy + MFA. Never type `mm wallet send-transaction` yourself.

After any failed write, verify nothing was broadcast (`mm wallet requests list` shows no new BROADCASTED
request, nonce unchanged, balances unchanged), report the failure, and do not re-issue the same write
without a fresh go-ahead from the user.

## Before any write
- Restate recipient, amount, token and chain in plain words and get a yes. Chains are ids
  (1 Ethereum, 8453 Base, 42161 Arbitrum, 10 Optimism, 137 Polygon). Ask if unsure.
- For swaps, show the `mm_swap_quote` first (output, min output, fees, price impact) and execute by
  `quote_id`.
- For signatures, explain what the message or typed data authorises. A permit can spend tokens.
- Never repeat a write on your own. If the result has no `tx_hash` and no `polling_id` and says NOT SENT
  (e.g. `rpc_fee_too_low`), nothing happened: tell the user, and only with their explicit go call it once more,
  with the fix the hint suggests (explicit fees). If Hermes blocked it, stop and explain.

## After a write
- `status: CONFIRMED` with `tx_hash` — report the hash and the explorer link.
- `status: AWAITING_MFA` — tell the user to approve on MetaMask Mobile or via the email link. Do not
  retry. The plugin watches in the background and will post a "MetaMask Agent Wallet update" notice;
  relay it in one sentence. `mm_requests action='watch'` checks on demand.
- An `error.hint` is present — follow it before asking the user.

## Reporting
Quote amounts, fees and chain names exactly as returned; do not round silently.
