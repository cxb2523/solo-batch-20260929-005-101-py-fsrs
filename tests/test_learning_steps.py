from datetime import timedelta

from fsrs import Card, Rating, Scheduler
from fsrs.state import State


class TestLearningStepDefaults:
    def test_default_step_sizes(self):
        scheduler = Scheduler()

        assert scheduler.learning_steps == (
            timedelta(minutes=1),
            timedelta(minutes=10),
        )
        assert scheduler.relearning_steps == (
            timedelta(minutes=1),
            timedelta(minutes=10),
        )

    def test_steps_do_not_apply_to_review_cards(self):
        scheduler = Scheduler(enable_fuzzing=False)

        card = Card()
        card, _ = scheduler.review_card(card, Rating.Easy, card.due)
        assert card.state == State.Review

        previous_due = card.due
        card, _ = scheduler.review_card(card, Rating.Good, card.due)

        assert card.state == State.Review
        assert (card.due - previous_due).days >= 1


class TestStepFreeze:
    def test_memo_state_frozen_until_graduation(self):
        scheduler = Scheduler(enable_fuzzing=False)
        card = Card()

        card, _ = scheduler.review_card(card, Rating.Again, card.due)
        initial_stability = card.stability
        initial_difficulty = card.difficulty

        card, _ = scheduler.review_card(card, Rating.Hard, card.due)
        assert card.stability == initial_stability
        assert card.difficulty == initial_difficulty

        card, _ = scheduler.review_card(card, Rating.Good, card.due)
        assert card.step == 1
        assert card.stability == initial_stability
        assert card.difficulty == initial_difficulty

        card, _ = scheduler.review_card(card, Rating.Again, card.due)
        assert card.step == 0
        assert card.stability == initial_stability
        assert card.difficulty == initial_difficulty

        card, _ = scheduler.review_card(card, Rating.Good, card.due)
        card, _ = scheduler.review_card(card, Rating.Good, card.due)
        assert card.state == State.Review
        assert card.step is None
        assert card.stability != initial_stability
        assert card.difficulty != initial_difficulty

    def test_hard_never_advances_or_graduates(self):
        scheduler = Scheduler(enable_fuzzing=False)
        card = Card()

        for _ in range(5):
            card, _ = scheduler.review_card(card, Rating.Hard, card.due)

        assert card.state == State.Learning
        assert card.step == 0

        card, _ = scheduler.review_card(card, Rating.Good, card.due)
        for _ in range(5):
            card, _ = scheduler.review_card(card, Rating.Hard, card.due)

        assert card.state == State.Learning
        assert card.step == 1

    def test_again_resets_to_first_step(self):
        scheduler = Scheduler(enable_fuzzing=False)
        card = Card()

        card, _ = scheduler.review_card(card, Rating.Good, card.due)
        assert card.step == 1

        card, _ = scheduler.review_card(card, Rating.Again, card.due)
        assert card.state == State.Learning
        assert card.step == 0

    def test_easy_graduates_from_any_step(self):
        scheduler = Scheduler(enable_fuzzing=False)
        card = Card()

        card, _ = scheduler.review_card(card, Rating.Again, card.due)
        card, _ = scheduler.review_card(card, Rating.Easy, card.due)

        assert card.state == State.Review
        assert card.step is None

    def test_relearning_again_resets_and_freeze(self):
        scheduler = Scheduler(enable_fuzzing=False)
        card = Card()

        card, _ = scheduler.review_card(card, Rating.Easy, card.due)
        review_stability = card.stability
        review_difficulty = card.difficulty

        card, _ = scheduler.review_card(card, Rating.Again, card.due)
        assert card.state == State.Relearning
        assert card.step == 0
        assert card.stability == review_stability
        assert card.difficulty == review_difficulty

        card, _ = scheduler.review_card(card, Rating.Good, card.due)
        assert card.step == 1
        assert card.stability == review_stability
        assert card.difficulty == review_difficulty

        card, _ = scheduler.review_card(card, Rating.Again, card.due)
        assert card.step == 0
        assert card.stability == review_stability
        assert card.difficulty == review_difficulty

        card, _ = scheduler.review_card(card, Rating.Good, card.due)
        card, _ = scheduler.review_card(card, Rating.Good, card.due)
        assert card.state == State.Review
        assert card.step is None

    def test_relearning_hard_stays_put(self):
        scheduler = Scheduler(
            enable_fuzzing=False,
            relearning_steps=(timedelta(minutes=2), timedelta(minutes=20)),
        )
        card = Card()

        card, _ = scheduler.review_card(card, Rating.Easy, card.due)
        card, _ = scheduler.review_card(card, Rating.Again, card.due)
        frozen_due = card.due

        card, _ = scheduler.review_card(card, Rating.Hard, card.due)
        assert card.state == State.Relearning
        assert card.step == 0
        assert card.due - frozen_due == timedelta(minutes=2)


class TestEarlierDue:
    def test_minute_step_wins_over_day_interval(self):
        scheduler = Scheduler(enable_fuzzing=False)
        card = Card()

        card, _ = scheduler.review_card(card, Rating.Again, card.due)

        assert card.due - card.last_review == timedelta(minutes=1)

    def test_stability_can_pull_a_step_even_earlier(self):
        # a custom scheduler whose day interval rounds down to 1 day is
        # always later than minute steps, but verify min() with a tiny
        # sub-minute step still resolves to the earlier minute step
        scheduler = Scheduler(
            enable_fuzzing=False,
            learning_steps=(timedelta(minutes=5), timedelta(minutes=10)),
        )
        card = Card()

        card, _ = scheduler.review_card(card, Rating.Again, card.due)
        assert card.due - card.last_review == timedelta(minutes=5)


class TestSingleState:
    def test_review_card_never_carries_a_step(self):
        scheduler = Scheduler(enable_fuzzing=False)
        card = Card()

        card, _ = scheduler.review_card(card, Rating.Easy, card.due)
        assert card.state == State.Review
        assert card.step is None

    def test_card_constructor_normalizes_conflicting_state(self):
        card = Card(state=State.Review, step=2)
        assert card.state == State.Review
        assert card.step is None

        learning_card = Card(state=State.Learning)
        assert learning_card.step == 0


class TestRemainingSteps:
    def test_remaining_steps(self):
        scheduler = Scheduler(enable_fuzzing=False)
        card = Card()

        assert scheduler.remaining_steps(card) == 2

        card, _ = scheduler.review_card(card, Rating.Again, card.due)
        assert scheduler.remaining_steps(card) == 2

        card, _ = scheduler.review_card(card, Rating.Good, card.due)
        assert scheduler.remaining_steps(card) == 1

        card, _ = scheduler.review_card(card, Rating.Good, card.due)
        assert scheduler.remaining_steps(card) == 0
        assert card.state == State.Review


class TestLegacyArchive:
    def test_from_dict_without_step_fields_treats_as_graduated(self):
        source_dict = {
            "parameters": [
                0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001,
                1.8722, 0.1666, 0.796, 1.4835, 0.0614, 0.2629, 1.6483, 0.6014,
                1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
            ],
            "desired_retention": 0.9,
            "maximum_interval": 36500,
            "enable_fuzzing": False,
        }

        scheduler = Scheduler.from_dict(source_dict)

        assert scheduler.learning_steps == ()
        assert scheduler.relearning_steps == ()

        card = Card()
        card, _ = scheduler.review_card(card, Rating.Good, card.due)

        # without learning steps the card graduates immediately and keeps its
        # original day-granularity interval
        assert card.state == State.Review
        assert card.step is None
        assert (card.due - card.last_review).days >= 1

    def test_round_trip_preserves_custom_steps(self):
        scheduler = Scheduler(
            learning_steps=(timedelta(seconds=30), timedelta(minutes=7)),
            relearning_steps=(timedelta(minutes=3),),
        )

        restored = Scheduler.from_dict(scheduler.to_dict())

        assert restored.learning_steps == scheduler.learning_steps
        assert restored.relearning_steps == scheduler.relearning_steps
