# Rocket Now CLI agent workflow

- Use the installed `rocketnow` command for the account owner. Its commands return JSON.
- Discover a store and dish, then write a draft JSON with `storeId`, `items[].dishId`, quantity, and selected option IDs. Include the chosen result's `logging.searchId` and `logging.searchJourneyId` as `searchId` / `searchJourneyId`; use `keyword` only as a fallback.
- Run `rocketnow order check <draft.json>`. Show the user its store, items, total, currency, delivery address and instructions, delivery type, and saved card's masked number. Ask for explicit approval for this exact purchase and amount. The review hash expires in 10 minutes and is single use.
- Only after approval, run `rocketnow order submit <draft.json> --approve-hash <approved reviewHash> --approve-amount <approved requestedAmount>`. If the checkout changed, show the new review and ask again.
- A successful submit starts a real purchase and returns a pending ID. Use `rocketnow order payment-url <pendingId>` to open the payment page without exposing its URL in tool output. The user completes any 3-D Secure challenge there. Never request or record card numbers or OTP codes in chat.
- After the user says payment is complete, run `rocketnow order confirm <pendingId> --payment-complete`, then inspect `rocketnow orders --in-progress` or the app to verify state. If a request times out, inspect the app before any retry; do not issue another prepay automatically.
- The hosted payment page and CLI-initiated purchase have not been verified with a live CLI order. Tell the user this before the first such attempt.
