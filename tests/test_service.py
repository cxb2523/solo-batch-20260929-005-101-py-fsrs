import threading

import pytest

from fsrs import Card, Scheduler
from fsrs.service import CardStore, create_app


@pytest.fixture
def client():
    store = CardStore(Scheduler(enable_fuzzing=False))
    store.add_card(Card(card_id=1))
    app = create_app(store, seed=False)
    app.testing = True
    return app.test_client(), store


class TestReviewEndpoint:
    def test_review_advances_card(self, client):
        http, _ = client

        response = http.post("/review", json={"card_id": 1, "rating": 1})

        assert response.status_code == 200
        body = response.get_json()
        assert body["card"]["state_name"] == "Learning"
        assert body["card"]["remaining_steps"] == 2
        assert body["timeline_entry"]["rating_name"] == "Again"

    def test_unknown_card_and_bad_rating(self, client):
        http, _ = client

        assert http.post("/review", json={"card_id": 99, "rating": 1}).status_code == 404
        assert http.post("/review", json={"card_id": 1, "rating": 9}).status_code == 400
        assert http.post("/review", json={"card_id": 1}).status_code == 400


class TestTimelineEndpoint:
    def test_timeline_ordered_with_current_marker(self, client):
        http, _ = client

        for rating in (1, 3, 3):
            http.post("/review", json={"card_id": 1, "rating": rating})

        body = http.get("/cards/1/timeline").get_json()
        timeline = body["timeline"]

        assert [entry["rating"] for entry in timeline] == [1, 3, 3]
        assert [entry["step"] for entry in timeline] == [0, 1, None]
        assert [entry["state_name"] for entry in timeline] == [
            "Learning",
            "Learning",
            "Review",
        ]
        assert [entry["remaining_steps"] for entry in timeline] == [2, 1, 0]
        assert timeline[-1]["current"] is True
        assert all(entry["current"] is False for entry in timeline[:-1])
        assert all("due" in entry for entry in timeline)

    def test_unknown_card_timeline(self, client):
        http, _ = client
        assert http.get("/cards/77/timeline").status_code == 404

    def test_cards_list_refreshes_after_review(self, client):
        http, _ = client

        before = http.get("/cards").get_json()["cards"][0]
        http.post("/review", json={"card_id": 1, "rating": 4})
        after = http.get("/cards").get_json()["cards"][0]

        assert before["state"] != after["state"]
        assert after["state_name"] == "Review"


class TestConcurrency:
    def test_concurrent_reviews_are_serialized(self, client):
        http, store = client

        status_codes: list[int] = []
        lock = threading.Lock()

        def rate():
            response = http.post("/review", json={"card_id": 1, "rating": 1})
            with lock:
                status_codes.append(response.status_code)

        threads = [threading.Thread(target=rate) for _ in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(status_codes) == 30
        assert status_codes == [200] * 30

        timeline = http.get("/cards/1/timeline").get_json()["timeline"]
        assert len(timeline) == 30  # each request advanced the card exactly once

        card = store.get_card(1)
        assert card.state.name == "Learning"
        assert card.step == 0


class TestReplayPage:
    def test_play_page_available(self, client):
        http, _ = client
        response = http.get("/cards/1/timeline/play")

        assert response.status_code == 200
        assert b"FSRS" in response.data

    def test_demo_seed_timeline(self):
        app = create_app()
        app.testing = True
        http = app.test_client()

        assert http.get("/").status_code == 200
        body = http.get("/cards/1001/timeline").get_json()
        assert len(body["timeline"]) >= 1

        relearning = http.get("/cards/1002/timeline").get_json()
        assert relearning["card"]["state_name"] == "Relearning"
