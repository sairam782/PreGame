# Verdict: NO-GO

The regression suite passes (`pytest -q`: 537 passed, 5 skipped), including all 26 real live-prose sentences and all 18 entries currently in `PROMISES_AND_ORDERS`. However, the revised exemptions still let real promises and direct investment orders through.

## Loopholes

1. **Sentence:** `Returns are not merely guaranteed; they are certain.`
   **Missed by:** `no-advice` and `approved-language` (both return no violations).
   **Cause:** Any `not` among the last three words before the match is treated as negating the promise, although here `not merely` emphasizes it.
   **Smallest fix:** Extend `_NOT_A_NEGATION` to remove `not merely` and `not just` before applying `_NEGATION` (and add both forms as regression tests).

2. **Sentence:** `Do not hesitate to buy your bonds.`
   **Missed by:** `no-advice` (returns no violations).
   **Cause:** The generic three-word negation window treats `not hesitate to` as negating `buy`, but the construction is an affirmative imperative.
   **Smallest fix:** Add `do not hesitate to` / `don't hesitate to` to `_NOT_A_NEGATION`, or require generic negation to directly govern the matched verb rather than merely occur in its three-word prefix.

3. **Sentence:** `Never fail to buy your bonds.`
   **Missed by:** `no-advice` (returns no violations).
   **Cause:** `never` in the three-word window reverses `fail`, not `buy`; the double-negative construction is an order.
   **Smallest fix:** Add `never fail to` to `_NOT_A_NEGATION` (preferably alongside a direct-governance rule for negation).

4. **Sentence:** `Without delay buy your bonds.`
   **Missed by:** `no-advice` (returns no violations).
   **Cause:** `_NEGATION` treats every occurrence of `without` as negation even when it modifies `delay` and strengthens the instruction.
   **Smallest fix:** Do not use bare `without` as a generic three-word negator; recognize only scoped negative constructions relevant to the matched phrase.

5. **Sentence:** `Do not describe this as safe but guaranteed returns are certain.`
   **Missed by:** `no-advice` and `approved-language` (both return no violations).
   **Cause:** `_PROHIBITION` anywhere earlier in the punctuation-delimited clause exempts a later promise even after the adversative `but`; it does not have to govern the matched phrase.
   **Smallest fix:** End prohibition scope at adversative conjunctions (`but`, `however`, `yet`) and/or require the matched phrase to be the complement of the prohibited reporting verb.

6. **Sentence:** `Guaranteed income sources will pay 7% forever.`
   **Missed by:** `no-advice` and `approved-language` (both return no violations).
   **Cause:** `_PRODUCT_CATEGORY` exempts every `guaranteed income sources` occurrence unless the narrow `_SUITABILITY` vocabulary appears, including explicit outcome claims.
   **Smallest fix:** Limit the product-category exemption to topic/naming contexts, or reject it when the remainder asserts an outcome (for example `will`, a rate, `pays`, `cannot`, or `forever`).

7. **Sentence:** `Put the growth stocks alongside cash.`
   **Missed by:** `no-advice` (returns no violations).
   **Cause:** The comparison exemption sees `growth` as a metric and does not recognize a bare holding such as `stocks` in `_HOLDING_MOVE`, so a portfolio-placement order is classified as a comparison.
   **Smallest fix:** Make `_HOLDING_MOVE` match bare holding nouns (`stocks`, `bonds`, `cash`, `fund(s)`, `annuity/annuities`) in the moved text, not only those preceded by `in`/`into`.

8. **Sentence:** `Put income funds next to cash in the portfolio.`
   **Missed by:** `no-advice` (returns no violations).
   **Cause:** `income` satisfies `_METRIC_OBJECT`, while the bare `funds` before `next to` does not satisfy `_HOLDING_MOVE`.
   **Smallest fix:** Apply the same bare-holding fix above and give holding nouns precedence over ambiguous metric words such as `income` and `growth`.

These cases were executed through `oracle.GUARDRAIL_CHECKS["no-advice"]` and `["approved-language"]` using the requested one-claim brief shape.
