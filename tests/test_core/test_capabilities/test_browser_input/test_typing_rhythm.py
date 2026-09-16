"""Typing rhythm strategies — the model, and that it is a model at all.

``instant`` is the default and must stay a no-op. ``human`` has to produce a
*distribution* rather than a constant: a fixed delay between keys is as
recognisable as no delay at all, so what is pinned here is the structure —
spread, word boundaries, punctuation, Shift, occasional pauses, a modelled
hold — and the seeded determinism that makes a run reproducible.
"""

from __future__ import annotations

import statistics

import pytest

from spectragent.core.capabilities.browser.input import (
    HumanRhythm,
    InstantRhythm,
    Keystroke,
    TypingRhythmProtocol,
    TypingRhythmRegistrar,
)

TEXT = "The quick brown fox, jumped over 13 lazy dogs!"


class TestTheRegistryShape:
    """Symmetric with ``mouse_trajectory``: same registrar, same discriminator."""

    def test_both_strategies_are_registered(self) -> None:
        assert sorted(TypingRhythmRegistrar.keys()) == ["human", "instant"]

    def test_the_strategy_is_the_switch_and_instant_is_off(self) -> None:
        assert InstantRhythm().humanized is False
        assert HumanRhythm().humanized is True

    def test_rhythm_type_is_the_registry_key(self) -> None:
        assert InstantRhythm().rhythm_type == "instant"
        assert HumanRhythm().rhythm_type == "human"

    def test_both_satisfy_the_protocol_structurally(self) -> None:
        for strategy in (InstantRhythm(), HumanRhythm()):
            assert isinstance(strategy, TypingRhythmProtocol)

    def test_the_generated_union_carries_every_knob(self) -> None:
        from spectragent.generated._composed import HumanTyping_RhythmConfig

        fields = HumanTyping_RhythmConfig.model_fields
        assert {
            "median_iki_ms",
            "iki_sigma",
            "space_factor",
            "punctuation_factor",
            "shift_factor",
            "pause_probability",
            "median_hold_ms",
            "seed",
        } <= set(fields)


class TestInstantRhythm:
    def test_one_keystroke_per_character_with_no_timing(self) -> None:
        keystrokes = InstantRhythm().generate("abc")
        assert keystrokes == [Keystroke("a"), Keystroke("b"), Keystroke("c")]

    def test_empty_text_is_no_keystrokes(self) -> None:
        assert InstantRhythm().generate("") == []


class TestHumanRhythmIsADistribution:
    def test_one_keystroke_per_character_carrying_that_character(self) -> None:
        keystrokes = HumanRhythm(seed=1).generate(TEXT)
        assert "".join(k.char for k in keystrokes) == TEXT

    def test_the_same_seed_types_the_same_way(self) -> None:
        assert HumanRhythm(seed=7).generate(TEXT) == HumanRhythm(seed=7).generate(TEXT)

    def test_a_different_seed_types_differently(self) -> None:
        assert HumanRhythm(seed=7).generate(TEXT) != HumanRhythm(seed=8).generate(TEXT)

    def test_the_rng_is_per_instance_not_global(self) -> None:
        """Two instances never influence each other, whatever runs between."""
        import random

        first = HumanRhythm(seed=3)
        random.seed(999)  # a global reseed must change nothing here
        second = HumanRhythm(seed=3)
        assert first.generate(TEXT) == second.generate(TEXT)

    def test_intervals_are_spread_not_constant(self) -> None:
        gaps = [k.gap_ms for k in HumanRhythm(seed=2).generate("a" * 400)]
        assert statistics.pstdev(gaps) > 20.0
        assert len({round(g, 3) for g in gaps}) > 300

    def test_the_tail_is_heavy_to_the_right(self) -> None:
        """A lognormal's mean sits above its median; a symmetric one would not."""
        gaps = sorted(k.gap_ms for k in HumanRhythm(seed=4).generate("a" * 3000))
        median = statistics.median(gaps)
        assert statistics.fmean(gaps) > median
        # Slowest 1% is multiples of the median, not a few ms above it.
        assert gaps[-30] > 2.0 * median

    @staticmethod
    def _intervals(text: str) -> list[float]:
        """Press-to-press intervals, which is what the model actually draws.

        ``gap_ms`` is the flight time — the interval *minus* the previous
        key's hold — so comparing raw gaps across characters compares two
        quantities at once. Drawn with both sigmas at zero so the structure
        is the only thing left varying.
        """
        rhythm = HumanRhythm(
            seed=5, iki_sigma=0.0, hold_sigma=0.0, pause_probability=0.0
        )
        keystrokes = rhythm.generate(text)
        previous_hold = 0.0
        intervals: list[float] = []
        for k in keystrokes:
            intervals.append(k.gap_ms + previous_hold)
            previous_hold = k.hold_ms
        return intervals

    def test_a_word_boundary_is_slower_than_a_letter(self) -> None:
        # "ab cd": the space is where the hands re-position.
        intervals = self._intervals("ab cd")
        assert intervals[2] > intervals[1]
        assert intervals[3] == pytest.approx(intervals[1])

    def test_punctuation_is_slower_and_so_is_what_follows_it(self) -> None:
        # "ab.cd": '.' pays the reach, 'c' pays the sentence break after it.
        intervals = self._intervals("ab.cd")
        assert intervals[2] > intervals[1]  # '.'
        assert intervals[3] > intervals[1]  # 'c', right after it
        assert intervals[4] == pytest.approx(intervals[1])  # 'd', back to normal

    def test_a_capital_costs_the_reach_for_shift(self) -> None:
        assert self._intervals("aB")[1] > self._intervals("ab")[1]

    def test_occasional_long_pauses_appear(self) -> None:
        rhythm = HumanRhythm(seed=6, pause_probability=0.05, pause_min_ms=800.0)
        gaps = [k.gap_ms for k in rhythm.generate("a" * 500)]
        assert sum(1 for g in gaps if g > 700.0) >= 10

    def test_no_pauses_when_the_probability_is_zero(self) -> None:
        rhythm = HumanRhythm(seed=6, pause_probability=0.0, max_gap_ms=600.0)
        assert all(k.gap_ms <= 600.0 for k in rhythm.generate("a" * 500))

    def test_gaps_and_holds_stay_inside_their_bounds(self) -> None:
        rhythm = HumanRhythm(seed=9, min_gap_ms=30.0, max_gap_ms=500.0,
                             min_hold_ms=40.0, max_hold_ms=120.0,
                             pause_probability=0.0)
        for k in rhythm.generate(TEXT * 10):
            assert 30.0 <= k.gap_ms <= 500.0
            assert 40.0 <= k.hold_ms <= 120.0

    def test_the_hold_is_modelled_separately_from_the_gap(self) -> None:
        keystrokes = HumanRhythm(seed=11).generate("a" * 300)
        holds = [k.hold_ms for k in keystrokes]
        gaps = [k.gap_ms for k in keystrokes]
        # A real dwell is short and tight next to the flight time; a
        # constant would make the collector's two features one.
        assert statistics.pstdev(holds) > 3.0
        assert statistics.median(holds) < statistics.median(gaps)

    def test_a_string_types_at_a_human_rate(self) -> None:
        """Sanity on the defaults: seconds, not milliseconds, and not minutes."""
        keystrokes = HumanRhythm(seed=12).generate("hello world")
        duration_s = sum(k.gap_ms + k.hold_ms for k in keystrokes) / 1000.0
        assert 1.0 < duration_s < 8.0

    def test_cjk_is_timed_like_anything_else(self) -> None:
        # Delivery differs (see HumanKeyboard); the *rhythm* does not.
        keystrokes = HumanRhythm(seed=13).generate("你好世界")
        assert [k.char for k in keystrokes] == ["你", "好", "世", "界"]
        assert all(k.gap_ms > 0 for k in keystrokes)
