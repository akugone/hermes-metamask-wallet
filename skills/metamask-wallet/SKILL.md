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
`mm_balance` answers for the mainnets; when they are empty it also probes the testnets and returns them under
`testnet` with a `hint`. Trust it: do not re-verify balances with curl, public RPCs or the terminal tool.
Test funds have no fiat value — say so instead of "$0".

## Reads are free, writes are gated twice
`mm_balance`, `mm_market`, `mm_history`, `mm_swap_quote`, `mm_requests` never move funds.
`mm_transfer`, `mm_swap_execute`, `mm_sign` each go through the Hermes approval prompt (the user
clicks) and then MetaMask's policy, which may add a 2FA on their phone or email.

## Known failure: `TX_FAILED` / `rpc_fee_too_low`, and sending with explicit fees
`mm transfer` can fail at signing with `TX_FAILED` + `rpc_fee_too_low`. Neither it nor the lower-level
commands expose a gas flag: fees come from the wallet's own estimator, so there is nothing to tune in the
plugin and retrying the same call reproduces the same failure.

To send with explicit fees, use the lower-level command (ask the user first — this path bypasses the
Hermes approval prompt):
`mm wallet send-transaction --chain-id <id> --payload '{"to":"0x…","value":"0x…","gas":"0x5208","maxFeePerGas":"0x…","maxPriorityFeePerGas":"0x…","chainId":"0x…"}' --intent "<sentence>" --wait`
It still goes through MetaMask's policy + MFA: expect `_notice.kind = AWAITING_MFA` (the user must confirm
on the email/device registered to their dashboard) and a possible `TX_DENIED` if nobody approves in time.

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
