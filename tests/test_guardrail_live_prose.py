"""Guardrails on real live-model prose (26 Sep): the no-advice and approved-language checks flagged 34 sentences in
three live runs on Claude Sonnet, every one a false positive (a compliance warning, the client's own topic, or a
comparison), which made the gate reject real improvements. These sentences must pass; plain promises and orders must
still be flagged."""
import pytest

from pregame import oracle

LIVE_SENTENCES_THAT_ARE_FINE = [
    "The 6.80% annuity figure is a payout rate on the premium. Do not present it as a return or as a guaranteed outcome.",
    "If she asks about guaranteed income, explain that a new retiree's immediate annuity pays about 6.85% of the premium a year. That is a payout rate, not an investment return.",
    "Put inflation of 3.00% a year next to those rates so she sees both sides.",
    "\"What income can we lock in now?\" The facts give current CD and annuity payout rates. They do not give the Okafors' income needs, balances or guaranteed income sources. Follow-up is needed before comparing options.",
    "Put inflation of 3.05% a year next to any income figure. Ask how they think about rising costs over a long retirement.",
    "Put inflation of 3.80% and the Ten-year Treasury yield of 4.15% in front of her as the current backdrop.",
    "The annuity payout rate of about 7.60% is a market figure for new retirees. Do not present it as a guaranteed result for her.",
    "The annuity payout rate is a headline figure for new retirees. Do not present it as a return or a guaranteed outcome. Keep the conversation on facts and questions.",
    "Put the projected 3.20% cost-of-living adjustment next to 3.85% price growth. Ask which household costs worry her most.",
    "Open with the annuity payout rate of about 7.50% and ask how much of a steady income floor they want from guaranteed sources versus their portfolio.",
    "If she brings up guaranteed income, explain that the annuity payout rate of about 6.95% is a share of the premium paid each year. Ask what she wants that income to cover.",
    "Do not describe the annuity payout rate as a guaranteed return or as safe from loss. The facts say only that new retirees are paid about 7.50% of premium a year.",
    "Note the current annuity payout rate of about 7.50% of premium a year as a market fact. Ask whether she wants to discuss guaranteed income at all.",
    "Do not call CDs or any deposit safe or risk-free. The Harborview failure may make her ask about this, so keep to the facts we have.",
    "The annuity payout rate is a percentage of premium. Do not present it as a return or a guaranteed outcome. Keep the comparison to facts and questions.",
    "Keep the discussion to facts and questions. Do not present the annuity payout rate as a recommendation or as a guaranteed result.",
    "Put the income picture side by side: one-year CDs at about 4.00%, prices up 3.80%, and a projected 3.35% Social Security adjustment.",
    "Put the current rates side by side: 4.75% for a one-year CD, about 6.85% for an immediate annuity payout, and 3.05% inflation. Ask what income she needs from her IRA of about $642 thousand.",
    "Put the rate picture side by side: 10-year Treasury at 4.90%, CDs at about 4.65%, annuity payout at about 6.95% of premium, and inflation at 3.00%.",
    "Do not present the annuity payout rate as a return or as a guaranteed outcome. Keep the discussion to facts and questions.",
    "Put inflation of 3.05% a year next to their income needs, and ask how much they expect to draw once Daniel stops work.",
    "They may ask how much guaranteed income they could build for the floor. The facts give a payout rate of about 6.95% of premium a year but no household income need or premium amount. This needs follow-up.",
    "Put the rate picture side by side: 4.15% on the ten-year Treasury, 3.85% inflation, and about 7.50% annuity payout rates.",
    "The annuity figure is a payout rate on premium, not a return. Do not present it as a guaranteed outcome or compare it to CD yields as if they were the same thing.",
    "They may ask how much guaranteed income they could get and how it compares with CDs. The facts give an annuity payout of about 7.60% of premium a year and a one-year CD rate of about 4.65%. They do not cover the household's premium amounts, their ages, or a like-for-like comparison, so this needs follow-up.",
    "Put their income floor next to inflation at 3.80%, the projected 3.35% Social Security adjustment, and the $214 Part B premium."
]

PROMISES_AND_ORDERS = [
    "Returns are guaranteed.",
    "This annuity guarantees income of 7% a year.",
    "You will get guaranteed income from this product.",
    "Don't worry, returns are guaranteed.",
    "We guarantee growth on this fund.",
    "Put 20% of the portfolio into bonds.",
    "Put your cash into the index fund next month.",
    "I would recommend selling the stock.",
    "This portfolio is built to protect your capital.",
    "The fund is risk-free."
]


def _flags(text):
    brief = {"sections": {"talking_points": [{"text": text, "fact_ids": []}]}}
    return oracle.GUARDRAIL_CHECKS["no-advice"](brief, {}) + oracle.GUARDRAIL_CHECKS["approved-language"](brief, {})


@pytest.mark.parametrize("text", LIVE_SENTENCES_THAT_ARE_FINE)
def test_live_compliant_prose_is_not_flagged(text):
    assert _flags(text) == []


@pytest.mark.parametrize("text", PROMISES_AND_ORDERS)
def test_promises_and_orders_are_still_flagged(text):
    assert _flags(text), text
