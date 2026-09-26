# Verdict: NO-GO

The fix is too loose. The new exemptions suppress clear promises and allocation advice. The requested full test run passes, but its negative cases do not cover the exemption boundaries below.

## Loopholes

1. **Sentence:** `Not only are returns guaranteed, they are tax-free.`
   **Missed by:** `no-advice` and `approved-language`.
   **Why:** `not` anywhere in the preceding 16-word clause is treated as negation, although `not only` affirms and intensifies the promise.
   **Smallest fix:** exclude `not only` from `_NEGATION` matches (or require a negation construction that governs the matched phrase).

2. **Sentence:** `Never doubt that returns are guaranteed.`
   **Missed by:** `no-advice` and `approved-language`.
   **Why:** `never` is treated as negating the promise, but here it negates `doubt` and therefore affirms the guarantee.
   **Smallest fix:** stop accepting an arbitrary negation token anywhere in the window; require the negation to govern the matched predicate, such as `not guaranteed` or `do not present/call/describe ... guaranteed`.

3. **Sentence:** `If she asks about guaranteed income tell her it is guaranteed.`
   **Missed by:** `no-advice` and `approved-language`.
   **Why:** `_CLIENT_TOPIC` exempts every guarantee match later in the same punctuation-delimited clause, including the advisor's answer promising the outcome.
   **Smallest fix:** limit the topic exemption to the topic noun phrase itself (for example, only the `guaranteed income` occurrence within the `asks about ...` span), not later guarantee matches; alternatively require punctuation after the topic frame and inspect the response separately.

4. **Sentence:** `Put 30% into bonds next to the cash.`
   **Missed by:** `no-advice` (and consequently the combined two-check probe is clean).
   **Why:** any comparison phrase later in the sentence exempts an allocation instruction, even when the grammatical object being put is a portfolio percentage.
   **Smallest fix:** allow the comparison exemption only when the direct object is a metric/fact (such as inflation, a rate, price growth, or an income figure), and never when it is a percentage allocation, money, or a holding.

5. **Sentence:** `No matter what, put 30% into bonds.`
   **Missed by:** `no-advice` (and consequently the combined two-check probe is clean).
   **Why:** the token `no` in the idiom `no matter what` suppresses the later allocation match within the 16-word clause.
   **Smallest fix:** exclude `no matter` from `_NEGATION`, alongside fixing negation scope as above.

6. **Sentence:** `Guaranteed income sources are right for you.`
   **Missed by:** `no-advice` and `approved-language`.
   **Why:** the product-category exemption is unconditional, even when the sentence endorses that category for the client.
   **Smallest fix:** exempt `guaranteed income sources` only in neutral topic/inventory contexts; do not exempt it when followed by recommendation language such as `right for you`, `best`, `should use`, or similar suitability claims.

## Verification

`C:/Projects/prep-harness/.venv/Scripts/python -m pytest -q` completed successfully: all collected non-skipped tests passed, with 5 skipped.
