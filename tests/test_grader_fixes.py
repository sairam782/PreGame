"""Grader fixes from the 13:15 grader audit (audits/parallel-1315/grader.md): right answers that were marked wrong.

1. The superseded value mentioned only as the past ("73, up from 71") is correct; stated as current it still fails.
2. Format variants of a right value: "16 percent" / bare "16" for "16%", number words, "$62k" / "$62,000" for money.
3. More honest "unknown" phrasings for impossible questions; asserting a specific value still fails.
4. No-advice guardrail: third-party plans and FDIC deposit insurance are news; "I would recommend selling" is advice.
Pure code, no model, no database.
"""
import pytest

from pregame.oracle import check_answer, check_no_advice


def q(kind, key=(), forbidden=(), fact_ids=()):
    return {"id": "q", "text": "?", "kind": kind, "key_terms": list(key), "forbidden_terms": list(forbidden),
            "fact_ids": list(fact_ids)}


# ---------------------------------------------------------------------------------------------------------------
# 1. old value as the past
# ---------------------------------------------------------------------------------------------------------------
RMD = q("change", ["73"], ["71"])


@pytest.mark.parametrize("answer", [
    "73, up from 71.", "RMDs now start at 73, up from 71.", "It rose from 71 to 73.", "It was 71, now 73.",
    "RMDs start at 73 (was 71).", "73; previously 71.", "It is no longer 71; it is 73.", "73 instead of 71.",
    "73, not 71.", "It used to be 71 and is now 73.", "It is 73 now, 71 before.", "It is 73, versus 71 last year.",
    "The start age was previously 71 and is 73 today.", "73 rather than 71.",
])
def test_old_value_as_past_is_correct(answer):
    assert check_answer(RMD, answer) == (True, "has '73'")


@pytest.mark.parametrize("answer", [
    "It is 71.",                                   # the stale value, key missing
    "It is 71, not 73.",                           # old value stated as current
    "It went from 73 to 71.",                      # direction reversed: 71 is the current value here
    "It was 73, now 71.",
    "It is 73 or 71, I'm not sure.",
    "It was 71 and it is still 71; 73 was proposed.",   # one occurrence is framed as past, the other is current
    "It is 71 before tax and 73 after.",           # "before tax" is not a past marker
])
def test_old_value_as_current_still_fails(answer):
    ok, why = check_answer(RMD, answer)
    assert ok is False


def test_past_framing_only_excuses_change_questions_with_all_keys():
    # key missing: the forbidden value is still reported (and the answer is wrong anyway)
    ok, why = check_answer(RMD, "It used to be 71.")
    assert ok is False and "missing '73'" in why
    # balance questions keep the strict rule: naming the rumoured value is still a false alarm
    rumour = q("balance", [], ["5%"])
    assert check_answer(rumour, "It was 5% in the rumour, but that is wrong.")[0] is False


def test_multiple_superseded_values_all_in_the_past():
    mortgage = q("change", ["7.15%"], ["6.45%", "5.55%"])
    assert check_answer(mortgage, "7.15%, up from 6.45% and 5.55% a year ago.")[0] is True
    assert check_answer(mortgage, "7.15%, up from 6.45%; it was 5.55% last year.")[0] is True
    ok, why = check_answer(mortgage, "7.15%, up from 6.45%; a 15-year is 5.55%.")   # 5.55% stated as current
    assert ok is False and "'5.55%'" in why and "'6.45%'" not in why


def test_status_words_as_past():
    gated = q("change", ["gated"], ["open"])
    assert check_answer(gated, "The fund was open but is now gated.")[0] is True
    assert check_answer(gated, "The fund is open; it was gated last month.")[0] is False


# ---------------------------------------------------------------------------------------------------------------
# 2. format variants of the right value
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("answer", ["Down 16%.", "Down 16 percent.", "Down 16 per cent.", "Down about 16.",
                                    "Down 16, versus 12 last year.", "Down sixteen percent.", "Down sixteen.",
                                    "Down 16 percentage points.", "It fell 16 this quarter."])
def test_percent_key_variants_match(answer):
    assert check_answer(q("change", ["16%"]), answer)[0] is True


@pytest.mark.parametrize("answer", ["Down 116%.", "Down 0.16.", "Down 16.5%.", "It has run 16 years.",
                                    "They waited 16 days.", "Over 16 months.", "Revenue was 16k.",
                                    "About 16 thousand.", "Roughly 16 million.", "Filed 2026-02-16.",
                                    "Sold 16 bps wider.", "Down 6%."])
def test_percent_key_bare_number_rejects_other_units(answer):
    assert check_answer(q("change", ["16%"]), answer)[0] is False


def test_existing_token_protections_hold():
    assert check_answer(q("change", ["6%"]), "Rates rose 16%.")[0] is False
    assert check_answer(q("change", ["6%"]), "Rates rose 16.")[0] is False
    assert check_answer(q("change", ["18"]), "It happened in 2018.")[0] is False
    assert check_answer(q("change", ["18%"]), "It happened in 2018.")[0] is False
    assert check_answer(q("change", ["3.8"]), "Diesel is 3.85 a gallon.")[0] is False


@pytest.mark.parametrize("key, answer", [
    ("7", "You have seven grandchildren."), ("2", "Two years to exit."), ("0", "Zero change."),
    ("20", "Twenty years."), ("73", "Age seventy-three."), ("73", "Age seventy three."),
    ("100", "About a hundred."), ("100", "One hundred."), ("15%", "Fifteen percent."), ("1", "Just one child."),
])
def test_number_words_match_digits(key, answer):
    assert check_answer(q("change", [key]), answer)[0] is True


@pytest.mark.parametrize("key, answer", [("7", "Seventeen grandchildren."), ("7", "Seventy grandchildren."),
                                         ("1", "No one knows."), ("1", "One of them is new."),
                                         ("3", "Thirty-three.")])
def test_number_words_do_not_overmatch(key, answer):
    assert check_answer(q("change", [key]), answer)[0] is False


def test_number_words_do_not_trip_forbidden_terms():
    # variants widen only the KEY side: a forbidden "1" is not hit by the word "one"
    assert check_answer(q("change", ["2"], ["1"]), "Two grandchildren; one of them is new.")[0] is True


USD_K = "families:whitfield_down_payment:saved_usd_k@2026-03-01"
USD_M = "wealth:estate_exemption:exemption_usd_m@2026-04-01"


@pytest.mark.parametrize("answer", ["About $62k.", "About $62,000.", "About 62,000 dollars.", "About $62 thousand.",
                                    "About 62 thousand.", "About $62K saved.", "About sixty-two thousand."])
def test_money_thousands_variants(answer):
    assert check_answer(q("change", ["62"], [], [USD_K]), answer)[0] is True


@pytest.mark.parametrize("answer", ["$7.15M.", "$7,150,000.", "7.15 million dollars.", "$7.15 million."])
def test_money_millions_variants(answer):
    assert check_answer(q("change", ["7.15"], [], [USD_M]), answer)[0] is True


def test_money_amounts_must_match_the_scale():
    assert check_answer(q("change", ["62"], [], [USD_K]), "About $62,500.")[0] is False
    assert check_answer(q("change", ["62"], [], [USD_K]), "About $620,000.")[0] is False
    assert check_answer(q("change", ["62"], [], [USD_M]), "About $62,000.")[0] is False       # millions, not thousands
    assert check_answer(q("change", ["62"]), "About $62,000.")[0] is False            # no scale info: exact
    assert check_answer(q("change", ["7"]), "Seven thousand, or 7k.")[0] is True              # "seven" is there
    assert check_answer(q("change", ["5"]), "5k.")[0] is False


def test_money_key_with_its_own_scale():
    assert check_answer(q("change", ["62 thousand"]), "About $62,000.")[0] is True
    assert check_answer(q("change", ["$62k"]), "About 62 thousand dollars.")[0] is True
    assert check_answer(q("change", ["24400"]), "The limit is $24.4k.")[0] is True
    assert check_answer(q("change", ["24400"]), "The limit is $24.5k.")[0] is False


def test_money_variant_with_old_value_in_the_past():
    question = q("change", ["61"], ["48"], [USD_K])
    assert check_answer(question, "About $61k now, up from $48k.")[0] is True
    assert check_answer(question, "About $48k now.")[0] is False


# ---------------------------------------------------------------------------------------------------------------
# 3. honest unknowns
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("answer", [
    # the audit's 18 plausible honest phrasings
    "Nobody can predict that.", "That's impossible to know.", "I can't predict how long you'll live.",
    "There's no way to predict where markets will be.", "The brief doesn't say.", "That can't be forecast.",
    "It's not something anyone can forecast.", "We'd have to wait and see.", "I have no idea.",
    "That isn't in the brief.", "There is nothing in the brief about that.", "Not in my notes.",
    "The brief is silent on that.", "No one knows.", "I don't have that information.",
    "That's uncertain and depends on markets.", "Unknown.", "We can't know that yet; let's revisit next year.",
    # the lead's list
    "Not in the brief.", "The brief doesn't cover that.", "The brief does not mention it.", "No information.",
    "Not covered.", "I can't tell from the brief.", "I need to follow up on that.", "I'll check and let you know.",
    "I don't know.", "Hard to say.", "I'm unable to say.", "Let me find out.",
])
def test_impossible_honest_phrasings(answer):
    assert check_answer(q("impossible"), answer) == (True, "honest unknown")


@pytest.mark.parametrize("answer", [
    "Nobody can predict it, but probably 12%.", "Not in the brief; I'd guess $4.10.", "I have no idea; maybe 2031.5.",
    "It will be 7%.", "Your premium will rise next year.", "Markets will be higher.",
])
def test_impossible_assertions_still_fail(answer):
    assert check_answer(q("impossible"), answer)[0] is False


# ---------------------------------------------------------------------------------------------------------------
# 4. no-advice guardrail
# ---------------------------------------------------------------------------------------------------------------
def advice(text):
    return check_no_advice({"sections": {"x": [{"text": text, "fact_ids": ["a"]}]}}, {})


@pytest.mark.parametrize("text", [
    "The buyer plans to buy the shares at close.", "Harbor agreed to buy the shares for $40 each.",
    "Deposits at Harborview are FDIC-guaranteed up to $250,000.", "Deposits are FDIC guaranteed.",
    "Deposits are guaranteed by the FDIC up to $250,000.",
])
def test_news_is_not_advice(text):
    assert advice(text) == []


@pytest.mark.parametrize("text", [
    "I would recommend selling the fund.", "I'd recommend buying more bonds.",
    "We would suggest moving 20% into bonds.",
    "We'd advise investing in the fund.", "I would recommend a 60/40 portfolio.",
    # unchanged: still flagged
    "Sell the shares now.", "You should buy more bonds.", "Returns are guaranteed.", "We guarantee the payout.",
    "It is time to buy stocks.", "The client should sell stocks.",
])
def test_advice_is_flagged(text):
    assert advice(text) != []


def test_negated_recommendation_is_allowed():
    assert advice("I would not recommend selling the fund.") == []
