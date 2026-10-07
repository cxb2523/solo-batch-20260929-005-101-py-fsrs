"""
fsrs.service
------------

Resident HTTP service for the FSRS scheduler.

Exposes:
    POST /review                       rate a card
    GET  /cards                        list cards (JSON)
    GET  /cards/<card_id>/timeline     ordered step timeline (JSON)
    GET  /cards/<card_id>/timeline/play  replay page for screen recording

Reviews of the same card are serialized through a per-card lock so that
concurrent ratings never interleave and a card only ever advances once per
rating.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Flask, jsonify, render_template_string, request

from fsrs.card import Card
from fsrs.rating import Rating
from fsrs.review_log import ReviewLog
from fsrs.scheduler import Scheduler
from fsrs.state import State

RATING_NAMES = {
    Rating.Again: "Again",
    Rating.Hard: "Hard",
    Rating.Good: "Good",
    Rating.Easy: "Easy",
}

STATE_NAMES = {
    State.Learning: "Learning",
    State.Review: "Review",
    State.Relearning: "Relearning",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimelineEntry(dict):
    """JSON-serializable snapshot of one rated step in the card's timeline."""


class CardStore:
    """
    In-memory card store with per-card review serialization.

    Each card keeps its current Card object, its ReviewLogs and the ordered
    timeline entries produced by every rating.
    """

    def __init__(self, scheduler: Scheduler | None = None) -> None:
        self.scheduler = scheduler or Scheduler(enable_fuzzing=False)
        self._cards: dict[int, Card] = {}
        self._logs: dict[int, list[ReviewLog]] = {}
        self._timeline: dict[int, list[dict[str, Any]]] = {}
        self._card_locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, card_id: int) -> threading.Lock:
        with self._locks_guard:
            lock = self._card_locks.get(card_id)
            if lock is None:
                lock = threading.Lock()
                self._card_locks[card_id] = lock
            return lock

    def add_card(self, card: Card | None = None) -> Card:
        card = card or Card()
        self._cards[card.card_id] = card
        self._logs.setdefault(card.card_id, [])
        self._timeline.setdefault(card.card_id, [])
        return card

    def get_card(self, card_id: int) -> Card:
        card = self._cards.get(card_id)
        if card is None:
            raise KeyError(card_id)
        return card

    def list_cards(self) -> list[Card]:
        return list(self._cards.values())

    def logs_for(self, card_id: int) -> list[ReviewLog]:
        self.get_card(card_id)
        return list(self._logs.get(card_id, []))

    def timeline_for(self, card_id: int) -> list[dict[str, Any]]:
        self.get_card(card_id)
        return list(self._timeline.get(card_id, []))

    def _snapshot(
        self,
        *,
        card: Card,
        rating: Rating,
        review_datetime: datetime,
        remaining: int,
    ) -> dict[str, Any]:
        return {
            "card_id": card.card_id,
            "rating": int(rating),
            "rating_name": RATING_NAMES[rating],
            "review_datetime": review_datetime.isoformat(),
            "due": card.due.isoformat(),
            "state": int(card.state),
            "state_name": STATE_NAMES[card.state],
            "step": card.step,
            "remaining_steps": remaining,
            "stability": card.stability,
            "difficulty": card.difficulty,
            "current": False,
        }

    def review(
        self,
        card_id: int,
        rating: Rating,
        review_datetime: datetime | None = None,
        review_duration: int | None = None,
    ) -> tuple[Card, ReviewLog, dict[str, Any]]:
        card = self.get_card(card_id)
        review_datetime = review_datetime or _utcnow()

        # serialize concurrent ratings of the same card
        with self._lock_for(card_id):
            reviewed_card, review_log = self.scheduler.review_card(
                card=card,
                rating=rating,
                review_datetime=review_datetime,
                review_duration=review_duration,
            )
            remaining = self.scheduler.remaining_steps(reviewed_card)
            entry = self._snapshot(
                card=reviewed_card,
                rating=rating,
                review_datetime=review_datetime,
                remaining=remaining,
            )

            self._cards[card_id] = reviewed_card
            self._logs.setdefault(card_id, []).append(review_log)
            timeline = self._timeline.setdefault(card_id, [])
            for previous in timeline:
                previous["current"] = False
            entry["current"] = True
            timeline.append(entry)

        return reviewed_card, review_log, entry

    def seed_demo_cards(self, *, now: datetime | None = None) -> list[Card]:
        """
        Create two cards that have already moved through learning steps so the
        replay page has a non-trivial timeline on first open.
        """

        now = now or _utcnow()
        seeded: list[Card] = []

        # card 1: mid-learning (on the final learning step)
        learning_card = Card(card_id=1001, due=now)
        self.add_card(learning_card)
        learning_card, _, _ = self.review(
            learning_card.card_id, Rating.Again, now - timedelta(minutes=12)
        )
        learning_card, _, _ = self.review(
            learning_card.card_id, Rating.Hard, now - timedelta(minutes=10)
        )
        learning_card, _, _ = self.review(
            learning_card.card_id, Rating.Good, now - timedelta(minutes=8)
        )
        seeded.append(learning_card)

        # card 2: graduated, lapsed once and is now mid-relearning
        relearning_card = Card(card_id=1002, due=now - timedelta(days=4))
        self.add_card(relearning_card)
        relearning_card, _, _ = self.review(
            relearning_card.card_id, Rating.Good, now - timedelta(days=4)
        )
        relearning_card, _, _ = self.review(
            relearning_card.card_id, Rating.Good, now - timedelta(days=3)
        )
        relearning_card, _, _ = self.review(
            relearning_card.card_id, Rating.Easy, now - timedelta(days=1)
        )
        relearning_card, _, _ = self.review(
            relearning_card.card_id, Rating.Again, now - timedelta(minutes=20)
        )
        relearning_card, _, _ = self.review(
            relearning_card.card_id, Rating.Good, now - timedelta(minutes=10)
        )
        seeded.append(relearning_card)

        return seeded


def card_payload(card: Card, scheduler: Scheduler) -> dict[str, Any]:
    return {
        "card_id": card.card_id,
        "state": int(card.state),
        "state_name": STATE_NAMES[card.state],
        "step": card.step,
        "remaining_steps": scheduler.remaining_steps(card),
        "stability": card.stability,
        "difficulty": card.difficulty,
        "due": card.due.isoformat(),
        "last_review": (
            card.last_review.isoformat() if card.last_review else None
        ),
    }


def create_app(store: CardStore | None = None, *, seed: bool = True) -> Flask:
    app = Flask(__name__)
    store = store or CardStore()
    app.config["CARD_STORE"] = store

    if seed and not store.list_cards():
        store.seed_demo_cards()

    def _store() -> CardStore:
        return app.config["CARD_STORE"]

    @app.get("/")
    def index() -> Any:
        first_card = _store().list_cards()[0]
        return render_template_string(
            PAGE_TEMPLATE,
            card_id=first_card.card_id,
        )

    @app.get("/cards")
    def list_cards() -> Any:
        current_store = _store()
        return jsonify(
            {
                "cards": [
                    card_payload(card, current_store.scheduler)
                    for card in current_store.list_cards()
                ]
            }
        )

    @app.post("/review")
    def review() -> Any:
        current_store = _store()
        payload = request.get_json(silent=True) or {}

        raw_card_id = payload.get("card_id")
        raw_rating = payload.get("rating")
        if raw_card_id is None or raw_rating is None:
            return jsonify({"error": "card_id and rating are required"}), 400

        try:
            card_id = int(raw_card_id)
        except (TypeError, ValueError):
            return jsonify({"error": "card_id must be an int"}), 400

        try:
            rating = Rating(int(raw_rating))
        except (TypeError, ValueError):
            return jsonify({"error": "rating must be one of 1,2,3,4"}), 400

        review_datetime = None
        raw_datetime = payload.get("review_datetime")
        if raw_datetime is not None:
            review_datetime = datetime.fromisoformat(raw_datetime)
            if review_datetime.tzinfo is None:
                review_datetime = review_datetime.replace(tzinfo=timezone.utc)

        review_duration = payload.get("review_duration")

        try:
            card, _, entry = current_store.review(
                card_id=card_id,
                rating=rating,
                review_datetime=review_datetime,
                review_duration=review_duration,
            )
        except KeyError:
            return jsonify({"error": f"unknown card_id {card_id}"}), 404

        return jsonify(
            {
                "card": card_payload(card, current_store.scheduler),
                "timeline_entry": entry,
            }
        )

    @app.get("/cards/<int:card_id>/timeline")
    def card_timeline(card_id: int) -> Any:
        current_store = _store()
        try:
            card = current_store.get_card(card_id)
        except KeyError:
            return jsonify({"error": f"unknown card_id {card_id}"}), 404

        return jsonify(
            {
                "card": card_payload(card, current_store.scheduler),
                "timeline": current_store.timeline_for(card_id),
            }
        )

    @app.get("/cards/<int:card_id>/timeline/play")
    def card_timeline_play(card_id: int) -> Any:
        current_store = _store()
        try:
            current_store.get_card(card_id)
        except KeyError:
            return f"unknown card_id {card_id}", 404
        return render_template_string(PAGE_TEMPLATE, card_id=card_id)

    return app


PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FSRS step timeline</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 860px; }
    h1 { font-size: 1.25rem; }
    .row { display: flex; gap: .5rem; flex-wrap: wrap; margin: 1rem 0; }
    button { padding: .5rem .9rem; border-radius: .4rem; border: 1px solid #888;
             background: #eef; cursor: pointer; font-size: .95rem; }
    button.rate-1 { background: #f8d7da; }
    button.rate-2 { background: #fff3cd; }
    button.rate-3 { background: #d1e7dd; }
    button.rate-4 { background: #cfe2ff; }
    table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
    th, td { border: 1px solid #999; padding: .45rem .6rem; text-align: left;
             font-size: .92rem; }
    tr.current { outline: 3px solid #ffb703; background: #fff7d6; }
    tr.current td:first-child { font-weight: 700; }
    .pill { display: inline-block; padding: .1rem .5rem; border-radius: 1rem;
            border: 1px solid #888; font-size: .8rem; }
    #cardMeta { font-size: .9rem; opacity: .85; }
  </style>
</head>
<body>
  <h1>FSRS learning-step timeline</h1>
  <div id="cardMeta">card: <span id="cardId">{{ card_id }}</span></div>
  <div class="row">
    <button class="rate-1" data-rating="1">Again</button>
    <button class="rate-2" data-rating="2">Hard</button>
    <button class="rate-3" data-rating="3">Good</button>
    <button class="rate-4" data-rating="4">Easy</button>
    <button id="replay">Replay timeline</button>
    <button id="refresh">Refresh</button>
  </div>
  <table>
    <thead>
      <tr><th>#</th><th>Rating</th><th>State</th><th>Step</th>
          <th>Due</th><th>Remaining</th></tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <script>
    const cardId = document.getElementById('cardId').textContent.trim();

    function fmt(iso) {
      const d = new Date(iso);
      return d.toLocaleString() + ' (' + iso + ')';
    }

    function render(data) {
      const meta = document.getElementById('cardMeta');
      const card = data.card;
      meta.innerHTML = 'card: <b>' + card.card_id + '</b> &nbsp; state=' +
        card.state_name + ' &nbsp; step=' +
        (card.step === null ? '-' : card.step) +
        ' &nbsp; remaining=' + card.remaining_steps;

      const tbody = document.getElementById('rows');
      tbody.innerHTML = '';
      data.timeline.forEach(function (entry, i) {
        const tr = document.createElement('tr');
        if (entry.current) tr.className = 'current';
        const cells = [
          String(i + 1),
          entry.rating_name,
          '<span class="pill">' + entry.state_name + '</span>',
          entry.step === null ? '-' : String(entry.step),
          fmt(entry.due),
          String(entry.remaining_steps),
        ];
        cells.forEach(function (text) {
          const td = document.createElement('td');
          td.innerHTML = text;
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      const current = tbody.querySelector('tr.current');
      if (current) current.scrollIntoView({ block: 'nearest' });
    }

    async function loadTimeline() {
      const res = await fetch('/cards/' + encodeURIComponent(cardId) + '/timeline');
      render(await res.json());
    }

    async function refreshCards() {
      await fetch('/cards');
    }

    async function rate(value) {
      const res = await fetch('/review', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ card_id: Number(cardId), rating: value }),
      });
      if (!res.ok) { alert('review failed'); return; }
      await refreshCards();
      await loadTimeline();
    }

    function replay() {
      const rows = Array.from(document.querySelectorAll('#rows tr'));
      rows.forEach(function (tr) { tr.classList.remove('current'); });
      rows.forEach(function (tr, i) {
        setTimeout(function () {
          rows.forEach(function (other) { other.classList.remove('current'); });
          tr.classList.add('current');
          tr.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }, 900 * (i + 1));
      });
    }

    document.querySelectorAll('button[data-rating]').forEach(function (btn) {
      btn.addEventListener('click', function () { rate(btn.dataset.rating); });
    });
    document.getElementById('replay').addEventListener('click', replay);
    document.getElementById('refresh').addEventListener('click', loadTimeline);

    loadTimeline().then(replay);
  </script>
</body>
</html>
"""


def main() -> None:
    app = create_app()
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()


__all__ = ["CardStore", "create_app", "main"]
