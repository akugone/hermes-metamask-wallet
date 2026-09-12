# Cron recipes

Hermes cron sessions run with no human present. With the plugin's defaults that means: **reads work,
writes are blocked**. Keep `approvals.cron_mode` and `approvals.single_query_mode` at their defaults;
the recipes below rely on them.

## 0. Prerequisite for any write: a policy that lets routine transactions through

A fresh server wallet has `rolling_24h: 0` and an empty allowlist. **Every** outgoing transaction exceeds a
0 USD limit, so MetaMask asks for 2FA by e-mail or push — and a cron job cannot answer it. The job stops at
`AWAITING_MFA`, the watcher reports it, nothing is sent. This is the default, not a failure.

Fix it once, interactively, before creating the job:

> "Show my MetaMask policy." → `mm_policy action='get'` tells you in plain words whether routine transactions
> will ask for 2FA.
>
> "Raise my 24 h outflow limit to 100 $ and allowlist 0x1234…abcd on Base." → Hermes approval prompt for that
> exact change → **one** MetaMask 2FA (the change broadens the policy) → done.

From then on, transactions to allowlisted addresses that keep the rolling 24 h outflow under the limit run with
no 2FA. Out-of-policy transactions and anything Blockaid flags still ask you — that part cannot be turned off,
and should not be. Size the limit to what the job legitimately moves per day, not higher.

For swaps the counterparty is MetaMask's router, not a recipient; the allowlist covers contract targets but the
router can change with the route. Run the swap once by hand and check it goes through without `AWAITING_MFA`
before scheduling it.

## 1. Morning wallet brief (read-only)

Ask Hermes:

> Every weekday at 08:30, check my MetaMask wallet: balances on Base and Ethereum, the last 10
> transactions, ETH and USDC prices in EUR, and anything pending in mm_requests. Send me a five-line
> summary and flag any outgoing transaction I did not initiate.

Tools used: `mm_balance`, `mm_history`, `mm_market`, `mm_requests`. Nothing is gated.

## 2. Price alert (read-only)

> Every 30 minutes, get the ETH price on Base. If it moved more than 5 % since your last check, tell me.

The model keeps the previous value in its cron memory; the plugin only provides `mm_market`.

## 3. Weekly DCA under Guard Mode (one write, pre-approved once)

0. Do step 0 above: the limit must cover the weekly amount, otherwise the job stops at `AWAITING_MFA`.
1. Interactively, say: *"Swap 50 USDC for ETH on Base."* Review the quote, then when Hermes shows the
   approval prompt for **"Swap 50 USDC for ETH on Base (8453)"**, answer **always**. That allow-list
   entry covers exactly this sentence — a different amount or chain will prompt again.
2. Create the cron job: *"Every Monday at 09:00, swap 50 USDC for ETH on Base using mm_swap_execute
   (re-quote), then report the tx hash."*
3. In MetaMask, Guard Mode's rolling 24 h outflow limit bounds the worst case. Do not switch to Beast
   Mode for automation, and do not remove the limit (`remove_outflow_limit`) to make a job "just work" —
   raise it to what the job needs.

If MetaMask asks for 2FA (e.g. the swap exceeds your outflow limit), the job pauses in `AWAITING_MFA`;
the plugin's watcher reports the outcome in the conversation that owns the cron job, and
`mm_requests` lists it.

## What never runs unattended

Setup steps that change custody or protection (`beast`, `byok`, `logout`), and any write whose exact
intent has not been explicitly allow-listed by you.
Policy changes (`mm_policy action='set'`) are writes too: they need you at the Hermes prompt and, when they
broaden the policy, at the MetaMask 2FA.
